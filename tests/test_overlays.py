"""Overlays: a user's hand-written edits to an installed dictionary.

What these guard is the promise an overlay makes: one wrong or missing entry is fixed in a
small file the user keeps, every transcription uses the fix, an upgrade of the base never
silently undoes it or breaks on it, and a cached transcript made before the edit is never
served as if it had been made after.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from stt_cli import archive, catalog, dictionary, installed, overlays, pipeline, sources, yamlsubset
from stt_cli._errors import EXIT_OK, EXIT_USAGE, UsageError
from stt_cli.cli import main
from stt_cli.config import Settings
from stt_cli.models import EngineInfo, MediaInfo, Segment, Transcript

FIXTURES = Path(__file__).parent / "fixtures" / "dictionaries"
QUIET = pipeline.Reporter(quiet=True)

# The spec's example, against fixture-tools v2 (conloca, hyperide, figma).
EXAMPLE = """\
# customizing fixture-tools
base_version: 2
entries:
  conloca:
    op: replace
    aka: ["ConLog", 'Kon''loka']   # ConLoka is no longer corrected
    note: "our fork, not the upstream project"
  hyperide:
    op: disable
  my-codename:
    op: add
    term: ProjNova
    aka:
      - Proj Nova
"""


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("STT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _overlay(dict_id: str, text: str) -> Path:
    path = installed.overlay_path(dict_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


def _install(dict_id: str = "fixture-tools") -> None:
    installed.install(sources.BundledSource(FIXTURES), dict_id)


def _base(version: int = 2) -> catalog.Distributable:
    document = {
        "id": "tools",
        "version": version,
        "title": "tools",
        "tags": ["gaming"],
        "languages": ["en"],
        "license": "CC0-1.0",
        "source": "written for this test",
        "terms": [
            {"id": "conloca", "term": "ConLoca", "aka": ["ConLog", "ConLoka"], "note": "a project"},
            {"id": "hyperide", "term": "HyperIDE", "aka": ["hyper ide"]},
            {"id": "figma", "term": "Figma", "aka": ["Vigma"]},
        ],
    }
    blob = json.dumps(document).encode("utf-8")
    return catalog.parse(blob, origin="tools", tags=catalog.read_tags())


def _compose(text: str, version: int = 2) -> overlays.Composed:
    return overlays.compose(_base(version), overlays.parse(text, origin="o.yaml"))


def _terms(composed: overlays.Composed) -> list[tuple[str, dictionary.Term]]:
    return [(entry.id, entry.term) for entry in composed.dictionary.entries]


def _corrected(text: str) -> str:
    """``text`` after the pipeline's own dictionary stage, with the glossary `_resolve` read."""
    settings, terms = pipeline._resolve(Settings(), QUIET)
    media = MediaInfo(path="rec.wav", sha256="a" * 64, size_bytes=1, duration=2.0)
    transcript = Transcript(media=media, engine=EngineInfo(backend="x", model="y"))
    transcript.segments = [Segment(start=0.0, end=2.0, text=text, confidence=0.9)]
    pipeline._apply_dictionary(transcript, settings, terms, QUIET)
    return transcript.segments[0].text


def _fingerprint() -> str:
    settled, _ = pipeline._resolve(Settings(), QUIET)
    return archive.fingerprint(settled)


# ── the YAML subset ───────────────────────────────────────────────────────────


def test_the_subset_reads_the_spec_example_with_the_line_of_every_key() -> None:
    document = yamlsubset.parse(EXAMPLE, "o.yaml")
    assert document == {
        "base_version": 2,
        "entries": {
            "conloca": {
                "op": "replace",
                "aka": ["ConLog", "Kon'loka"],
                "note": "our fork, not the upstream project",
            },
            "hyperide": {"op": "disable"},
            "my-codename": {"op": "add", "term": "ProjNova", "aka": ["Proj Nova"]},
        },
    }
    assert isinstance(document, yamlsubset.Block)
    entries = document["entries"]
    assert isinstance(entries, yamlsubset.Block)
    assert entries.lines == {"conloca": 4, "hyperide": 8, "my-codename": 10}


