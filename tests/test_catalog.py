"""The distributable dictionaries: the format's validator, the shipped content, the index.

The shipped files are what a user installs as published, so the tests hold them to the same
limits a hand-typed entry meets (`Dictionary.add`), and hold the committed catalog index to
what the files actually say, byte for byte — the command line and the website both read it
unchanged, and a stale hash would make the bundled source refuse its own content.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from stt_cli import catalog, dictionary
from stt_cli._errors import UsageError
from stt_cli.catalog import Tag

# The topics the ticket (#81) names; the catalog must cover every one of them.
REQUIRED_TOPICS = {
    "software-engineering-tools",
    "ai-ml",
    "devops-cloud",
    "web-frontend",
    "product-design-ux",
    "russian-it-anglicisms",
    "medical-general",
    "pharmacology",
    "legal-contracts",
    "personal-finance-banking",
    "accounting-tax",
    "marketing-analytics",
    "science-math",
    "education",
    "real-estate",
    "automotive",
    "music-production",
    "gaming",
}

VOCABULARY = {
    "gaming": Tag(kind="topic", label="Gaming"),
    "music-production": Tag(kind="topic", label="Music production"),
    "jargon": Tag(kind="facet", label="Professional jargon"),
}

SAMPLE: dict[str, object] = {
    "id": "sample",
    "version": 1,
    "title": "Sample",
    "tags": ["gaming", "jargon"],
    "languages": ["en", "ru"],
    "license": "CC0-1.0",
    "source": "written for this test",
    "terms": [
        {"id": "conloca", "term": "ConLoca", "aka": ["ConLog", "ConLoka"], "note": "a project"},
        {"id": "figma", "term": "Figma", "aka": ["Vigma"]},
    ],
}


def _blob(document: object) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode()


def _parse(document: object) -> catalog.Distributable:
    return catalog.parse(_blob(document), origin="sample.json", tags=VOCABULARY)


def _files() -> list[Path]:
    return [
        path
        for path in sorted(catalog.DIRECTORY.glob("*.json"))
        if path.name not in (catalog.INDEX, catalog.TAGS)
    ]


@pytest.fixture(scope="module")
def shipped() -> list[catalog.Distributable]:
    return catalog.bundled()


# ── the format ────────────────────────────────────────────────────────────────


def test_a_valid_file_keeps_its_ids_order_and_terms() -> None:
    parsed = _parse(SAMPLE)

    assert [entry.id for entry in parsed.entries] == ["conloca", "figma"]
    assert parsed.entries[0].term == dictionary.Term("ConLoca", ["ConLog", "ConLoka"], "a project")
    assert parsed.entries[1].term == dictionary.Term("Figma", ["Vigma"])
    assert parsed.sha256 == hashlib.sha256(_blob(SAMPLE)).hexdigest()


def _terms(document: dict[str, object]) -> list[dict[str, object]]:
    terms = document["terms"]
    assert isinstance(terms, list)
    return terms


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(extra=1), "unknown fields 'extra'"),
        # A misspelt "aka" must not load as an entry with no aliases.
        (lambda d: _terms(d)[0].update(aliases=["x"]), "unknown fields 'aliases'"),
        (lambda d: d.pop("license"), "missing license"),
        (lambda d: _terms(d)[0].pop("id"), "missing id"),
        (lambda d: d.update(version=True), "has version"),
        (lambda d: d.update(version=0), "has version"),
        (lambda d: d.update(version="1.0.0"), "has version"),
        (lambda d: d.update(id="Sample"), "not a slug"),
        (lambda d: d.update(id="x"), "not a slug"),
        (lambda d: _terms(d)[1].update(id="--"), "not a slug"),
        (lambda d: d.update(tags=["gaming", "music-production"]), "2 topic tags"),
        (lambda d: d.update(tags=["jargon"]), "0 topic tags"),
        (lambda d: d.update(tags=[]), "non-empty list"),
        (lambda d: d.update(tags=["gaming", "gaming"]), "repeats a value"),
        (lambda d: d.update(tags=["gaming", "cooking"]), "not in the vocabulary: 'cooking'"),
        # An error names what the file holds escaped, so the file cannot drive the terminal.
        (lambda d: d.update(tags=["gaming", "\x1b]8;;x\x07"]), r"'\x1b]8;;x\x07'"),
        (lambda d: d.update(languages=["english"]), "ISO 639-1"),
        (lambda d: d.update(license="GPL-3.0-only"), "has license"),
        # Unhashable: a bare membership test would raise TypeError, not a diagnosed error.
        (lambda d: d.update(license=["CC0-1.0"]), "has license"),
        (lambda d: d.update(license={"id": "CC0-1.0"}), "has license"),
        (lambda d: d.update(title="x" * 81), "81 characters"),
        (lambda d: d.update(source="two\nlines"), "one line"),
        (lambda d: d.update(title="Sample\x1b[2J"), "control character"),
        (lambda d: d.update(source="zero\u200bwidth"), "control character"),
        (lambda d: d.update(terms=[]), "no terms"),
        (lambda d: _terms(d)[1].update(id="conloca"), "appears twice"),
        # The limits a typist meets, enforced by `Dictionary.add` itself.
        (lambda d: _terms(d)[1].update(aka=["ConLog"]), "already belongs to"),
        (lambda d: _terms(d)[1].update(aka=["ConLoca"]), "already belongs to"),
        (
            lambda d: _terms(d)[1].update(term="one two three four five six seven eight nine"),
            "too long",
        ),
        (lambda d: _terms(d)[1].update(term="F" * 81), "too long"),
        (lambda d: _terms(d)[0].update(aka=[f"Con{i}" for i in range(25)]), "more than 24"),
        (lambda d: _terms(d)[1].update(term="conloca"), "repeats the term"),
        # Anything the loader would silently rewrite is refused instead.
        (lambda d: _terms(d)[0].update(term="ConLoca, Inc"), "rewritten"),
        (lambda d: _terms(d)[0].update(term=" ConLoca"), "rewritten"),
        (lambda d: _terms(d)[0].update(aka=["ConLog", "conlog"]), "rewritten"),
        (lambda d: _terms(d)[0].update(note="two\nlines"), "rewritten"),
        (lambda d: _terms(d)[0].update(note="x" * 121), "rewritten"),
        (lambda d: _terms(d)[0].update(aka="ConLog"), "not a list of spellings"),
    ],
)
def test_an_invalid_file_is_refused_with_the_reason(
    mutate: Callable[[dict[str, object]], object], message: str
) -> None:
    document = copy.deepcopy(SAMPLE)
    mutate(document)

    with pytest.raises(UsageError) as refused:
        _parse(document)

    assert message in refused.value.what


def test_a_repeated_key_is_refused_rather_than_resolved_to_the_last() -> None:
    blob = _blob(SAMPLE).replace(b'"id": "sample"', b'"id": "sample", "id": "other"', 1)

    with pytest.raises(UsageError) as refused:
        catalog.parse(blob, origin="sample.json", tags=VOCABULARY)

    assert "appears twice in one object" in refused.value.why


@pytest.mark.parametrize(
    ("blob", "message"),
    [
        (b" " * (dictionary.MAX_DICTIONARY_BYTES + 1), "is larger than"),
        ('{"id": "sample"}'.encode("utf-16"), "could not read"),
        (b'{"id": ', "could not read"),
        (b"[1, 2]", "is not a dictionary"),
    ],
    ids=["oversize", "not-utf8", "truncated-json", "not-an-object"],
)
def test_bytes_that_are_not_a_dictionary_file_are_refused(blob: bytes, message: str) -> None:
    with pytest.raises(UsageError) as refused:
        catalog.parse(blob, origin="sample.json", tags=VOCABULARY)

    assert message in refused.value.what


@pytest.mark.parametrize(
    ("vocabulary", "message"),
    [
        ({"Gaming": {"kind": "topic", "label": "Gaming"}}, "not a slug"),
        ({"gaming": {"kind": "genre", "label": "Gaming"}}, "has kind 'genre'"),
        ({"gaming": {"kind": "topic", "label": "two\nlines"}}, "not one line"),
        ({"gaming": {"kind": "topic", "label": "Gaming", "extra": 1}}, "is not a tag"),
        ({}, "not a tag vocabulary"),
    ],
)
def test_an_invalid_tag_vocabulary_is_refused(
    tmp_path: Path, vocabulary: dict[str, object], message: str
) -> None:
    (tmp_path / catalog.TAGS).write_bytes(_blob(vocabulary))

    with pytest.raises(UsageError) as refused:
        catalog.read_tags(tmp_path)

    assert message in refused.value.what


def test_a_file_that_is_not_regular_is_refused_instead_of_blocking(tmp_path: Path) -> None:
    """A FIFO opened for reading waits for a writer that never comes; it must not hang."""
    directory = _catalog_dir(tmp_path, SAMPLE)
    os.mkfifo(directory / "pipe.json")

    with pytest.raises(UsageError) as refused:
        catalog.bundled(directory)

    assert refused.value.what == f"could not read {directory / 'pipe.json'}"
    assert "not a regular file" in refused.value.why


def _catalog_dir(tmp_path: Path, *documents: dict[str, object]) -> Path:
    shutil.copy(catalog.DIRECTORY / catalog.TAGS, tmp_path / catalog.TAGS)
    for document in documents:
        (tmp_path / f"{document['id']}.json").write_bytes(_blob(document))
    return tmp_path


def test_a_spelling_claimed_by_two_bundled_dictionaries_is_refused(tmp_path: Path) -> None:
    other = copy.deepcopy(SAMPLE) | {"id": "other"}
    other["terms"] = [{"id": "vercel", "term": "Vercel", "aka": ["Vigma"]}]

    with pytest.raises(UsageError) as refused:
        catalog.bundled(_catalog_dir(tmp_path, SAMPLE, other))

    assert "'vigma', which already belongs to other/vercel" in refused.value.what


def test_a_collision_is_reported_on_the_term_before_its_aliases(tmp_path: Path) -> None:
    """The same files must fail the same way on every run, whatever the string hash seed."""
    spellings = ["Figma", "Vigma", "Figmah", "Phigma", "Fegma", "Figmo", "Figgma", "Fikma"]
    sample = copy.deepcopy(SAMPLE)
    sample["terms"] = [{"id": "figma", "term": spellings[0], "aka": spellings[1:]}]
    other = copy.deepcopy(SAMPLE) | {"id": "other"}
    other["terms"] = [{"id": "rival", "term": spellings[-1], "aka": spellings[:-1]}]

    with pytest.raises(UsageError) as refused:
        catalog.bundled(_catalog_dir(tmp_path, sample, other))

    assert "claims 'figma', which already belongs to other/rival" in refused.value.what


def test_an_index_that_cannot_be_written_is_a_usage_error(tmp_path: Path) -> None:
    directory = _catalog_dir(tmp_path, SAMPLE)
    (directory / catalog.INDEX).mkdir()  # unwritable even for root, unlike a chmod

    with pytest.raises(UsageError) as refused:
        catalog.write_index(directory)

    assert refused.value.what == f"could not write {directory / catalog.INDEX}"
    assert [path.name for path in directory.iterdir() if path.suffix == ".part"] == []


def test_write_index_writes_the_committed_index_byte_for_byte(tmp_path: Path) -> None:
    directory = tmp_path / "dictionaries"
    shutil.copytree(catalog.DIRECTORY, directory)
    (directory / catalog.INDEX).unlink()

    assert catalog.write_index(directory) == directory / catalog.INDEX
    assert (directory / catalog.INDEX).read_bytes() == (
        catalog.DIRECTORY / catalog.INDEX
    ).read_bytes()


def test_a_directory_with_no_dictionaries_is_refused_rather_than_indexed_empty(
    tmp_path: Path,
) -> None:
    directory = _catalog_dir(tmp_path)

    with pytest.raises(UsageError) as refused:
        catalog.write_index(directory)

    assert "holds no dictionaries" in refused.value.what
    assert not (directory / catalog.INDEX).exists()


def test_a_dictionary_whose_id_is_not_its_file_name_is_refused(tmp_path: Path) -> None:
    directory = _catalog_dir(tmp_path, SAMPLE)
    (directory / "sample.json").rename(directory / "renamed.json")

    with pytest.raises(UsageError) as refused:
        catalog.bundled(directory)

    assert "has id 'sample'" in refused.value.what


# ── the shipped content ───────────────────────────────────────────────────────


def test_the_catalog_has_many_dictionaries_covering_every_named_topic(
    shipped: list[catalog.Distributable],
) -> None:
    vocabulary = catalog.read_tags()
    topics = {tag for entry in shipped for tag in entry.tags if vocabulary[tag].kind == "topic"}

    assert len(shipped) >= 20
    assert REQUIRED_TOPICS - topics == set()
    assert {entry.id: len(entry.entries) for entry in shipped if len(entry.entries) < 30} == {}
    assert all(entry.tags and entry.languages for entry in shipped)


@pytest.mark.parametrize("path", _files(), ids=lambda path: path.stem)
def test_every_shipped_term_is_one_a_typist_could_have_added(path: Path) -> None:
    """Straight through `Term.from_dict` and `Dictionary.add`, not through the validator."""
    glossary = dictionary.Dictionary()
    for item in json.loads(path.read_text(encoding="utf-8"))["terms"]:
        term = dictionary.Term.from_dict(item)

        assert dictionary.normalized(term) == term, item
        assert glossary.add(term), item


def test_dictionary_ids_are_unique_and_term_ids_are_unique_within_each_dictionary(
    shipped: list[catalog.Distributable],
) -> None:
    ids = [entry.id for entry in shipped]

    assert len(ids) == len(set(ids)) == len(_files())
    for entry in shipped:
        term_ids = [term.id for term in entry.entries]
        assert len(term_ids) == len(set(term_ids)), entry.id


def test_the_committed_index_is_what_the_files_say(shipped: list[catalog.Distributable]) -> None:
    committed = (catalog.DIRECTORY / catalog.INDEX).read_bytes()

    assert committed == catalog.render_index(shipped).encode("utf-8"), (
        "stale index: run `python -m stt_cli.catalog` and commit stt_cli/dictionaries/index.json"
    )
    for row in json.loads(committed)["dictionaries"]:
        blob = (catalog.DIRECTORY / f"{row['id']}.json").read_bytes()
        assert row["sha256"] == hashlib.sha256(blob).hexdigest()


def test_the_built_wheel_carries_every_dictionary_and_the_index(tmp_path: Path) -> None:
    """Data files leave the package only if packaging says so; a source checkout hides it."""
    root = Path(__file__).resolve().parent.parent
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy(root / name, source / name)
    shutil.copytree(
        root / "stt_cli", source / "stt_cli", ignore=shutil.ignore_patterns("__pycache__")
    )
    build = "import sys; from setuptools import build_meta; build_meta.build_wheel(sys.argv[1])"

    built = subprocess.run(
        [sys.executable, "-c", build, str(tmp_path / "dist")],
        cwd=source,
        capture_output=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stderr.decode(errors="replace")

    (wheel,) = (tmp_path / "dist").glob("*.whl")
    packed = set(zipfile.ZipFile(wheel).namelist())
    wanted = {f"stt_cli/dictionaries/{path.name}" for path in catalog.DIRECTORY.glob("*.json")}
    assert wanted - packed == set()
    assert f"stt_cli/dictionaries/{catalog.INDEX}" in packed
    assert f"stt_cli/dictionaries/{catalog.TAGS}" in packed
