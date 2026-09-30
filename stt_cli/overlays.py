"""overlays — a user's own edits to an installed dictionary, kept apart from it.

WHAT AN OVERLAY IS
    One wrong entry in an installed dictionary, or one missing entry a team needs, should not
    force a choice between living with it and copying the whole file into the personal
    dictionary, which loses every later upgrade. An overlay is a small YAML file the user
    writes by hand, ``<config>/stt-cli/dicts/overlays/<dict_id>.yaml`` (the path is
    `installed.overlay_path`), naming entries of the base dictionary by their stable term id::

        base_version: 3          # the installed version this overlay was written against
        entries:
          conloca:               # a term id from the base dictionary
            op: replace
            aka: ["ConLog", "our-internal-nickname"]
          hyperide:
            op: disable
          my-codename:           # a new id, local to this overlay
            op: add
            term: "ProjNova"

    ``disable`` drops a base entry; ``replace`` gives a base entry new values for the fields
    it names, each replacing the old value whole (``aka`` is not merged), keeping its
    position and id; ``add`` appends an entry after the base ones, in overlay order. The
    shape is a named-field object per entry rather than a positional ``["replace", "..."]``
    pair, because a replace changes several fields a pair cannot name, and a named field
    lets an error say which one is wrong. The YAML is read by `yamlsubset`, never PyYAML.

A MISTAKE IS AN ERROR; A BASE THAT MOVED IS A WARNING
    What the user wrote is checked when it is parsed: an unknown ``op``, a missing ``term``
    on an add, a field an op does not take, or an entry over a limit `stt dict add` would
    refuse is a `UsageError` naming the line, as a malformed personal dictionary is. What
    only the installed base can decide is a warning, never an error, because the base is
    upgraded underneath the overlay: a replace or disable whose id the base no longer has,
    a ``base_version`` that is not the installed version, an add whose id the base has since
    gained, and a name or spelling that another entry already has. "Flag first, drop
    second": every one of those is said, and none of them stops a transcription.

WHY COMPOSITION IS PURE
    `compose` takes a parsed base and a parsed overlay and reads nothing, so every rule
    above is a unit test with no file on disk; `installed` does the reading.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from . import catalog, dictionary, yamlsubset
from ._errors import UsageError
from .dictionary import MAX_DICTIONARY_BYTES, MAX_SCREENED_TERMS, Term
from .jsonio import read_text

Op = Literal["disable", "replace", "add"]
Mark = Literal["base", "replaced", "added"]
OPS: tuple[Op, ...] = ("disable", "replace", "add")
_FIELDS = ("term", "aka", "note")
_TOP = ("base_version", "entries")
_EXAMPLE = "for example `hyperide:` and under it `op: disable`"
WHY = "an overlay changes the words of every transcription, so a mistake in it is not guessed at"


@dataclass(frozen=True, slots=True)
class Change:
    """One entry of an overlay. A field left out is None: a replace keeps the base's value."""

    id: str
    op: Op
    line: int
    term: str | None = None
    aka: tuple[str, ...] | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Overlay:
    """A parsed overlay. ``origin`` names the file in every warning about it."""

    origin: str
    base_version: int
    changes: tuple[Change, ...]


@dataclass(frozen=True, slots=True)
class Composed:
    """An installed dictionary as the overlay leaves it, and an account of what it did.

    ``dictionary`` is the base with its entries replaced; its ``sha256`` is still the
    installed file's, since that is the file the entries came from.
    """

    dictionary: catalog.Distributable
    marks: dict[str, Mark]  # term id -> where the entry came from
    disabled: tuple[catalog.Entry, ...]
    warnings: tuple[str, ...]
    overlay: Overlay | None


def read(path: Path) -> Overlay | None:
    """The overlay at ``path``, parsed; None when there is none."""
    how = f"fix {path}, or move it away to use the dictionary without it"
    text = read_text(path, how=how, limit=MAX_DICTIONARY_BYTES, too_big="an overlay is a few edits")
    if text is None:
        return None
    try:
        return parse(text, origin=str(path))
    except UsageError as broken:
        raise UsageError(what=broken.what, why=broken.why, how=f"{broken.how}; {how}") from broken


