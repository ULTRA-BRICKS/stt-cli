"""catalog — dictionaries somebody else curated, in a shape that can be published and installed.

WHY A SECOND FORMAT
    ``dictionary.json`` is one person's list, and its entries are known only by their own
    casefolded text (`Dictionary.find`). A dictionary that is published, versioned and
    installed by strangers needs more than that: a stable id to install it by, a version to
    tell an upgrade from a reinstall, tags to find it under, and the languages, licence and
    provenance of what is inside. And every TERM needs an id of its own, independent of its
    text: a local overlay has to keep saying "disable the entry that used to be called X"
    after the next version of the base dictionary edits X's spelling or its aliases, and a
    key made of the text breaks the moment the text changes. The id lives here, in the
    distributable layer only — adding it to `Term` would change the personal file's shape
    and the digest of every archived run.

THE FORMAT, FIELD BY FIELD
    One JSON object per file, ``stt_cli/dictionaries/<id>.json``; every field is required
    unless marked optional, and an unknown field is an error::

        id         stable slug naming the dictionary everywhere: the file name, the install
                   command, the overlay file, the website URL. Never changes once published.
        version    positive integer, raised by one on every published change, so an
                   installer can tell an upgrade and an overlay can tell its base moved.
        title      the human-readable name on listings, at most 80 characters.
        tags       from the committed vocabulary ``tags.json``: exactly one topic tag, any
                   number of facets. Drives search, the website filter, suggestions.
        languages  ISO 639-1 codes of the languages the terms are SPOKEN in.
        license    the SPDX id of the content (see ``LICENSES``).
        source     one line: where the terms came from and how they were checked.
        terms      ordered list; order is priority, as in the personal dictionary, because
                   the decoder prompt takes terms from the top. At most 500 terms
                   (``dictionary.MAX_SCREENED_TERMS``), the cap a personal dictionary has,
                   since every term goes through `Dictionary.add`. Each term is:
            id     stable slug for this entry, unique in the file and independent of its
                   text (a Russian term gets a transliterated id), never reused.
            term   the correct spelling.
            aka    optional: recorded wrong spellings, substituted as facts.
            note   optional: one line for the language-model correction pass.

THE CONTRACT
    ``docs/specs/dictionaries.md`` sections 3 and 4, summarised by "THE FORMAT, FIELD BY
    FIELD" above. A file is valid only if every term, fed in file order through
    `Dictionary.add` into a fresh dictionary, is accepted, and is unchanged by `normalized`.
    Calling `add` rather than re-listing its checks is what guarantees an installed dictionary
    never holds an entry a typist would have been refused, and "unchanged by `normalized`"
    means a published file never holds a character the loader would silently rewrite, so what
    the website shows is what the pipeline uses.

WHAT THIS MODULE DOES NOT DO
    It does not change what `dictionary.load()` returns: installing a dictionary and merging
    it into the glossary a transcription uses is a separate step (#83), and so are overlays
    (#84). This module reads, validates and indexes the files; nothing in the transcription
    path imports it yet.

PUBLISHING
    The bundled dictionaries live in ``stt_cli/dictionaries/`` beside ``tags.json`` (the tag
    vocabulary) and ``index.json`` (the catalog index the command line and the website both
    read unchanged). After adding or editing a dictionary, regenerate the index with
    ``python -m stt_cli.catalog``; a test regenerates it and fails if the committed copy
    differs by a single byte.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ._errors import UsageError, guard
from .dictionary import MAX_DICTIONARY_BYTES, NOTE_CHARS, Dictionary, Term, normalized
from .jsonio import in_units, read_text

DIRECTORY = Path(__file__).parent / "dictionaries"
INDEX = "index.json"
TAGS = "tags.json"

# The index's own shape. Nothing reads the index yet; the reader that installs from it (#83)
# is meant to refuse a format it does not know, so a future shape says "upgrade stt" rather
# than being misread.
INDEX_FORMAT = 1

TITLE_CHARS = 80
# One line saying where the terms came from and how they were checked. Long enough to name a
# glossary and a method; short enough that it stays a line on a listing.
SOURCE_CHARS = 300

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUG_LENGTHS = range(2, 65)

_ENVELOPE = ("id", "version", "title", "tags", "languages", "license", "source", "terms")
_TERM_FIELDS = ("aka", "id", "note", "term")
_TOO_BIG = "a dictionary is a list of names, not a corpus"

# ISO 639-1, the two-letter codes. The list is closed and has not changed in years, so it is
# written out rather than fetched: validation must work offline and give the same answer on
# every machine.
_ISO_639_1 = """
    aa ab ae af ak am an ar as av ay az ba be bg bi bm bn bo br bs ca ce ch co cr cs cu cv cy
    da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht
    hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky
    la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny
    oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss
    st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo
    za zh zu
