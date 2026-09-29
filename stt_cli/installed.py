"""installed — the dictionaries a user adopted, and the one glossary every consumer reads.

WHAT INSTALLING IS
    ``stt dict install medical-general`` copies one distributable dictionary, byte for byte as
    its source served it and verified against the catalog index, to
    ``<app_home>/dicts/installed/<id>.json``. stt writes and replaces that file itself, so it
    lives beside the personal ``dictionary.json`` under ``STT_HOME``, and ``stt dict
    uninstall`` deletes it. An overlay — a user's hand-written edits to an installed
    dictionary (#84) — lives under ``~/.config`` instead, where an upgrade or an uninstall
    can never touch it; this module only resolves that path, to say an overlay exists.

THE EFFECTIVE GLOSSARY
    `load_effective` is what a transcription reads instead of `dictionary.load()`: the
    personal terms first, in their order, then every installed dictionary's terms in
    ascending id order. The personal dictionary wins every conflict, and an earlier
    installed dictionary wins over a later one:

    - an installed entry whose name is already a name or a spelling in the list is dropped
      whole — unlike `Dictionary.add`, nothing from it is merged into the entry it hit;
    - an installed alias already claimed by an earlier entry is dropped from the later one,
      because `_alias_index` would otherwise skip that alias for BOTH, silently switching
      off a correction the user wrote down.

    Every drop is a warning naming both dictionaries ("flag first, drop second"). With
    nothing installed the result is the personal dictionary itself, so its digest — and the
    fingerprint of every archived run — is exactly what it was before this module existed.

WHY THE MERGE IS NOT `Dictionary.add`
    ``add`` refuses a 501st term, and three installed dictionaries can hold more than that
    together. The glossary is built as ``Dictionary(terms=...)``, as `dictionary.load()`
    builds it; past the caps the phonetic screen and the alias pattern stop, and each cap
    that is reached is a warning saying how much it left out.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import catalog, config, dictionary, portable
from ._errors import SttError, UnknownItemError, UsageError, unknown_item
from .config import Settings
from .dictionary import MAX_ALIAS_BRANCHES, MAX_DICTIONARY_BYTES, MAX_SCREENED_TERMS
from .jsonio import read_text
from .sources import DictionarySource

PERSONAL = "your dictionary"


def directory() -> Path:
    """``<app_home>/dicts/installed``: every installed dictionary, one ``<id>.json`` each."""
    return config.app_home() / "dicts" / "installed"


def overlay_path(dict_id: str) -> Path:
    """Where the user's overlay for ``dict_id`` lives, whether or not there is one.

    ``$XDG_CONFIG_HOME/stt-cli/dicts/overlays`` when that is set, ``~/.config/...``
    otherwise: the one file of the epic a person writes by hand and expects to keep.
    """
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "stt-cli" / "dicts" / "overlays" / f"{dict_id}.yaml"


def installed_ids() -> list[str]:
    """The ids of the installed dictionaries, sorted. Reads names only, never contents."""
    return sorted(path.stem for path in directory().glob("*.json"))


def read(dict_id: str) -> catalog.Distributable:
    """One installed dictionary, validated as it was on install."""
    found = _parsed(_path_of_installed(dict_id), catalog.read_tags())
    if found is None:  # uninstalled since the listing
        raise _not_installed(dict_id, installed_ids())
    return found


def read_all() -> list[catalog.Distributable]:
    """Every installed dictionary, validated, in ascending id order.

    One uninstalled between the listing and its read is left out, not an error: it is no
    longer installed, and `stt dict uninstall` in another terminal must not abort a
    transcription that is reading the glossary at that moment.
    """
    ids = installed_ids()
    if not ids:
        return []
    tags = catalog.read_tags()
    found = (_parsed(directory() / f"{dict_id}.json", tags) for dict_id in ids)
    return [adopted for adopted in found if adopted is not None]


@dataclass(frozen=True, slots=True)
class Installation:
    """What `install` did: the dictionary now installed, and the version it replaced."""

    dictionary: catalog.Distributable
    path: Path
    previous: int | None  # None when nothing was installed under that id before


def install(source: DictionarySource, dict_id: str) -> Installation:
    """Fetch, verify, validate and write one dictionary. Nothing is written on any failure."""
    index = source.index()
    entry = index.find(dict_id)
    if entry is None:
        raise _unknown(
            "dictionary", dict_id, index.ids(), "`stt dict search` lists them", "dictionaries"
        )
    blob = source.fetch(entry)
    parsed = catalog.parse(blob, origin=f"{entry.id} from {source.name}", tags=catalog.read_tags())
    served = (parsed.id, parsed.version, len(parsed.entries))
    listed = (entry.id, entry.version, entry.term_count)
    if served != listed:
        raise UsageError(
            what=f"{source.name} served {parsed.id!r} v{parsed.version} with "
            f"{len(parsed.entries)} terms for {entry.id!r} v{entry.version} with "
            f"{entry.term_count}",
            why="its catalog index and its files disagree",
            how="nothing was installed; report it to whoever runs that source",
        )
    from .archive import write_atomic

    with _locked():
        target = directory() / f"{entry.id}.json"
        previous = _version_of(target)
        # The fetched bytes themselves, so the installed file still hashes to the index's value.
        try:
            write_atomic(target, blob)
        except OSError as exc:
            raise _unwritable(f"could not write {target}", exc) from exc
    return Installation(dictionary=parsed, path=target, previous=previous)


def uninstall(dict_id: str) -> Path:
    """Delete one installed dictionary's file. Its overlay, if any, is the user's and stays."""
    with _locked():
        target = _path_of_installed(dict_id)
        try:
            target.unlink()
        except OSError as exc:
            raise _unwritable(f"could not delete {target}", exc) from exc
    return target


def load_effective(settings: Settings) -> tuple[dictionary.Dictionary, list[str]]:
    """The glossary a transcription uses, and a warning for everything the merge left out.

    With the dictionary turned off nothing is read, installed dictionaries included.
    """
    if not settings.dictionary:
        return dictionary.Dictionary(), []
    personal = dictionary.load()
    found = read_all()
    if not found:
        return personal, []
    return merge(personal, found)


def merge(
    personal: dictionary.Dictionary, dictionaries: Sequence[catalog.Distributable]
) -> tuple[dictionary.Dictionary, list[str]]:
    """The personal terms, then each installed dictionary's, earlier always winning."""
    glossary, drops = _merged(personal, dictionaries)
    return glossary, [drop.text for drop in drops] + _caps(glossary)