def parse(text: str, *, origin: str) -> Overlay:
    """Validate an overlay's text. Everything the user wrote wrong is an error naming its line."""
    document = yamlsubset.parse(text, origin)
    if not isinstance(document, yamlsubset.Block):
        raise _invalid(origin, 1, "is not a mapping", "start with `base_version: <n>`")
    unknown = [key for key in document if key not in _TOP]
    if unknown:
        line = document.lines[unknown[0]]
        raise _invalid(
            origin, line, f"has the key {unknown[0]!r}", "the keys are base_version, entries"
        )
    if "base_version" not in document:
        raise _invalid(
            origin,
            document.line,
            "needs `base_version:`, the installed version it was written against",
            "copy the version `stt dict show <id>` prints, for example `base_version: 3`",
        )
    version = document["base_version"]
    if not isinstance(version, int) or version < 1:
        raise _invalid(
            origin,
            document.lines["base_version"],
            "has a `base_version:` that is not a whole number above 0",
            "write the number `stt dict show <id>` prints, unquoted: `base_version: 3`",
        )
    entries = document.get("entries")
    if entries is None:
        return Overlay(origin=origin, base_version=version, changes=())
    if not isinstance(entries, yamlsubset.Block):
        raise _invalid(
            origin, document.lines["entries"], "has `entries:` that is not a mapping", _EXAMPLE
        )
    if len(entries) > MAX_SCREENED_TERMS:
        raise _invalid(
            origin,
            entries.line,
            f"has {len(entries)} entries",
            f"an overlay holds at most {MAX_SCREENED_TERMS}, as a dictionary does",
        )
    changes = tuple(
        _change(ident, spec, entries.lines[ident], origin) for ident, spec in entries.items()
    )
    return Overlay(origin=origin, base_version=version, changes=changes)


def _change(ident: str, spec: object, line: int, origin: str) -> Change:
    if not catalog.is_slug(ident):
        raise _invalid(origin, line, f"has the entry id {ident!r}", catalog.SLUG_RULE)
    if not isinstance(spec, yamlsubset.Block):
        raise _invalid(origin, line, f"gives entry {ident!r} no fields", _EXAMPLE)
    where = f"entry {ident!r}"
    stray = [key for key in spec if key not in ("op", *_FIELDS)]
    if stray:
        raise _invalid(
            origin,
            spec.lines[stray[0]],
            f"gives {where} the field {stray[0]!r}",
            "the fields are op, term, aka, note",
        )
    op = spec.get("op")
    if op not in OPS:
        shown = "no op" if op is None else f"the op {op!r}"
        raise _invalid(
            origin,
            spec.lines.get("op", line),
            f"gives {where} {shown}",
            "op is disable, replace or add",
        )
    given = [key for key in _FIELDS if key in spec]
    if op == "disable" and given:
        raise _invalid(
            origin,
            spec.lines[given[0]],
            f"disables {where} and also sets {given[0]}",
            "a disable takes no fields",
        )
    if op == "replace" and not given:
        raise _invalid(
            origin, line, f"replaces {where} with nothing", "set at least one of term, aka, note"
        )
    if op == "add" and "term" not in spec:
        raise _invalid(origin, line, f"adds {where} with no term", "an add needs `term:`")
    term = _text(spec, "term", where, origin)
    note = _text(spec, "note", where, origin)
    aka = _spellings(spec, where, origin)
    change = Change(id=ident, op=op, line=line, term=term, aka=aka, note=note)
    _within_the_limits(change, spec, origin)
    return change


def _text(spec: yamlsubset.Block, key: str, where: str, origin: str) -> str | None:
    if key not in spec:
        return None
    value = spec[key]
    if not isinstance(value, str) or (key == "term" and not value):
        raise _invalid(
            origin, spec.lines[key], f"gives {where} a {key} that is not text", 'quote it: "..."'
        )
    return value