"""
LANGUAGES = frozenset(_ISO_639_1.split())

# SPDX identifiers a dictionary's content may carry: the subset of the SPDX list that fits a
# word list somebody installs and redistributes. Every entry is a real SPDX id; the subset is
# deliberate, so a publisher reaching for a licence outside it is a review, not an accident.
LICENSES = frozenset(
    {
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "ODbL-1.0",
        "ODC-By-1.0",
        "PDDL-1.0",
        "MIT",
        "Apache-2.0",
    }
)


@dataclass(frozen=True, slots=True)
class Tag:
    """One entry of the tag vocabulary: a topic (exactly one per dictionary) or a facet."""

    kind: Literal["topic", "facet"]
    label: str


@dataclass(frozen=True, slots=True)
class Entry:
    """A term, and the id an overlay addresses it by whatever its text becomes."""

    id: str
    term: Term


@dataclass(frozen=True, slots=True)
class Distributable:
    """One validated distributable dictionary, and the hash of the exact bytes it came from."""

    id: str
    version: int
    title: str
    tags: tuple[str, ...]
    languages: tuple[str, ...]
    license: str
    source: str
    entries: tuple[Entry, ...]
    sha256: str

    def index_entry(self) -> dict[str, object]:
        """This dictionary's row of the catalog index."""
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "tags": list(self.tags),
            "languages": list(self.languages),
            "license": self.license,
            "source": self.source,
            "term_count": len(self.entries),
            "sha256": self.sha256,
        }


def read_tags(directory: Path = DIRECTORY) -> dict[str, Tag]:
    """The committed tag vocabulary: ``{slug: {"kind": "topic" | "facet", "label": ...}}``."""
    target = directory / TAGS
    raw = _decoded(_read(target), str(target))
    if not isinstance(raw, dict) or not raw:
        raise _invalid(str(target), "is not a tag vocabulary", 'expected {"slug": {...}}')
    tags: dict[str, Tag] = {}
    for slug, spec in raw.items():
        where = f"{target}: tag {slug!r}"
        _require_slug(slug, where)
        if not isinstance(spec, dict) or set(spec) != {"kind", "label"}:
            raise _invalid(where, "is not a tag", 'expected {"kind": ..., "label": ...}')
        kind, label = spec["kind"], spec["label"]
        _require_line(label, f"{where} label", TITLE_CHARS)
        if kind not in ("topic", "facet"):
            raise _invalid(where, f"has kind {kind!r}", 'kind is "topic" or "facet"')
        tags[slug] = Tag(kind=kind, label=label)
    return tags


def parse(blob: bytes, *, origin: str, tags: Mapping[str, Tag]) -> Distributable:
    """Validate one dictionary file's bytes. ``origin`` names it in every error.

    Everything that is wrong is an error, not a repair: this is what a user installs, and a
    loader that quietly fixed a file would install something other than what was reviewed,
    published and hashed.
    """
    if len(blob) > MAX_DICTIONARY_BYTES:
        raise _invalid(origin, f"is larger than {in_units(MAX_DICTIONARY_BYTES)}", _TOO_BIG)
    raw = _decoded(blob, origin)
    if not isinstance(raw, dict):
        raise _invalid(origin, "is not a dictionary", "expected a JSON object")
    _require_exact_fields(raw, _ENVELOPE, _ENVELOPE, origin)
    ident = raw["id"]
    _require_slug(ident, f"{origin}: id")
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _invalid(
            origin,
            f"has version {version!r}",
            "the version is a positive integer, raised by one on every published change",
        )
    _require_line(raw["title"], f"{origin}: title", TITLE_CHARS)
    _require_line(raw["source"], f"{origin}: source", SOURCE_CHARS)
    tag_list = _unique_strings(raw["tags"], f"{origin}: tags")
    unknown = [tag for tag in tag_list if tag not in tags]
    if unknown:
        raise _invalid(
            origin,
            f"uses tags that are not in the vocabulary: {_listed(unknown)}",
            f"use tags from {TAGS}, or add the new tag there in the same change",
        )
    topics = [tag for tag in tag_list if tags[tag].kind == "topic"]
    if len(topics) != 1:
        raise _invalid(
            origin,
            f"has {len(topics)} topic tags ({', '.join(topics) or 'none'})",
            "a dictionary has exactly one topic tag and any number of facets",
        )
    languages = _unique_strings(raw["languages"], f"{origin}: languages")
    strange = [code for code in languages if code not in LANGUAGES]
    if strange:
        raise _invalid(
            origin,
            f"names languages that are not ISO 639-1 codes: {_listed(strange)}",
            'use two-letter codes, for example ["ru", "en"]',
        )
    if not isinstance(raw["license"], str) or raw["license"] not in LICENSES:
        raise _invalid(
            origin,
            f"has license {raw['license']!r}",
            f"use one of the SPDX ids {', '.join(sorted(LICENSES))}",
        )
    return Distributable(
        id=ident,
        version=version,
        title=raw["title"],
        tags=tuple(tag_list),
        languages=tuple(languages),
        license=raw["license"],
        source=raw["source"],
        entries=_entries(raw["terms"], origin),
        sha256=hashlib.sha256(blob).hexdigest(),
    )


