"""dict — the words a speech model has never heard of.

``stt dict add ConLoca --aka ConLog --aka Coloca`` is the whole workflow: the term goes
into the glossary the speech model is prompted with, the spellings after ``--aka`` are
corrected automatically wherever they appear, and anything else that merely SOUNDS like the
term gets flagged for the reader and for the LLM correction pass.

``stt dict check`` exists so the phonetic threshold is inspectable rather than magic: paste
a line of a transcript and see exactly what would have been flagged and how strongly.

``stt dict search`` / ``install`` / ``installed`` / ``show`` / ``uninstall`` adopt a whole
curated dictionary at once — bundled with stt, or from the marketplace website with
``--remote``. Installed terms join the personal ones in every transcription; the personal
dictionary wins every conflict, and ``stt dict list`` still shows only the personal terms.
An overlay — a hand-written ``<config>/stt-cli/dicts/overlays/<id>.yaml`` — disables,
replaces or adds entries of one installed dictionary without editing it; ``stt dict show``
says what it did.
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

from .. import dictionary, installed, portable, sources
from .._errors import EXIT_OK, EXIT_UNKNOWN_ITEM, UsageError

# How far below the flagging threshold `stt dict check` still reports a resemblance.
NEAR_MISS_FLOOR = 0.5
NEAR_MISS_LIMIT = 20

NAME = "dict"
SUMMARY = "terms the speech model does not know, and their known misspellings"


OVERLAY_HELP = """\
overlays:
  Customize an installed dictionary without editing it, so an upgrade still reaches you:
  write ~/.config/stt-cli/dicts/overlays/<id>.yaml (under $XDG_CONFIG_HOME when that is
  an absolute path; `stt dict show <id>` prints the exact path and the ids to name entries by):

    base_version: 3          # the installed version you wrote it against
    entries:
      conloca:
        op: replace          # new values for the fields you give; the rest stay
        aka: ["ConLog", "our-nickname"]
      hyperide:
        op: disable          # drop the entry
      my-codename:
        op: add              # a new entry, after the dictionary's own
        term: "ProjNova"
        note: "internal codename"

  The file is a small subset of YAML: `key: value` lines indented with spaces, quoted or
  plain text, ["flow", "lists"] or `- item` lines, and # comments. A mistake in it is an
  error naming the line; an id the dictionary no longer has is only a warning.