def _spellings(spec: yamlsubset.Block, where: str, origin: str) -> tuple[str, ...] | None:
    if "aka" not in spec:
        return None
    value = spec["aka"]
    spellings = (
        [alias for alias in value if isinstance(alias, str)] if isinstance(value, list) else []
    )
    if not isinstance(value, list) or len(spellings) != len(value):
        raise _invalid(
            origin,
            spec.lines["aka"],
            f"gives {where} an aka that is not a list of spellings",
            'write it as ["Vigma"], quoting anything that looks like a number',
        )
    return tuple(spellings)


def _within_the_limits(change: Change, spec: yamlsubset.Block, origin: str) -> None:
    """The checks a distributable term passes, so an overlay never holds what `add` refuses.

    A replace may name one field only; the others are checked with stand-ins, since the
    base's values were checked when it was installed.
    """
    line = spec.lines.get("term", spec.lines.get("aka", spec.lines.get("note", change.line)))
    term = Term(term=change.term or "stand-in", aka=list(change.aka or ()), note=change.note or "")
    fixed = dictionary.normalized(term)
    if fixed != term:
        # The line of the field the loader would rewrite; the stand-ins never are.
        rewritten = next(key for key in _FIELDS if getattr(fixed, key) != getattr(term, key))
        raise _invalid(
            origin,
            spec.lines.get(rewritten, line),
            f"gives entry {change.id!r} text the loader would rewrite",
            "no `,` `|` `~`, control characters, repeated or edge spaces, duplicate "
            f"spellings, or a note over {dictionary.NOTE_CHARS} characters",
        )
    try:
        dictionary.Dictionary().add(term)
    except UsageError as refused:
        if "aka" in spec and _accepted(replace(term, aka=[])):
            line = spec.lines["aka"]  # the spellings are what was refused
        # The refusal's own `how` is advice for the personal dictionary (`stt dict rm`).
        raise UsageError(
            what=f"{origin} line {line} gives entry {change.id!r} {refused.what}",
            why=refused.why,
            how="edit the entry: an overlay term must be one `stt dict add` would accept",
        ) from refused


def _accepted(term: Term) -> bool:
    try:
        dictionary.Dictionary().add(term)
    except UsageError:
        return False
    return True


def compose(base: catalog.Distributable, overlay: Overlay | None) -> Composed:
    """The base as the overlay leaves it. Pure: reads nothing, and raises nothing.

    Disables go first, so a spelling a disabled entry held is free for a replace or an add;
    then replaces, then adds, each in overlay order, each checked against the entries as
    they stand at that moment. An add whose id the base has since gained is kept — the user
    wrote it — and takes that entry's place, as a replace of every field would.
    """
    current = {entry.id: entry for entry in base.entries}
    marks: dict[str, Mark] = dict.fromkeys(current, "base")
    if overlay is None:
        return Composed(base, marks, (), (), None)
    # (line, text), so the warnings read in the file's order whichever pass raised them.
    warnings: list[tuple[int, str]] = []
    if overlay.base_version != base.version:
        stale = (
            f"{overlay.origin} was written against {base.id} v{overlay.base_version}, and "
            f"v{base.version} is installed; check its entries still fit, then set "
            f"base_version: {base.version}"
        )
        warnings.append((0, stale))
    disabled: list[catalog.Entry] = []
    for change in _only(overlay, "disable"):
        if change.id not in current:
            warnings.append(_gone(overlay, change, base))
            continue
        disabled.append(current.pop(change.id))
        del marks[change.id]
    for change in _only(overlay, "replace"):
        if change.id not in current:
            warnings.append(_gone(overlay, change, base))
            continue
        old = current[change.id].term
        wanted = Term(
            term=old.term if change.term is None else change.term,
            aka=list(old.aka if change.aka is None else change.aka),
            note=old.note if change.note is None else change.note,
        )
        _put(wanted, "replaced", change, current, marks, overlay, warnings)
    for change in _only(overlay, "add"):
        gained = change.id in current
        mark: Mark = "replaced" if gained else "added"
        wanted = Term(term=change.term or "", aka=list(change.aka or ()), note=change.note or "")
        before = len(warnings)
        if _put(wanted, mark, change, current, marks, overlay, warnings) and gained:
            # Only once it has taken the place: a skipped add already says the entry stays.
            took = (
                f"{_at(overlay, change)}: {base.id} v{base.version} now has an entry "
                f"{change.id!r} too; yours replaces it (write `op: replace` to say so)"
            )
            warnings.insert(before, (change.line, took))
    composed = replace(base, entries=tuple(current.values()))
    said = tuple(text for _, text in sorted(warnings, key=lambda warning: warning[0]))
    return Composed(composed, marks, tuple(disabled), said, overlay)