def test_the_subset_keeps_yaml_1_1_booleans_as_text_and_reads_escapes_and_integers() -> None:
    document = yamlsubset.parse(
        '---\na: yes\nb: off\nc: -3\nd: "007"\ne: "\\u00e9\\t\\"x\\""\nf: [on, 2,]\ng:\n- no\n',
        "o",
    )
    assert document == {
        "a": "yes",
        "b": "off",
        "c": -3,
        "d": "007",
        "e": 'é\t"x"',
        "f": ["on", 2],
        "g": ["no"],
    }
    assert yamlsubset.parse("# nothing but a comment\n", "o") is None


@pytest.mark.parametrize(
    ("text", "line", "said"),
    [
        ("a: 1\nb: 2\na: 3\n", 3, "repeats the key 'a' from line 1"),
        ("a: &anchor 1\n", 1, "'&'"),
        ("a: *alias\n", 1, "'*'"),
        ("a: !tag 1\n", 1, "'!'"),
        ("a: |\n  block\n", 1, "'|'"),
        ("a: >\n  folded\n", 1, "'>'"),
        ("a: {b: 1}\n", 1, "flow mapping"),
        ("a:\n  - b: 1\n", 2, "mapping inside a list"),
        ("a: one\n  two\n", 2, "indented deeper"),
        ("a:\n\tb: 1\n", 2, "tab"),
        ("a: null\n", 1, "'null'"),
        ("a: ~\n", 1, "'~'"),
        ("a: true\n", 1, "'true'"),
        ('a: "open\n', 1, "does not close"),
        ("a: [x, y\n", 1, "does not close"),
        ("a: [x, [y]]\n", 1, "list in a list"),
        ("a: 1\n---\nb: 2\n", 2, "another document"),
        ("%YAML 1.2\na: 1\n", 1, "directive"),
        ("a: 'x' y\n", 1, "after its value"),
        ('a: "\\q"\n', 1, "escape"),
        ("a: b: c\n", 1, "colon"),
        ("a: - x\n", 1, "list item after a key"),
        ("a:\n    b: 1\n  c: 2\n", 3, "indented deeper"),
        ("just text\nb: 1\n", 2, "does not line up"),
    ],
)
def test_the_subset_refuses_what_it_does_not_read_and_names_the_line(
    text: str, line: int, said: str
) -> None:
    with pytest.raises(UsageError) as refused:
        yamlsubset.parse(text, "o.yaml")
    assert refused.value.what.startswith(f"o.yaml line {line} ")
    assert said in refused.value.what


# ── parsing an overlay ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "line", "said"),
    [
        ("entries:\n  x1:\n    op: disable\n", 1, "needs `base_version:`"),
        ("base_version: two\n", 1, "not a whole number"),
        ('entries:\nbase_version: "3"\n', 2, "not a whole number"),
        ("base_version: 0\n", 1, "not a whole number"),
        ("base_version: 2\nextra: 1\n", 2, "'extra'"),
        ("base_version: 2\nentries:\n  x1:\n    op: rename\n", 4, "the op 'rename'"),
        ("base_version: 2\nentries:\n  x1:\n    term: X\n", 3, "no op"),
        ("base_version: 2\nentries:\n  x1:\n    op: add\n", 3, "no term"),
        ("base_version: 2\nentries:\n  x1:\n    op: disable\n    note: n\n", 5, "also sets note"),
        ("base_version: 2\nentries:\n  x1:\n    op: replace\n", 3, "with nothing"),
        ("base_version: 2\nentries:\n  x1:\n    op: add\n    colour: red\n", 5, "'colour'"),
        ("base_version: 2\nentries:\n  Bad_Id:\n    op: disable\n", 3, "'Bad_Id'"),
        ("base_version: 2\nentries:\n  x1:\n    op: add\n    term: 42\n", 5, "not text"),
        ("base_version: 2\nentries:\n  x1:\n    op: add\n    term: X\n    aka: V\n", 6, "aka"),
        ("base_version: 2\nentries:\n  x1:\n    op: add\n    term: Foo, Inc\n", 5, "rewrite"),
        ("base_version: 2\nentries:\n  x1:\n    op: replace\n    aka: [a, A]\n", 5, "rewrite"),
        (
            "base_version: 2\nentries:\n  x1:\n    op: replace\n    note: " + "n" * 121 + "\n",
            5,
            "rewrite",
        ),
        (
            "base_version: 2\nentries:\n  x1:\n    op: replace\n    aka: [a]\n    note: "
            + "n" * 121
            + "\n",
            6,
            "rewrite",
        ),
        (
            "base_version: 2\nentries:\n  x1:\n    op: add\n    term: X\n    aka: ["
            + ", ".join(f"a{n}" for n in range(25))
            + "]\n",
            6,
            "spellings",
        ),
        ("base_version: " + "9" * 5000 + "\n", 1, "a number too long"),
        (
            "base_version: 2\nentries:\n"
            + "".join(f"  x{n}:\n    op: disable\n" for n in range(501)),
            3,
            "has 501 entries",
        ),
        (
            "base_version: 2\nentries:\n  x1:\n    op: add\n    term: one two three four "
            "five six seven eight nine\n",
            5,
            "x1",
        ),
        (
            "base_version: 2\nentries:\n  x1:\n    op: add\n    term: A\n  x1:\n    op: disable\n",
            6,
            "repeats the key 'x1'",
        ),
    ],
)
def test_a_mistake_in_an_overlay_is_an_error_naming_its_line(
    text: str, line: int, said: str
) -> None:
    with pytest.raises(UsageError) as refused:
        overlays.parse(text, origin="o.yaml")
    assert refused.value.what.startswith(f"o.yaml line {line} "), refused.value.what
    assert said in refused.value.what