"""


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="stt dict",
        description=SUMMARY,
        epilog=OVERLAY_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("list", help="show every term (default)")

    add = sub.add_parser("add", help="add or extend a term")
    add.add_argument("term")
    add.add_argument("--aka", action="append", default=[], help="a spelling to correct; repeatable")
    add.add_argument("--note", default="", help="what it is — goes to the LLM, not the decoder")

    remove = sub.add_parser("rm", help="remove a term")
    remove.add_argument("term")

    load = sub.add_parser("import", help="add terms from a file, one per line")
    load.add_argument("file")

    check = sub.add_parser("check", help="show what a line of text would match")
    check.add_argument("text", nargs="+")

    remote_help = "ask the marketplace website instead of the dictionaries bundled with stt"
    search = sub.add_parser("search", help="find a curated dictionary to install")
    search.add_argument("query", nargs="?", default="", help="part of an id, title or tag")
    search.add_argument("--tag", help="only dictionaries carrying this tag")
    search.add_argument("--remote", action="store_true", help=remote_help)

    adopt = sub.add_parser("install", help="install (or upgrade) a curated dictionary")
    adopt.add_argument("id")
    adopt.add_argument("--remote", action="store_true", help=remote_help)

    sub.add_parser("installed", help="list the installed dictionaries")

    show = sub.add_parser(
        "show", help="the terms of one installed dictionary, and what its overlay changed"
    )
    show.add_argument("id")

    drop = sub.add_parser("uninstall", help="remove an installed dictionary")
    drop.add_argument("id")

    args = parser.parse_args(argv)
    action = args.action or "list"
    return _ACTIONS[action](args)


def _list(_args: argparse.Namespace) -> int:
    terms = dictionary.load()
    if not terms:
        print("the dictionary is empty")
        print("  add one:  stt dict add ConLoca --aka ConLog --aka Coloca")
        return EXIT_OK
    for term in terms.terms:
        print(term.describe())
    print(f"\n{len(terms.terms)} term(s) in {dictionary.path()}")
    print(f"prompt given to the speech model: {terms.prompt() or '(none — all over budget)'}")
    return EXIT_OK


def _add(args: argparse.Namespace) -> int:
    entry = dictionary.normalized(
        dictionary.Term(term=args.term, aka=list(args.aka), note=args.note)
    )
    # Validated AFTER normalizing, not before: `stt dict add ","` passed the old check —
    # a comma is not whitespace — and then normalization emptied it, so the command
    # reported success and stored a term that `load()` silently dropped again.
    if not entry.term:
        raise UsageError(
            what="a term cannot be empty",
            why=f"nothing usable was left of {args.term!r}",
            how="stt dict add ConLoca --aka ConLog",
        )
    with dictionary.editing() as terms:
        new = terms.add(entry)
        # `find(entry.term)`, not `find(args.term)`: the stored term is stripped and has its
        # commas removed, so looking it up by what the user typed misses and used to crash
        # on `.describe()`.
        stored = terms.find(entry.term) or entry
    print(f"{'added' if new else 'updated'}: {stored.describe()}")
    print(f"saved to {dictionary.path()}")
    _warn_if_over_budget(terms)
    return EXIT_OK


def _remove(args: argparse.Namespace) -> int:
    # Normalized like the write path: `dict add "Foo, Inc"` stores `Foo Inc`, so removing it
    # by what the user typed looked the term up under a name that is never on disk.
    wanted = dictionary.normalized(dictionary.Term(term=args.term)).term
    with dictionary.editing() as terms:
        removed = terms.remove(wanted)
    if not removed:
        print(f"no such term: {args.term}")
        return EXIT_UNKNOWN_ITEM
    print(f"removed: {args.term}")
    return EXIT_OK


def _import(args: argparse.Namespace) -> int:
    """Read ``Term = alias, alias  # note`` lines. The format is deliberately the one people
    already write glossaries in, so an existing list can be pasted in unedited."""
    source = Path(args.file).expanduser()
    if not source.is_file():
        raise UsageError(
            what=f"no such file: {source}",
            why="stt dict import needs a text file with one term per line",
            how="write lines like `ConLoca = ConLog, Coloca  # the project`",
        )
    lines = _readable_lines(source)
    added = 0
    with dictionary.editing() as terms:
        parsed = [entry for entry in map(_parse_line, lines) if entry is not None]
        # Counted before anything is added, because the transaction is all-or-nothing: hit
        # the cap halfway through and nothing at all is written, while the error from the
        # add path says "remove the entries you no longer need" — of which there are none,
        # since the import did not happen. Said up front, it is a fact about the file.
        room = dictionary.MAX_SCREENED_TERMS - len(terms.terms)
        # Only the entries that would take a NEW slot. A line for a term already in the
        # dictionary merges its aliases and consumes nothing — `Dictionary.add` exempts it
        # from the cap for that reason — so counting raw lines refused a full dictionary the
        # right to import a single alias, and refused a fresh one a file that repeats the
        # same name five hundred times to store one term.
        # Normalized first, because that is what `add` will store: a line reading `Foo, Inc`
        # becomes the term `Foo Inc`, and counting the raw text would count a name that is
        # already there as a newcomer.
        arriving = {dictionary.normalized(entry).term.casefold() for entry in parsed}
        newcomers = {name for name in arriving if terms.find(name) is None}
        if len(newcomers) > room:
            raise UsageError(
                what=f"{source} has {len(newcomers)} new term(s) and only {room} would fit",
                why=f"a dictionary holds {dictionary.MAX_SCREENED_TERMS} terms, and every one "
                "of them is compared against every word run of every segment",
                how="split the file, or trim it to the names that actually turn up",
            )
        for entry in parsed:
            if terms.add(entry):
                added += 1
    print(f"imported {added} new term(s); {len(terms.terms)} in the dictionary")
    _warn_if_over_budget(terms)
    return EXIT_OK


# A glossary is one line per name. A file far past this is not one, and reading it whole to
# discover that is the trap: the read happened while the dictionary lock was held, so an
# enormous file exhausted memory with every other `stt dict` writer waiting behind it.
MAX_IMPORT_BYTES = 4 * 1024 * 1024


def _readable_lines(source: Path) -> list[str]:
    """The file's lines, read BEFORE the lock is taken and bounded on the way in.

    Three failures, one shape: an oversized file, a file that is not text at all (an
    uncaught `UnicodeDecodeError`, which is not an `SttError`, so it came out as a raw
    traceback), and a path swapped for a FIFO after the `is_file()` check — which blocked
    forever, holding the dictionary lock the whole time. Reading first means the lock is
    only ever held around work that cannot block on somebody else's file.
    """
    try:
        # Opened without blocking and checked through the DESCRIPTOR, not the path: a path
        # that is a regular file when it is measured can be a FIFO by the time it is opened,
        # and opening one waits for a writer that never comes — forever, with no message.
        # `os.open` with O_NONBLOCK returns instead of waiting, and the check then asks the
        # file that was actually opened rather than whatever the name points at now.
        fd = os.open(source, os.O_RDONLY | portable.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise UsageError(
                    what=f"{source} is not a regular file",
                    why="stt dict import reads a text file, not a device or a pipe",
                    how="write lines like `ConLoca = ConLog, Coloca  # the project`",
                )
            if os.fstat(handle.fileno()).st_size > MAX_IMPORT_BYTES:
                raise UsageError(
                    what=f"{source} is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MiB",
                    why="a glossary is one term per line, not a corpus",
                    how="import the list of names, not the document they came from",
                )
            return handle.read(MAX_IMPORT_BYTES).splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(
            what=f"cannot read {source}",
            why=str(exc),
            how="stt dict import needs a UTF-8 text file with one term per line",
        ) from exc


def _parse_line(line: str) -> dictionary.Term | None:
    body, _, note = line.partition("#")
    name, _, aliases = body.partition("=")
    name = name.strip()
    if not name:
        return None
    return dictionary.Term(
        term=name,
        aka=[part.strip() for part in aliases.split(",") if part.strip()],
        note=note.strip(),
    )


def _check(args: argparse.Namespace) -> int:
    """Show what a line would match, by running the screen the pipeline actually runs.

    This used to score each whitespace-split word against each term directly. That is a
    different answer from the pipeline's in both directions — it flagged the term itself,
    which the pipeline never does, and it said nothing about "hyper ide", which the pipeline
    flags — so the command meant to explain the threshold was explaining something else.
    For the same reason it reads the glossary the pipeline reads: the effective one, with
    the installed dictionaries in it, and none at all when the dictionary is turned off.
    """
    from .. import config

    settings = config.load_settings()
    # Validated BEFORE the empty-dictionary shortcut, because the pipeline validates
    # unconditionally: with a broken `dict_similarity` in the config and no terms yet, this
    # command answered "nothing is wrong" while `stt rec.m4a` refused to start. The command
    # that exists to explain the threshold has to refuse exactly what the pipeline refuses.
    threshold = settings.dict_similarity
    dictionary.validate_similarity(threshold)
    if not settings.dictionary:
        print("the dictionary is turned off in the settings — nothing to match against")
        return EXIT_OK
    terms, _ = installed.load_effective(settings)
    if not terms:
        print("the dictionary is empty — nothing to match against")
        return EXIT_OK
    text = " ".join(args.text)
    flagged = dictionary.screen(text, terms, similarity=threshold)
    # The near misses are the useful half of the answer: they are what a lower
    # --dict-similarity would start catching. Scored below the bar, they never reach a
    # transcript, so they are shown separately rather than mixed in with the flags.
    near = _near_misses(text, terms, threshold)

    if not flagged and not near:
        print("nothing in that line resembles a term in the dictionary")
        return EXIT_OK
    print(f"{'phrase':<24} {'term':<20} similarity  (flagged at >= {threshold:.2f})")
    for hit in flagged:
        print(f"{hit.phrase:<24} {hit.term:<20} {hit.score:>10.2f}  FLAG")
    for hit in near:
        print(f"{hit.phrase:<24} {hit.term:<20} {hit.score:>10.2f}")
    return EXIT_OK


def _near_misses(text: str, terms: dictionary.Dictionary, threshold: float) -> list[dictionary.Hit]:
    """What the screen would catch at a lower threshold, minus what it already caught."""
    floor = min(threshold, NEAR_MISS_FLOOR)
    already = {hit.phrase for hit in dictionary.screen(text, terms, similarity=threshold)}
    below = dictionary.screen(text, terms, similarity=floor)
    return sorted((hit for hit in below if hit.phrase not in already), key=lambda hit: -hit.score)[
        :NEAR_MISS_LIMIT
    ]


def _warn_if_over_budget(terms: dictionary.Dictionary) -> None:
    """Say so when a term will not reach the speech model.

    The prompt has a hard budget and whisper truncates an over-long one from the front, so
    a big dictionary silently stops biasing the decoder for its later entries. Better to say
    which half is working than to let someone wonder why a term never gets spelled right.
    """
    carried = terms.prompt().count(",") + 1 if terms.prompt() else 0
    if carried < len(terms.terms):
        print(
            f"note: only the first {carried} term(s) fit in the speech model's prompt; "
            f"the rest are still used for corrections and by the LLM pass"
        )


def _source(args: argparse.Namespace) -> sources.DictionarySource:
    return sources.RemoteSource() if args.remote else sources.BundledSource()


def _search(args: argparse.Namespace) -> int:
    source = _source(args)
    index = source.index()
    found = [
        entry
        for entry in index.entries
        if entry.matches(args.query) and (args.tag is None or args.tag.casefold() in entry.tags)
    ]
    if not found:
        asked = [repr(args.query)] if args.query else []
        asked += [f"--tag {args.tag}"] if args.tag else []
        print(f"no dictionary in {source.name} matches {' '.join(asked) or 'anything'}")
        print("  every dictionary:  stt dict search" + (" --remote" if args.remote else ""))
        return EXIT_OK
    have = set(installed.installed_ids())
    width = max(len(entry.id) for entry in found)
    for entry in found:
        mark = "*" if entry.id in have else " "
        print(
            f"{mark} {entry.id:<{width}}  v{entry.version:<3} {entry.term_count:>4} terms  "
            f"{','.join(entry.languages):<6} {entry.title}  [{', '.join(entry.tags)}]"
        )
    print(f"\n{len(found)} of {len(index.entries)} dictionaries in {source.name}; * = installed")
    print(f"install one:  stt dict install <id>{' --remote' if args.remote else ''}")
    return EXIT_OK


def _install(args: argparse.Namespace) -> int:
    done = installed.install(_source(args), args.id)
    adopted = done.dictionary
    size = f"{len(adopted.entries)} terms"
    if done.previous is None:
        print(f"installed {adopted.id} v{adopted.version} ({size}): {adopted.title}")
    elif done.previous == adopted.version:
        print(f"reinstalled {adopted.id} v{adopted.version} ({size}), already the latest version")
    elif done.previous < adopted.version:
        print(f"upgraded {adopted.id} from v{done.previous} to v{adopted.version} ({size})")
    else:
        # The other source is behind: `install` without --remote after a --remote install.
        print(f"replaced {adopted.id} v{done.previous} with the older v{adopted.version} ({size})")
    print(f"  {done.path}")
    print(f"  its terms:  stt dict show {adopted.id}")
    overlay = installed.overlay_path(adopted.id)
    if installed.has_overlay(adopted.id):
        print(f"warning: your overlay {overlay} customizes this dictionary; check it still fits")
        # The install has happened by now, so a broken overlay is said, not a failed install:
        # a script re-running `install` to upgrade must not read a failure.
        try:
            said = list(installed.with_overlay(adopted).warnings)
        except UsageError as broken:
            said = [f"{broken.what}; {broken.how}"]
        for warning in said:
            print(f"warning: {warning}")
    return EXIT_OK


def _installed(_args: argparse.Namespace) -> int:
    from .. import config

    found = installed.read_all()
    composed, warnings = installed.overlaid(found)
    if not found:
        print("no dictionaries are installed")
        print("  find one:  stt dict search")
        for warning in warnings:
            print(f"warning: {warning}")
        return EXIT_OK
    width = max(len(adopted.id) for adopted in found)
    for each in composed:
        adopted = each.dictionary
        extra = f"  overlay: {installed.overlay_path(adopted.id)}" if each.overlay else ""
        print(
            f"{adopted.id:<{width}}  v{adopted.version:<3} {len(adopted.entries):>4} terms"
            f"  {adopted.title}{extra}"
        )
    print(f"\n{len(found)} installed in {installed.directory()}")
    if not config.load_settings().dictionary:
        print("note: the dictionary is turned off in the settings, so none of these is used")
    _, dropped = installed.merge(dictionary.load(), [each.dictionary for each in composed])
    for warning in warnings + dropped:
        print(f"warning: {warning}")
    return EXIT_OK


def _show(args: argparse.Namespace) -> int:
    """The effective terms, each marked base, replaced or added, and what was disabled."""
    shown, merged, unread = installed.composed_for_show(installed.read_all(), args.id)
    base = shown.dictionary
    print(f"{base.id} v{base.version}: {base.title}")
    print(f"  tags: {', '.join(base.tags)}   languages: {', '.join(base.languages)}")
    print(f"  license: {base.license}   source: {base.source}")
    overlay = installed.overlay_path(base.id)
    if shown.overlay is None:
        print(f"  overlay: none (write {overlay} to customize it; `stt dict --help` shows how)")
        stray = overlay.with_suffix(".yml")
        if stray.is_file():
            print(f"warning: {installed.misnamed(stray)}")
    else:
        print(f"  overlay: {overlay} (written against v{shown.overlay.base_version})")
    print()
    width = max(len(entry.id) for entry in (*shown.dictionary.entries, *shown.disabled))
    for entry in shown.dictionary.entries:
        mark = shown.marks[entry.id]
        print(f"  {mark:<8}  {entry.id:<{width}}  {entry.term.describe()}")
    if shown.disabled:
        print("\ndisabled by the overlay:")
        for entry in shown.disabled:
            print(f"  {entry.id:<{width}}  {entry.term.describe()}")
    print(
        f"\n{len(shown.dictionary.entries)} term(s) from "
        f"{installed.directory() / (base.id + '.json')}"
        + (f", {len(shown.disabled)} disabled" if shown.disabled else "")
    )
    for warning in shown.warnings:
        print(f"warning: {warning}")
    for warning in unread + installed.drops_involving(base.id, dictionary.load(), merged):
        print(f"warning: {warning}")
    return EXIT_OK


def _uninstall(args: argparse.Namespace) -> int:
    gone = installed.uninstall(args.id)
    print(f"uninstalled {args.id} ({gone})")
    overlay = installed.overlay_path(args.id)
    if installed.has_overlay(args.id):
        print(f"your overlay stays at {overlay}; delete it yourself if you no longer want it")
    return EXIT_OK


_ACTIONS = {
    "list": _list,
    "add": _add,
    "rm": _remove,
    "import": _import,
    "check": _check,
    "search": _search,
    "install": _install,
    "installed": _installed,
    "show": _show,
    "uninstall": _uninstall,
}
