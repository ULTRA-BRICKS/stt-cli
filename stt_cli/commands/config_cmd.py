"""config — read and write the stored defaults, so common flags stop being flags.

If every run of yours is ``-l ru -t absolute --tz Europe/Belgrade``, those are not options,
they are your defaults. Store them once and the command line goes back to being the file
name. A flag always still wins over a stored value for a single run.

A bare ``stt config``, typed at an interactive terminal, opens a browsable screen instead of
printing the list once — see ``_configtui.py``. Piped, redirected, or run from a script, it
prints the same list `--list`/no-TUI callers have always gotten: nothing about scripting
`stt config` changes.
"""

from __future__ import annotations

import argparse
import sys

from .. import config
from .._errors import EXIT_OK
from ..jsonio import JsonDict, as_dict

NAME = "config"
SUMMARY = "show or change stored defaults"


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="stt config", description=SUMMARY)
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("list", help="show every setting and its current value")
    sub.add_parser(
        "tui", help="open the interactive screen (what a bare `stt config` opens on a terminal)"
    )
    sub.add_parser("path", help="print the config file location")
    get = sub.add_parser("get", help="print one setting")
    get.add_argument("key")
    put = sub.add_parser("set", help="store one setting")
    put.add_argument("key")
    put.add_argument("value")
    drop = sub.add_parser("unset", help="remove a stored setting, back to its default")
    drop.add_argument("key")
    args = parser.parse_args(argv)

    want_tui = args.action == "tui" or (
        # No subcommand: the browsable screen on a real terminal, the same listing as
        # always everywhere else (a pipe, a script, `stt config | cat`, CI) otherwise.
        args.action is None and sys.stdout.isatty() and sys.stdin.isatty()
    )
    if want_tui:
        try:
            import curses  # noqa: F401 — probe only: Windows Python ships without it
        except ImportError:
            print("stt config: this Python has no curses, so here is `stt config list` instead")
            return list_settings()
        from ._configtui import launch

        return launch()
    if args.action is None or args.action == "list":
        return list_settings()
    if args.action == "path":
        print(config.config_path())
        return EXIT_OK
    if args.action == "get":
        return _get(args.key)
    if args.action == "unset":
        return _unset(args.key)
    return _set(args.key, args.value)


def list_settings() -> int:
    """`stt config list`, and the plain-text fallback `_configtui.launch` prints when the
    screen can't run (a terminal too small, or one with no TUI support at all) — public so
    that fallback imports a real function, not another module's private one."""
    settings = config.load_settings()
    stored = _stored()
    print(f"{'setting':<24} {'value':<28} source")
    for name in sorted(config.configurable()):
        value = getattr(settings, name)
        source = "config file" if name in stored else "default"
        print(f"{name:<24} {_show(value):<28} {source}")
    print(f"\nfile: {config.config_path()}")
    return EXIT_OK


def _stored() -> JsonDict:
    """What is actually in config.json, so `config list` can say which values are stored.

    Through `config` rather than opening the file here. The hand-rolled version checked the
    path with `is_file()` and then opened it, and a regular file swapped for a FIFO in that
    gap made `stt config list` block forever with no message — the one failure the shared
    reader exists to make impossible.
    """
    return as_dict(config.stored())


def _get(key: str) -> int:
    settings = config.load_settings()
    if key not in config.configurable():
        from .._errors import unknown_item

        raise unknown_item("setting", key, sorted(config.configurable()))
    print(_show(getattr(settings, key)))
    return EXIT_OK


def _set(key: str, raw: str) -> int:
    """Store one setting, coercing the argv string to the field's DECLARED type."""
    if key not in config.configurable():
        from .._errors import unknown_item

        raise unknown_item("setting", key, sorted(config.configurable()))
    value = config.coerce(key, raw)
    validate_extra(key, value)
    config.save_setting(key, value)
    print(f"{key} = {_show(value)}   ({config.config_path()})")
    return EXIT_OK


def validate_extra(key: str, value: object) -> None:
    """A handful of settings mean nothing as a bare type-check — a syntactically fine string
    can still be a hotkey chord nothing can ever press, or a mode word that isn't one. Those
    get one extra pass here, refused with the SAME clean error `stt config set` and the TUI
    already refuse a bad type with, instead of surfacing later as a raw crash inside
    whatever actually tries to use the value.

    Public — imported by ``_configtui.apply_edit`` so the TUI's edit path shares this exact
    check instead of re-implementing it against a private name (`list_settings`, just above,
    was made public for the identical reason: the TUI reuses this module's logic, not just
    its own copy of it).
    """
    if key in ("hotkey", "hotkey_mode") and value is not None:
        from ..live import hotkey as hotkey_mod

        hotkey_mod.validate(key, str(value))


def _unset(key: str) -> int:
    """Remove one stored setting, so it goes back to answering with the built-in default.

    No pre-check for an unknown key here: `config.unset_setting` raises the same
    `unknown_item` itself, as the first thing it does, before touching the file.
    """
    config.unset_setting(key)
    print(f"{key} reset to its default   ({config.config_path()})")
    return EXIT_OK


def _show(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(v) for v in value) or "(empty)"
    return "(unset)" if value is None else str(value)