def test_an_overlay_with_no_entries_yet_is_valid_and_changes_nothing() -> None:
    composed = _compose("base_version: 2\nentries:\n")
    assert composed.dictionary.entries == _base().entries
    assert composed.warnings == ()


# ── composing, in memory ──────────────────────────────────────────────────────


def test_disable_replace_and_add_compose_in_place_and_in_order() -> None:
    composed = _compose(EXAMPLE)
    assert _terms(composed) == [
        (
            "conloca",
            dictionary.Term(
                term="ConLoca",
                aka=["ConLog", "Kon'loka"],
                note="our fork, not the upstream project",
            ),
        ),
        ("figma", dictionary.Term(term="Figma", aka=["Vigma"])),
        ("my-codename", dictionary.Term(term="ProjNova", aka=["Proj Nova"])),
    ]
    assert composed.marks == {"conloca": "replaced", "figma": "base", "my-codename": "added"}
    assert [entry.id for entry in composed.disabled] == ["hyperide"]
    assert composed.warnings == ()


def test_a_replace_keeps_every_field_it_does_not_name() -> None:
    composed = _compose(
        "base_version: 2\nentries:\n  conloca:\n    op: replace\n    term: Conloca2\n"
    )
    assert _terms(composed)[0] == (
        "conloca",
        dictionary.Term(term="Conloca2", aka=["ConLog", "ConLoka"], note="a project"),
    )


def test_a_replace_with_an_empty_list_or_note_clears_that_field() -> None:
    composed = _compose(
        "base_version: 2\nentries:\n  conloca:\n    op: replace\n    aka: []\n    note: ''\n"
    )
    assert _terms(composed)[0] == ("conloca", dictionary.Term(term="ConLoca", aka=[], note=""))


def test_an_id_the_base_no_longer_has_is_a_warning_and_never_an_error() -> None:
    composed = _compose(
        "base_version: 1\nentries:\n  gone:\n    op: replace\n    note: n\n"
        "  also-gone:\n    op: disable\n  figma:\n    op: disable\n",
        version=2,
    )
    assert [entry.id for entry in composed.dictionary.entries] == ["conloca", "hyperide"]
    assert composed.warnings == (
        "o.yaml was written against tools v1, and v2 is installed; check its entries still "
        "fit, then set base_version: 2",
        "o.yaml line 3: replace 'gone' is skipped, because tools v2 has no entry with that id",
        "o.yaml line 6: disable 'also-gone' is skipped, because tools v2 has no entry with that id",
    )