@dataclass(frozen=True, slots=True)
class Drop:
    """One entry or spelling the merge left out: whose it was, and who already had it."""

    source: str
    holder: str
    text: str


def drops_involving(
    dict_id: str, personal: dictionary.Dictionary, dictionaries: Sequence[catalog.Distributable]
) -> list[str]:
    """The merge warnings that name ``dict_id`` on either side, for `stt dict show`."""
    _, drops = _merged(personal, dictionaries)
    return [drop.text for drop in drops if dict_id in (drop.source, drop.holder)]


def _merged(
    personal: dictionary.Dictionary, dictionaries: Sequence[catalog.Distributable]
) -> tuple[dictionary.Dictionary, list[Drop]]:
    terms = list(personal.terms)
    owner: dict[str, str] = {}
    for term in personal.terms:
        for spelling in (term.term, *term.aka):
            owner.setdefault(spelling.casefold(), PERSONAL)
    drops: list[Drop] = []
    for source in sorted(dictionaries, key=lambda found: found.id):
        for entry in source.entries:
            term = entry.term
            holder = owner.get(term.term.casefold())
            if holder is not None:
                drops.append(
                    Drop(
                        source.id,
                        holder,
                        f"{source.id}: {term.term!r} is left out, because {holder} already "
                        "has that spelling",
                    )
                )
                continue
            kept: list[str] = []
            for alias in term.aka:
                holder = owner.get(alias.casefold())
                if holder is None:
                    kept.append(alias)
                    continue
                drops.append(
                    Drop(
                        source.id,
                        holder,
                        f"{source.id}: the spelling {alias!r} is not corrected to {term.term!r}, "
                        f"because {holder} already has that spelling",
                    )
                )
            adopted = dictionary.Term(term=term.term, aka=kept, note=term.note)
            terms.append(adopted)
            for spelling in (adopted.term, *adopted.aka):
                owner[spelling.casefold()] = source.id
    return dictionary.Dictionary(terms=terms), drops