def _only(overlay: Overlay, op: Op) -> list[Change]:
    return [change for change in overlay.changes if change.op == op]


def _put(
    wanted: Term,
    mark: Mark,
    change: Change,
    current: dict[str, catalog.Entry],
    marks: dict[str, Mark],
    overlay: Overlay,
    warnings: list[tuple[int, str]],
) -> bool:
    """Store ``wanted`` under the change's id — in place when the id is there, else last.

    False when it was skipped, with the reason already among the warnings.
    """
    fitted = _fitted(wanted, change, current, overlay, warnings)
    if fitted is None:
        return False
    current[change.id] = catalog.Entry(id=change.id, term=fitted)
    marks[change.id] = mark
    return True


def _fitted(
    wanted: Term,
    change: Change,
    current: dict[str, catalog.Entry],
    overlay: Overlay,
    warnings: list[tuple[int, str]],
) -> Term | None:
    """``wanted`` as the other entries let it be, or None when its name is already taken.

    The same two rules the effective glossary applies between dictionaries: a name another
    entry already has keeps the entry out whole, and a spelling another entry already has is
    dropped from this one — otherwise the alias pattern would skip it for both.
    """
    owner: dict[str, str] = {}
    for ident, entry in current.items():
        if ident != change.id:
            for spelling in (entry.term.term, *entry.term.aka):
                owner.setdefault(spelling.casefold(), entry.term.term)
    holder = owner.get(wanted.term.casefold())
    if holder is not None:
        kept = "the base entry stays as it was" if change.id in current else "it is not added"
        skipped = (
            f"{_at(overlay, change)}: {wanted.term!r} is already {_whose(holder, wanted.term)}; "
            f"{change.id!r} is skipped and {kept}"
        )
        warnings.append((change.line, skipped))
        return None
    aka: list[str] = []
    for alias in wanted.aka:
        if alias.casefold() == wanted.term.casefold():
            # A replace that renames an entry to one of the spellings it keeps: the spelling
            # is now the name, and correcting a word to itself is not a correction lost.
            continue
        holder = owner.get(alias.casefold())
        if holder is None:
            aka.append(alias)
            continue
        dropped = (
            f"{_at(overlay, change)}: the spelling {alias!r} is already {_whose(holder, alias)}, "
            f"so it is dropped from {wanted.term!r}"
        )
        warnings.append((change.line, dropped))
    return Term(term=wanted.term, aka=aka, note=wanted.note)


def _whose(holder: str, spelling: str) -> str:
    return "an entry" if holder.casefold() == spelling.casefold() else f"a spelling of {holder!r}"


def _gone(overlay: Overlay, change: Change, base: catalog.Distributable) -> tuple[int, str]:
    return change.line, (
        f"{_at(overlay, change)}: {change.op} {change.id!r} is skipped, because "
        f"{base.id} v{base.version} has no entry with that id"
    )


def _at(overlay: Overlay, change: Change) -> str:
    return f"{overlay.origin} line {change.line}"


def _invalid(origin: str, line: int, what: str, how: str) -> UsageError:
    return UsageError(what=f"{origin} line {line} {what}", why=WHY, how=how)