def _entries(raw: object, origin: str) -> tuple[Entry, ...]:
    """Every term, accepted by `Dictionary.add` exactly as a typist's entry would have to be."""
    if not isinstance(raw, list) or not raw:
        raise _invalid(origin, "has no terms", 'expected "terms": [{"id": ..., "term": ...}]')
    glossary = Dictionary()
    entries: list[Entry] = []
    seen: set[str] = set()
    for position, item in enumerate(raw, start=1):
        slot = f"{origin}: term {position}"
        if not isinstance(item, dict):
            raise _invalid(slot, "is not an object", 'expected {"id": ..., "term": ...}')
        _require_exact_fields(item, ("id", "term"), _TERM_FIELDS, slot)
        ident = item["id"]
        _require_slug(ident, f"{slot} id")
        where = f"{origin}: term {ident!r}"
        if ident in seen:
            raise _invalid(where, "appears twice", "a term id is unique within its dictionary")
        seen.add(ident)
        term = _term(item, where)
        if normalized(term) != term:
            raise _invalid(
                where,
                "would be rewritten by the loader",
                "no `,` `|` `~`, control characters, repeated or edge spaces, duplicate "
                f"aliases, or a note over {NOTE_CHARS} characters",
            )
        try:
            fresh = glossary.add(term)
        except UsageError as refused:
            # The refusal's own `how` is advice for the personal dictionary (`stt dict rm`),
            # which a published file cannot follow.
            raise UsageError(
                what=f"{where} is refused: {refused.what}",
                why=refused.why,
                how="edit the entry: a bundled term must be one `stt dict add` would accept",
            ) from refused
        if not fresh:
            raise _invalid(where, f"repeats the term {term.term!r}", "merge the two entries")
        entries.append(Entry(id=ident, term=term))
    return tuple(entries)


def _term(item: Mapping[str, object], where: str) -> Term:
    text, aka, note = item["term"], item.get("aka", []), item.get("note", "")
    if not isinstance(text, str) or not text:
        raise _invalid(where, "has no term text", '"term" is the correct spelling, as text')
    if not isinstance(aka, list) or not all(isinstance(alias, str) for alias in aka):
        raise _invalid(where, 'has an "aka" that is not a list of spellings', '["Vigma"]')
    if not isinstance(note, str):
        raise _invalid(where, 'has a "note" that is not text', "a note is one line of text")
    return Term(term=text, aka=list(aka), note=note)


def bundled(directory: Path = DIRECTORY) -> list[Distributable]:
    """Every dictionary shipped with stt, validated, sorted by id.

    On top of each file's own rules, the bundled set is checked as a whole: a file's id must
    be its name, and no spelling — a term or an alias — may belong to two dictionaries. A user
    may install any combination of these, and a spelling claimed twice would be silently
    dropped from one of them on the way into the glossary.
    """
    tags = read_tags(directory)
    found: list[Distributable] = []
    owner: dict[str, tuple[str, str]] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name in (INDEX, TAGS):
            continue
        dictionary = parse(_read(path), origin=str(path), tags=tags)
        if dictionary.id != path.stem:
            raise _invalid(
                str(path),
                f"has id {dictionary.id!r}",
                "a dictionary's id is its file name without .json",
            )
        for entry in dictionary.entries:
            # Term first, then aliases, in file order: a set here made which of two conflicts
            # got reported depend on string hashing, so the same file could fail differently.
            spellings = (entry.term.term, *entry.term.aka)
            for spelling in dict.fromkeys(text.casefold() for text in spellings):
                other = owner.setdefault(spelling, (dictionary.id, entry.id))
                if other[0] != dictionary.id:
                    raise _invalid(
                        f"{path}: term {entry.id!r}",
                        f"claims {spelling!r}, which already belongs to {other[0]}/{other[1]}",
                        "keep the spelling in the dictionary whose topic it belongs to",
                    )
        found.append(dictionary)
    if not found:
        # Otherwise `write_index` would blank the committed index and report success.
        raise _invalid(
            str(directory), "holds no dictionaries", f"a catalog is <id>.json files beside {TAGS}"
        )
    return sorted(found, key=lambda dictionary: dictionary.id)