def test_an_add_whose_id_the_base_has_since_gained_is_kept_in_its_place_and_warned() -> None:
    composed = _compose("base_version: 2\nentries:\n  figma:\n    op: add\n    term: Figma\n")
    assert _terms(composed)[2] == ("figma", dictionary.Term(term="Figma"))
    assert composed.marks["figma"] == "replaced"
    assert composed.warnings == (
        "o.yaml line 3: tools v2 now has an entry 'figma' too; yours replaces it "
        "(write `op: replace` to say so)",
    )


def test_a_gained_add_whose_name_is_taken_says_only_that_it_was_skipped() -> None:
    composed = _compose("base_version: 2\nentries:\n  figma:\n    op: add\n    term: ConLog\n")
    assert _terms(composed) == [(entry.id, entry.term) for entry in _base().entries]
    assert composed.marks["figma"] == "base"
    assert composed.warnings == (
        "o.yaml line 3: 'ConLog' is already a spelling of 'ConLoca'; 'figma' is skipped and "
        "the base entry stays as it was",
    )


def test_a_name_another_entry_has_is_skipped_and_its_spellings_are_dropped() -> None:
    composed = _compose(
        "base_version: 2\nentries:\n"
        "  figma:\n    op: replace\n    term: ConLog\n"  # conloca's alias: skipped whole
        "  hyperide:\n    op: replace\n    aka: [Vigma, hyper ide]\n"  # Vigma is figma's
        "  sketch:\n    op: add\n    term: figma\n"  # an entry's name, casefolded
    )
    # Every base entry is as it was: the skipped replace left figma alone, and all that
    # survived of hyperide's replace was the spelling it already had.
    assert _terms(composed) == [(entry.id, entry.term) for entry in _base().entries]
    assert composed.marks == {"conloca": "base", "hyperide": "replaced", "figma": "base"}
    assert composed.warnings == (
        "o.yaml line 3: 'ConLog' is already a spelling of 'ConLoca'; 'figma' is skipped and "
        "the base entry stays as it was",
        "o.yaml line 6: the spelling 'Vigma' is already a spelling of 'Figma', so it is "
        "dropped from 'HyperIDE'",
        "o.yaml line 9: 'figma' is already an entry; 'sketch' is skipped and it is not added",
    )


def test_disabling_an_entry_frees_its_spellings_for_an_add() -> None:
    composed = _compose(
        "base_version: 2\nentries:\n"
        "  mine:\n    op: add\n    term: Vigma\n"
        "  figma:\n    op: disable\n"
    )
    assert composed.marks == {"conloca": "base", "hyperide": "base", "mine": "added"}
    assert composed.warnings == ()


def test_two_adds_of_one_name_keep_the_first_and_warn_about_the_second() -> None:
    composed = _compose(
        "base_version: 2\nentries:\n"
        "  first:\n    op: add\n    term: ProjNova\n"
        "  second:\n    op: add\n    term: projnova\n"
    )
    assert [entry.id for entry in composed.dictionary.entries][-1] == "first"
    assert "second" not in composed.marks
    assert composed.warnings == (
        "o.yaml line 6: 'projnova' is already an entry; 'second' is skipped and it is not added",
    )


def test_renaming_an_entry_to_a_spelling_it_keeps_does_not_list_its_name_as_a_spelling() -> None:
    composed = _compose(
        "base_version: 2\nentries:\n  conloca:\n    op: replace\n    term: ConLog\n"
    )
    assert _terms(composed)[0] == (
        "conloca",
        dictionary.Term(term="ConLog", aka=["ConLoka"], note="a project"),
    )
    assert composed.warnings == ()


# ── the effective glossary, from files on disk ────────────────────────────────


def test_the_pipeline_uses_the_overlay_disabled_replaced_and_added() -> None:
    _install()
    before = "ConLoka and hyper ide and Proj Nova and Konloka"
    assert _corrected(before) == "ConLoca and HyperIDE and Proj Nova and Konloka"
    _overlay("fixture-tools", EXAMPLE.replace("Kon''loka", "Konloka"))
    assert _corrected(before) == "ConLoka and hyper ide and ProjNova and ConLoca"