def _caps(glossary: dictionary.Dictionary) -> list[str]:
    """Say how much of the glossary the phonetic screen and the alias pattern cannot hold."""
    warnings: list[str] = []
    unscreened = dictionary.unscreened_count(glossary.terms)
    if unscreened:
        warnings.append(
            f"{unscreened} term(s) past the first {MAX_SCREENED_TERMS} are not screened for "
            "sound-alikes; their recorded spellings are still corrected"
        )
    uncorrected = dictionary.uncorrected_alias_count(glossary.terms)
    if uncorrected:
        warnings.append(
            f"{uncorrected} recorded spelling(s) past the first "
            f"{MAX_ALIAS_BRANCHES} are not corrected"
        )
    return warnings


def _path_of_installed(dict_id: str) -> Path:
    """The file of an installed dictionary, found by listing, never by joining user text.

    ``stt dict uninstall ../../config`` must not reach ``config.json``: the id is looked up
    among the names that are there, and anything else is an unknown id.
    """
    ids = installed_ids()
    if dict_id not in ids:
        raise _not_installed(dict_id, ids)
    return directory() / f"{dict_id}.json"


def _not_installed(dict_id: str, ids: list[str]) -> UnknownItemError:
    return _unknown(
        "installed dictionary",
        dict_id,
        ids,
        "`stt dict installed` lists them",
        "installed dictionaries",
    )


def _unwritable(what: str, exc: OSError) -> UsageError:
    """A full disk or a read-only STT_HOME, said as an error rather than a traceback."""
    return UsageError(what=what, why=str(exc), how=f"make {directory()} writable and run it again")


def _parsed(path: Path, tags: dict[str, catalog.Tag]) -> catalog.Distributable | None:
    """An installed file, validated. A broken one is an error that says how to repair it.

    None when the file is gone: an uninstall ran between the directory listing and this read.
    """
    repair = f"reinstall it (`stt dict install {path.stem}`) or `stt dict uninstall {path.stem}`"
    text = read_text(
        path,
        how=repair,
        limit=MAX_DICTIONARY_BYTES,
        too_big=catalog.TOO_BIG,
    )
    if text is None:
        if not os.path.lexists(path):
            return None
        raise UsageError(what=f"could not read {path}", why="it is not a regular file", how=repair)
    try:
        parsed = catalog.parse(text.encode("utf-8"), origin=str(path), tags=tags)
    except UsageError as broken:
        raise UsageError(what=broken.what, why=broken.why, how=repair) from broken
    if parsed.id != path.stem:
        raise UsageError(
            what=f"{path} holds the dictionary {parsed.id!r}",
            why="an installed dictionary's file is named after its id",
            how=repair,
        )
    return parsed


def _version_of(path: Path) -> int | None:
    """The version installed at ``path`` before an install replaces it, if it is readable."""
    if not path.is_file():
        return None
    try:
        found = _parsed(path, catalog.read_tags())
    except SttError:
        return None  # a damaged copy is being replaced, which is the repair the error names
    return None if found is None else found.version


def _unknown(kind: str, name: str, known: list[str], listing: str, plural: str) -> UnknownItemError:
    base = unknown_item(kind, name, known, plural=plural)
    if not known:
        return UnknownItemError(what=base.what, why=f"there are no {plural}", how=listing)
    return UnknownItemError(what=base.what, why=base.why, how=f"{base.how} ({listing})")


@contextmanager
def _locked() -> Iterator[None]:
    """One writer at a time, as `dictionary.editing` does for the personal file.

    Opened as `config._editing_the_settings` opens its lock, non-blocking and then asked
    what it is: a named pipe sitting where the lock goes would otherwise hang `stt dict
    install` forever on the line meant to make it safe.
    """
    config.ensure_dirs()
    directory().mkdir(parents=True, exist_ok=True)
    lock = directory().parent / "installed.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | portable.O_NONBLOCK, 0o600)
    except OSError as exc:
        raise UsageError(
            what=f"could not lock {directory()}",
            why=str(exc),
            how=f"remove {lock} if something else is sitting in its place",
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UsageError(
                what=f"could not lock {directory()}",
                why=f"{lock} is not a regular file",
                how="move it out of the way",
            )
        portable.lock(descriptor)
        try:
            yield
        finally:
            portable.unlock(descriptor)
    finally:
        os.close(descriptor)