def render_index(dictionaries: list[Distributable]) -> str:
    """The catalog index, byte for byte: sorted by id, sorted keys, no timestamps."""
    rows = [dictionary.index_entry() for dictionary in sorted(dictionaries, key=lambda d: d.id)]
    document = {"format": INDEX_FORMAT, "dictionaries": rows}
    return json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _read(path: Path) -> bytes:
    """The file's exact bytes, through the one guarded reader (`jsonio.read_text`).

    Strict UTF-8 decodes and re-encodes losslessly, so the bytes hashed are the bytes on disk.
    """
    text = read_text(path, how="restore it", limit=MAX_DICTIONARY_BYTES, too_big=_TOO_BIG)
    if text is None:
        raise UsageError(
            what=f"could not read {path}",
            why="it is missing or not a regular file",
            how="restore it",
        )
    return text.encode("utf-8")


def _decoded(blob: bytes, origin: str) -> object:
    try:
        return json.loads(blob.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as exc:
        # `UnicodeDecodeError` and `JSONDecodeError` are both `ValueError`s.
        raise UsageError(
            what=f"could not read {origin}", why=str(exc), how="fix the JSON, saved as UTF-8"
        ) from exc


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object, refused if a key repeats.

    The standard parser keeps the last of two equal keys without a word, so a file carrying
    two "id" fields would validate as whichever one came second — and a reviewer reading the
    first would approve something other than what gets installed.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"the key {key!r} appears twice in one object")
        result[key] = value
    return result


def _require_exact_fields(
    raw: Mapping[str, object], required: tuple[str, ...], allowed: tuple[str, ...], where: str
) -> None:
    missing = [key for key in required if key not in raw]
    if missing:
        raise _invalid(where, f"is missing {', '.join(missing)}", "every required field is set")
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        # An error, as a malformed personal entry is: a misspelt "aka" would otherwise load as
        # an entry with no aliases, and nobody would find out until a word was not corrected.
        raise _invalid(
            where, f"has unknown fields {_listed(unknown)}", f"allowed: {', '.join(allowed)}"
        )


def _require_slug(value: object, where: str) -> None:
    if not isinstance(value, str) or len(value) not in SLUG_LENGTHS or not _SLUG.fullmatch(value):
        raise _invalid(
            where,
            f"is {value!r}, not a slug",
            "ids are 2 to 64 lowercase ASCII letters and digits in hyphen-separated words, "
            "like medical-general; a Russian term gets a transliterated id",
        )


def _require_line(value: object, where: str, limit: int) -> None:
    if not isinstance(value, str) or not value or " ".join(value.split()) != value:
        raise _invalid(where, "is not one line of text", "one line, no edge or repeated spaces")
    # The rule `normalized` applies to a term's text, applied to the envelope: a title or a
    # source is printed on listings, and an ESC in it is executed by the terminal showing it.
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        raise _invalid(where, "holds a control character", "plain printable text only")
    if len(value) > limit:
        raise _invalid(where, f"is {len(value)} characters long", f"keep it to {limit}")


def _unique_strings(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise _invalid(where, "is not a non-empty list of text", 'for example ["en"]')
    if len(set(value)) != len(value):
        raise _invalid(where, "repeats a value", "list each value once")
    return list(value)


def _listed(values: list[str]) -> str:
    """Values from the file, quoted and escaped, so an error cannot carry a terminal escape."""
    return ", ".join(repr(value) for value in values)


def _invalid(where: str, what: str, how: str) -> UsageError:
    return UsageError(
        what=f"{where} {what}",
        why="a distributable dictionary is installed as published, so it has to be exact",
        how=how,
    )


def write_index(directory: Path = DIRECTORY) -> Path:
    """Validate every bundled dictionary and rewrite the committed index from them."""
    from .archive import write_atomic

    target = directory / INDEX
    text = render_index(bundled(directory))
    try:
        write_atomic(target, text)
    except OSError as exc:
        raise UsageError(
            what=f"could not write {target}",
            why=str(exc),
            how="run it from a writable source checkout, not an installed package",
        ) from exc
    return target


def _main() -> int:
    target = write_index()
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(guard(_main))