def test_editing_only_the_overlay_changes_the_fingerprint_and_reformatting_it_does_not() -> None:
    _install()
    plain = _fingerprint()
    path = _overlay("fixture-tools", "base_version: 2\nentries:\n  figma:\n    op: disable\n")
    edited = _fingerprint()
    assert edited != plain

    # The same edit, written differently: a document marker, comments, quotes, indentation.
    path.write_text(
        '---\n# mine\nbase_version: 2\nentries:\n\n  "figma":   # the design tool\n'
        "      op: 'disable'\n",
        "utf-8",
    )
    assert _fingerprint() == edited
    # A base_version that no longer matches is a warning, not a different set of words.
    path.write_text("base_version: 1\nentries:\n  figma:\n    op: disable\n", "utf-8")
    assert _fingerprint() == edited

    path.unlink()
    assert _fingerprint() == plain


def test_the_pipeline_reports_what_an_overlay_could_not_do(capsys) -> None:
    _install()
    path = _overlay("fixture-tools", "base_version: 2\nentries:\n  gone:\n    op: disable\n")
    pipeline._resolve(Settings(), pipeline.Reporter())
    assert (
        f"stt: dictionary: {path} line 3: disable 'gone' is skipped, because fixture-tools v2 "
        "has no entry with that id"
    ) in capsys.readouterr().err


def test_an_overlay_for_a_dictionary_that_is_not_installed_is_ignored_and_warned() -> None:
    _install()
    alone = _fingerprint()
    stray = _overlay("fixture-games", "base_version: 1\nentries:\n  x1:\n    op: disable\n")
    misnamed = _overlay("fixture-tools", "base_version: 2\n").with_suffix(".yml")
    misnamed.write_text("entries: [not even read", "utf-8")
    (stray.parent / "backup.yaml").mkdir()  # a directory is not an overlay, and not noise

    _, warnings = installed.load_effective(Settings())
    assert _fingerprint() == alone
    assert warnings == [
        f"{str(stray)!r} is ignored: 'fixture-games' is not installed "
        "(`stt dict search` lists what can be)",
        f"{str(misnamed)!r} is ignored: an overlay is named <id>.yaml",
    ]


def test_an_overlay_named_in_another_case_is_ignored_even_where_the_disk_would_open_it(
    capsys, monkeypatch
) -> None:
    from stt_cli.commands import dict_cmd

    _install()
    alone = _fingerprint()
    wrong = _overlay("fixture-tools", "base_version: 2\nentries:\n  figma:\n    op: disable\n")
    wrong = wrong.rename(wrong.with_name("Fixture-Tools.yaml"))
    _, warnings = installed.load_effective(Settings())
    assert _fingerprint() == alone
    assert warnings == [
        f"{str(wrong)!r} is ignored: 'Fixture-Tools' is not installed "
        "(`stt dict search` lists what can be)"
    ]

    monkeypatch.setattr(dict_cmd, "_source", lambda args: sources.BundledSource(FIXTURES))
    assert main(["dict", "install", "fixture-tools"]) == EXIT_OK
    assert "your overlay" not in capsys.readouterr().out


def test_a_fifo_where_an_overlay_goes_is_no_overlay_and_never_blocks() -> None:
    _install()
    alone = _fingerprint()
    path = installed.overlay_path("fixture-tools")
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    assert _fingerprint() == alone


def test_installed_with_nothing_installed_still_names_an_orphaned_overlay(capsys) -> None:
    stray = _overlay("fixture-games", "base_version: 1\n")
    assert main(["dict", "installed"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "no dictionaries are installed" in out
    assert f"warning: {str(stray)!r} is ignored: 'fixture-games' is not installed" in out


def test_a_broken_overlay_is_an_error_that_names_the_file_and_line(capsys) -> None:
    _install()
    path = _overlay("fixture-tools", "base_version: 2\nentries:\n  figma:\n    op: rename\n")
    with pytest.raises(UsageError) as refused:
        installed.load_effective(Settings())
    assert refused.value.what.startswith(f"{path} line 4 ")
    assert str(path) in refused.value.how

    assert main(["dict", "installed"]) == EXIT_USAGE
    assert f"{path} line 4" in capsys.readouterr().err


def test_with_the_dictionary_off_no_overlay_is_even_read() -> None:
    from stt_cli import config

    _install()
    _overlay("fixture-tools", "base_version: [broken\n")
    config.save_setting("dictionary", False)
    terms, warnings = installed.load_effective(config.load_settings())
    assert not terms and warnings == []


# ── what the commands show ────────────────────────────────────────────────────


def test_show_marks_every_entry_and_lists_the_disabled_ones_and_every_warning(capsys) -> None:
    _install()
    path = _overlay(
        "fixture-tools",
        EXAMPLE.replace("base_version: 2", "base_version: 1")
        + "  gone:\n    op: replace\n    note: n\n",
    )
    assert main(["dict", "show", "fixture-tools"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"  overlay: {path} (written against v1)" in out
    assert "  replaced  conloca      ConLoca (heard as: ConLog, Kon'loka)" in out
    assert "  base      figma        Figma (heard as: Vigma) — a design tool" in out
    assert "  added     my-codename  ProjNova (heard as: Proj Nova)" in out
    assert "disabled by the overlay:\n  hyperide     HyperIDE (heard as: hyper ide)" in out
    assert "3 term(s) from " in out and ", 1 disabled" in out
    assert f"warning: {path} was written against fixture-tools v1, and v2 is installed" in out
    assert f"warning: {path} line 15: replace 'gone' is skipped" in out


def test_show_without_an_overlay_marks_everything_base_and_says_where_one_would_go(
    capsys,
) -> None:
    _install()
    assert main(["dict", "show", "fixture-tools"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "  base      conloca   ConLoca (heard as: ConLog, ConLoka)" in out
    assert f"overlay: none (write {installed.overlay_path('fixture-tools')}" in out
    assert "disabled" not in out


def test_show_names_a_misnamed_yml_overlay_next_to_saying_there_is_none(capsys) -> None:
    _install()
    misnamed = _overlay("fixture-tools", "base_version: 2\n").rename(
        installed.overlay_path("fixture-tools").with_suffix(".yml")
    )
    assert main(["dict", "show", "fixture-tools"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"warning: {str(misnamed)!r} is ignored: an overlay is named <id>.yaml" in out


def test_another_dictionarys_broken_overlay_does_not_stop_show(capsys) -> None:
    _install()
    _install("fixture-games")
    _overlay("fixture-games", "base_version: 1\nentries:\n  x1:\n    op: rename\n")
    assert main(["dict", "show", "fixture-tools"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "  base      conloca   ConLoca (heard as: ConLog, ConLoka)" in out
    assert "warning: fixture-games is counted here without its overlay: " in out

    assert main(["dict", "show", "fixture-games"]) == EXIT_USAGE
    assert "line 4" in capsys.readouterr().err


@pytest.mark.parametrize("relative", ["cfg", "~/cfg"])
def test_a_relative_xdg_config_home_counts_as_unset(monkeypatch, tmp_path, relative) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("XDG_CONFIG_HOME", relative)
    assert installed.overlay_directory() == tmp_path / "user/.config/stt-cli/dicts/overlays"


def test_installed_counts_the_effective_terms_and_reinstall_rechecks_the_overlay(
    capsys, monkeypatch
) -> None:
    from stt_cli.commands import dict_cmd

    monkeypatch.setattr(dict_cmd, "_source", lambda args: sources.BundledSource(FIXTURES))
    _install()
    path = _overlay("fixture-tools", "base_version: 1\nentries:\n  figma:\n    op: disable\n")
    assert main(["dict", "installed"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"   2 terms  Fixture tool names  overlay: {path}" in out
    assert f"warning: {path} was written against fixture-tools v1" in out

    assert main(["dict", "install", "fixture-tools"]) == EXIT_OK
    assert f"warning: {path} was written against fixture-tools v1" in capsys.readouterr().out

    # The upgrade is written before the overlay is read, so a broken overlay is said, and
    # the install still reports success: re-running it must not look like nothing happened.
    path.write_text("base_version: 2\nentries:\n  figma:\n    op: rename\n", "utf-8")
    assert main(["dict", "install", "fixture-tools"]) == EXIT_OK
    assert f"warning: {path} line 4 gives entry 'figma' the op 'rename'" in (
        capsys.readouterr().out
    )


def test_the_dict_help_shows_how_to_write_an_overlay(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["dict", "--help"])
    out = capsys.readouterr().out
    assert "stt-cli/dicts/overlays/<id>.yaml" in out and "op: disable" in out
