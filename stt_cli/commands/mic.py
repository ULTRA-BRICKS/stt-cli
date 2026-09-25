"""mic — dictate into whatever window has focus, correcting as it goes.

``stt mic`` opens the microphone, and from then on what you say is typed where you are
already typing. Two models run at once: a small one answers immediately so words appear
while you are still speaking, and `large-v3-turbo` answers a second later and replaces them
with the version that has the proper nouns and the punctuation right. Your terminology
dictionary reaches both, so a project name comes out spelled the way you wrote it down.

Press any key yourself and stt stops correcting the sentence in flight and leaves it alone —
it will never delete a character it did not type. Escape twice ends the session.

A bare `stt mic` (no subcommand) is the ad-hoc path above: dictate right now, in this shell,
until you stop it. `run`/`start`/`status`/`stop`/`enable`/`disable` are a SEPARATE, newer way
to use the same dictation: a background daemon that loads both models once and then idles
with the microphone CLOSED until a global hotkey opens it for one burst at a time — see
`stt mic --help` for the ad-hoc flags and `stt mic run --help` for the daemon's.

WHAT IT NEEDS THAT NOTHING ELSE IN STT NEEDS
    Accessibility and Microphone, both granted to the terminal application rather than to
    stt, because that is the level macOS grants them at.

WHEN NOTHING HAPPENS
    `stt mic --check` is the answer, and it is the one command here that exists because of a
    real failure rather than a design. Dictation can fail by producing nothing at all — no
    text, no error, no exit code — when every frame the microphone delivers is below the bar
    the detector sets. From outside, that is indistinguishable from a denied permission, the
    wrong device being open, or nobody having spoken. So `--check` opens the microphone,
    draws what it hears against the bar it has to clear, and names which of those it was.
    It types nothing anywhere; it is safe to run in the middle of anything.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from typing import TYPE_CHECKING

from .._errors import EXIT_ENGINE, EXIT_MISSING_DEP, EXIT_OK, EXIT_PERMISSION, SttError
from ..live import hotkey as hotkey_mod
from ..live.mic_service import SERVE_TOKEN

if TYPE_CHECKING:
    from agenttools_service import ServiceManager

NAME = "mic"
SUMMARY = "dictate into the focused window, corrected live"

# The small model exists to be fast, not right: whatever it says is replaced within a second
# or two. `base` is the smallest one that produces recognisable Russian.
DEFAULT_DRAFT = "base"

# The words that mean "manage the background daemon" rather than "dictate now, ad-hoc". None
# of the ad-hoc flags below start with one of these bare words, so the split is unambiguous.
_LIFECYCLE = ("run", "start", "stop", "status", "enable", "disable")

# `agenttools_service.run_action`'s own convention (its docstring: "3 when status/stop find
# nothing running") — `_status` returns it too when IT is the one deciding nothing is
# running, so a script branching on `stt mic status`'s exit code sees one convention either
# way, not a different number depending on which code path answered.
_EXIT_NOT_RUNNING = 3


def run(argv: list[str]) -> int:
    if argv and argv[0] == SERVE_TOKEN:
        return _serve(argv[1:])
    if argv and argv[0] in _LIFECYCLE:
        return _lifecycle(argv)
    args = _parse(argv)
    # Listing first. `--check` was tested first and `--list-devices` is handled further in,
    # so `stt mic --check --list-devices` spent six seconds listening instead of printing the
    # list — the cheaper and more certain of the two answers losing to the slower one.
    if args.list_devices:
        return asyncio.run(_list_devices())
    if args.check:
        return asyncio.run(_check(args))
    from ..live import dictation

    return asyncio.run(dictation.dictate(args))


def _add_dictation_flags(parser: argparse.ArgumentParser, *, daemon: bool) -> None:
    """The flags common to the ad-hoc path and the daemon: one place a default or a help
    string is written, instead of the two places `_parse`/`_daemon_parser` used to keep in
    sync by hand. ``daemon`` only changes the `--idle-minutes` wording (a "session" versus
    a "burst") — everything else is identical."""
    parser.add_argument("--device", help="microphone index or part of its name")
    parser.add_argument("--model", help="the accurate model (default: your configured one)")
    parser.add_argument(
        "--draft-model",
        default=DEFAULT_DRAFT,
        help=f"the fast model typed first (default: {DEFAULT_DRAFT})",
    )
    parser.add_argument(
        "--draft",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="type a fast draft before the accurate answer (default: yes)",
    )
    parser.add_argument("-l", "--language", help="spoken language (default: your configured one)")
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=None,
        help="threads per model (default: your configured one; 0 lets the engine choose)",
    )
    idle_help = (
        "end a burst after this long with nobody speaking (0 to never)"
        if daemon
        else "stop after this long with nobody speaking (0 to never stop)"
    )
    parser.add_argument("--idle-minutes", type=_minutes, default=None, help=idle_help)
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="no status line, sounds or notifications"
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stt mic",
        description=SUMMARY,
        epilog=(
            "types twice per sentence: a fast draft model answers first so words appear "
            "while you are still speaking, then the accurate model replaces them a moment "
            "later with punctuation and proper nouns corrected. any key you press yourself "
            "stops that second pass on the sentence in flight; press Escape twice to finish. "
            "needs Accessibility and Microphone granted to the TERMINAL running stt (not "
            "stt itself) — System Settings > Privacy & Security. if nothing appears at all, "
            "`stt mic --check` shows what the microphone actually hears against the bar it "
            "has to clear. run/start/status/stop/enable/disable run the same dictation as a "
            "background daemon behind a hotkey instead — see `stt mic run --help`"
        ),
    )
    _add_dictation_flags(parser, daemon=False)
    parser.add_argument(
        "--check",
        action="store_true",
        help="open the microphone, show what it hears, and say whether that is enough",
    )
    parser.add_argument("--list-devices", action="store_true", help="show microphones and exit")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------------------
# The daemon: `run`/`start`/`status`/`stop`/`enable`/`disable`, via the shared
# `agenttools_service` lib — the same one review-cli's dashboard and spec-web use, not
# hand-rolled here. A bare `stt mic` above is completely unaffected by any of this.
# ---------------------------------------------------------------------------------------

_MISSING_LIB_MSG = (
    "[stt mic] the managed-service subcommands need the shared 'agenttools_service' lib, "
    "which isn't installed.\n"
    "  why:  run/start/status/stop/enable/disable come from one shared service-manager "
    "(agent-tools/lib/agenttools_service) — the same one review-cli's dashboard and spec-web "
    "use — instead of a per-tool copy.\n"
    "  fix:  pip install -e <agent-tools>/lib/agenttools_daemon "
    "-e <agent-tools>/lib/agenttools_service\n"
    "  note: `stt mic run` still works without it, and a bare `stt mic` (ad-hoc, no "
    "subcommand) was never affected."
)


def _daemon_parser() -> argparse.ArgumentParser:
    """The flags the daemon itself takes — shared between `_lifecycle` (as top-level options
    on `run`/`start`/`status`/`stop`/`enable`/`disable`) and the hidden `__serve` entry those
    actions ultimately run or detach."""
    parser = argparse.ArgumentParser(
        prog="stt mic",
        description=f"{SUMMARY} — as a managed background daemon",
        epilog=(
            "run/start load both models once, then idle with the microphone CLOSED; the "
            "hotkey opens it for one burst at a time — a fast draft model answers first, "
            "then the accurate one retypes it corrected, same two-pass dictation as the "
            "ad-hoc path (see `stt mic --help`). any key you press yourself during a burst "
            "stops that second pass on the sentence in flight; Escape twice, the hotkey "
            "again (toggle mode), or releasing it (hold mode) ends that burst. "
            "`stt mic stop` ends the daemon itself. needs Accessibility and Microphone: "
            "granted to the TERMINAL for `run` (foreground, this shell) — but `start`/"
            "`enable` detach into a launchd-spawned process with its OWN grant, separate "
            "from any terminal's, so `stt mic status` names that process specifically if "
            "it is what is missing the grant. System Settings > Privacy & Security. the "
            "pidfile lives at $XDG_STATE_HOME/stt/mic.pid (default "
            "~/.local/state/stt/mic.pid), the log at $XDG_CACHE_HOME/stt/mic.log (default "
            "~/.cache/stt/mic.log) — the first place to look if `stt mic status` says it is "
            "enabled but dictation never actually starts."
        ),
    )
    _add_dictation_flags(parser, daemon=True)
    parser.add_argument(
        "--hotkey",
        type=_hotkey_spec,
        default=None,
        help=f"the chord that starts/stops dictation (default: {hotkey_mod.DEFAULT_HOTKEY}; "
        "also settable with `stt config set hotkey ...`)",
    )
    parser.add_argument(
        "--hotkey-mode",
        choices=hotkey_mod.VALID_MODES,
        default=None,
        help="toggle: press once to start, again to stop. hold: dictate only while held "
        f"(push-to-talk). (default: {hotkey_mod.DEFAULT_MODE})",
    )
    return parser


def _lifecycle(argv: list[str]) -> int:
    """`stt mic {run,start,status,stop,enable,disable}` — the daemon as a MANAGED service.

      run      run the daemon in the FOREGROUND (this shell), blocking.
      start    start it in the BACKGROUND (detached); return immediately.
      status   is it running? pid / autostart-enabled.
      stop     stop the background instance.
      enable   install OS autostart (launchd / systemd --user) AND start now.
      disable  remove OS autostart AND stop.

    A bare `stt mic` (no lifecycle word) is UNCHANGED by any of this: it still dictates
    immediately in this shell, ad-hoc, the way it always has. Everything here (the shared
    service lib, the daemon's own flags) is imported lazily, so the ad-hoc path never pays
    for it.
    """
    try:
        from agenttools_service import add_service_subcommands, dispatch
    except ImportError:
        return _lifecycle_without_lib(argv)

    parser = _daemon_parser()
    subs = parser.add_subparsers(dest="action")
    # `ns` is bound to a real (if not yet populated) `Namespace` from this first line, not
    # merely annotated — `manager_factory` below closes over it before it holds anything,
    # and reading a name a closure captures before its enclosing function ever assigns it
    # is exactly the "bare-annotated before assignment" shape #48 flagged here. Passing this
    # same object to `parse_args(namespace=...)` has it populate `ns` IN PLACE instead of
    # building a new one and rebinding the name, so there is only ever one assignment.
    ns = argparse.Namespace()
    add_service_subcommands(subs, manager_factory=lambda: _manager(ns), service_name="mic")
    parser.parse_args(_reordered_for_argparse(argv), namespace=ns)

    def _print_help_ok() -> int:
        parser.print_help()
        return 0

    if ns.action == "status":
        # The one action the shared lib cannot answer correctly for an OS-managed
        # (`enable`d) daemon — see `_status` and ticket #54.
        return _status(ns)
    return int(dispatch(ns, on_no_subcommand=_print_help_ok))


def _reordered_for_argparse(argv: list[str]) -> list[str]:
    """`stt mic start --hotkey X` — the lifecycle word first, its flags after, the only order
    every example in this file's own help text and the README shows — does not parse as
    written. `_daemon_parser`'s flags live on the PARENT parser, but once `argv[0]` (the
    lifecycle word — guaranteed present and first; `run()` only reaches `_lifecycle` when it
    is) is consumed as the subcommand, argparse hands everything AFTER it to that
    subcommand's OWN subparser, which `add_service_subcommands` builds with no flags of its
    own — so `--hotkey X` there is reported as unrecognized. Moving the lifecycle word to the
    end lets the flags reach the parser that actually declares them, while `argv[0]` is still
    what `run()` looks at to decide `_lifecycle` should run at all — the user-facing order
    never changes, only what this function hands to `argparse.parse_args`.
    """
    if not argv:
        return argv
    return [*argv[1:], argv[0]]


def _lifecycle_without_lib(argv: list[str]) -> int:
    """Fallback when `agenttools_service` is not installed: `run` still works (it never
    needed the lib, only the OS-lifecycle actions do); everything else names what to install.
    """
    action = argv[0]
    if action == "run":
        return _serve(argv[1:])
    print(_MISSING_LIB_MSG, file=sys.stderr)
    return EXIT_MISSING_DEP


def _manager(ns: argparse.Namespace) -> ServiceManager:
    """Build the `ServiceManager` for THIS invocation's flags (lazy: constructed only when an
    action actually runs, per the shared lib's `manager_factory` contract)."""
    from agenttools_service import ServiceManager

    from ..live.mic_service import mic_service

    return ServiceManager(mic_service(extra_argv=_daemon_extra_argv(ns)))


def _status(ns: argparse.Namespace) -> int:
    """`stt mic status`, but naming the real state instead of a bare "running" (#54).

    `ServiceManager.status()` cannot distinguish a genuinely alive daemon from a launchd
    job that is merely REGISTERED for an OS-managed (`enable`d) service: there is no
    pidfile to probe there, only "is the unit installed" — its own docstring calls this
    "deliberately out of scope here; use the OS's own tools for authoritative OS-managed
    liveness." `mic_service.probe_launchd` is that OS tool; this is where its answer
    reaches the CLI. Every other state (a pidfile-tracked instance, a genuinely stopped
    one, a foreign listener) is already reported correctly, so those fall straight through
    to the shared lib's own formatting.
    """
    from agenttools_service import run_action

    from ..live import mic_service

    manager = _manager(ns)
    # `run_action` re-derives `ServiceManager.status()` internally (no way to hand it a
    # `ServiceStatus` already in hand) — a second pidfile/plist read on the fall-through
    # paths below, not a bug: cheap, and the alternative is reimplementing its formatting.
    st = manager.status()
    if st.state != "autostart":
        return int(run_action(manager, "status"))

    probe = mic_service.probe_launchd(manager.service.label)
    if probe is None:
        # launchctl itself unreachable (or off macOS): no better answer than the shared
        # lib's own OS-managed-is-presumed-running read.
        return int(run_action(manager, "status"))
    if probe.alive:
        # The shared lib's own status() has no pid here (an OS-managed instance has no
        # pidfile to read one from) — launchctl just gave us a better answer than
        # `run_action` would print, so use it instead of discarding it.
        print(f"mic: running (pid {probe.pid}) [autostart on]")
        return EXIT_OK

    # Registered and enabled, but launchd has no live process for it right now — exactly
    # the crash-loop #54 was filed from. The reason, when known, comes from the DAEMON's
    # own diagnosed exit code — NOT `quartz.trusted()` of this CLI process: a launchd job
    # execs a different binary under a different TCC identity than the terminal `status`
    # runs in, so a terminal already granted Accessibility (the common case: ad-hoc `stt
    # mic` already works, which is why someone ran `enable` at all) would make
    # `quartz.trusted()` here answer for the WRONG process and miss the diagnosis
    # entirely. `require_accessibility` (`live/quartz.py`) is the only place anywhere
    # `dictate_daemon` can reach that raises `PermissionDeniedError`, so the daemon's own
    # `last exit code == EXIT_PERMISSION` is unambiguous evidence about THAT process.
    exit_note = "" if probe.last_exit_code is None else f" (last exit code {probe.last_exit_code})"
    if probe.last_exit_code == EXIT_PERMISSION:
        print(
            f"mic: not running — crash-looping{exit_note}: the daemon exited denied Accessibility"
        )
        print(
            "     fix: grant Accessibility to the process launchd runs for the daemon "
            "(its python interpreter, or bin/stt — add it manually in System Settings > "
            "Privacy & Security > Accessibility if not listed; the terminal `stt mic "
            "status` runs in does not cover this separate, launchd-spawned process), "
            "then `stt mic disable && stt mic enable`"
        )
        return EXIT_PERMISSION
    print(f"mic: not running — enabled, but currently down between restarts{exit_note}")
    print(f"     check the log: {manager.service.logfile}")
    return _EXIT_NOT_RUNNING


def _daemon_extra_argv(ns: argparse.Namespace) -> list[str]:
    """The parsed daemon flags, rendered back to CLI tokens for the detached `__serve`
    process — one owner (this function) for what a daemon flag looks like on the wire."""
    argv: list[str] = []
    if ns.device is not None:
        argv += ["--device", str(ns.device)]
    if ns.model is not None:
        argv += ["--model", ns.model]
    argv += ["--draft-model", ns.draft_model]
    if not ns.draft:
        argv += ["--no-draft"]
    if ns.language is not None:
        argv += ["--language", ns.language]
    if ns.threads is not None:
        argv += ["--threads", str(ns.threads)]
    if ns.idle_minutes is not None:
        argv += ["--idle-minutes", str(ns.idle_minutes)]
    if ns.hotkey is not None:
        argv += ["--hotkey", ns.hotkey]
    if ns.hotkey_mode is not None:
        argv += ["--hotkey-mode", ns.hotkey_mode]
    if ns.quiet:
        argv += ["--quiet"]
    return argv


def _serve(rest: list[str]) -> int:
    """Hidden entry: `stt mic __serve` — run the daemon, blocking, in THIS process.

    What `run` runs directly and what `start`/`enable` detach into the background. Reachable
    without the shared service lib (see `_lifecycle_without_lib`): a host that HAS the lib can
    `start` a daemon whose detached child still lands here regardless.
    """
    args = _daemon_parser().parse_args(rest)
    from ..live import dictation

    asyncio.run(dictation.dictate_daemon(args))
    return EXIT_OK


def _hotkey_spec(given: str) -> str:
    """`--hotkey` refusing a spec `hotkey.parse` cannot use, at parse time, not at daemon
    startup. `stt mic enable` bakes the argv this validates into a launchd `LaunchAgent`
    with `KeepAlive`; an unreachable `--hotkey` that only failed once the daemon actually
    started would have crash-looped it — every restart reloading both models before dying
    on the same invalid spec.
    """
    try:
        hotkey_mod.parse(given)
    except hotkey_mod.HotkeyError as broken:
        raise argparse.ArgumentTypeError(str(broken)) from broken
    return given


def _minutes(given: str) -> float:
    """A number of minutes, refusing the values that quietly mean "never".

    `--idle-minutes -1` used to parse, become zero, and switch the timeout off — which is
    what `0` means, but nobody types a minus sign to ask for that. A typo left the microphone
    open indefinitely, which is the one outcome the timeout exists to prevent. `nan` did the
    same, more quietly still: every comparison against it is false.
    """
    try:
        minutes = float(given)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{given!r} is not a number of minutes") from None
    if not math.isfinite(minutes):
        raise argparse.ArgumentTypeError("a number of minutes, or 0 to never stop")
    if minutes < 0:
        raise argparse.ArgumentTypeError(
            f"{given} is negative; use 0 if you mean never to stop on your own"
        )
    return minutes


async def _list_devices() -> int:
    """Every microphone the system offers, by the number `--device` takes."""
    from ..live import capture

    for device in await capture.devices():
        print(f"{device.index}  {device.name}")
    return EXIT_OK


async def _check(args: argparse.Namespace) -> int:
    """Answer the question somebody asks when nothing happened: is it hearing me?

    The old version of this printed two lines, one of which was "not checked here" — the
    permission it could not read being the one people actually wanted to know about. It could
    not distinguish a granted microphone from a denied one, a room from a voice, or the right
    device from the wrong device, which is to say it could not diagnose a single one of the
    ways `stt mic` fails silently. So it opens the microphone and reports what arrives.
    """
    from ..live import quartz

    granted = quartz.trusted()
    print(f"accessibility: {'granted' if granted else 'NOT granted'}")
    if not granted:
        print(f"  {quartz.ACCESSIBILITY_HINT}")
    heard = await _listen_and_report(args)
    if heard is None:
        return EXIT_ENGINE
    working, said = heard
    print(f"\n{said}")
    if not granted:
        return EXIT_PERMISSION
    return EXIT_OK if working else EXIT_ENGINE


async def _listen_and_report(args: argparse.Namespace) -> tuple[bool, str] | None:
    """Open the microphone, draw the meter while it listens, and judge what it heard."""
    from .. import config
    from ..live import capture, dictation, meter, status

    try:
        device = await capture.resolve(args.device)
    except SttError as refused:
        print(f"microphone:    {refused.what}", file=sys.stderr)
        return None
    print(f"microphone:    [{device.index}] {device.name}")
    print(
        f"\n  say something for {meter.LISTEN_SECONDS:.0f} seconds — "
        "the bar passes the mark when stt can hear you\n"
    )
    line = status.Line(enabled=not args.quiet)
    reading = await meter.listen(
        device,
        # The gate a session would build, so the check answers the question the session
        # would answer rather than a similar-looking one.
        gate=dictation.gate_for(config.load_settings()),
        on_sample=lambda level, threshold: line.show(
            f"  {meter.bar(level, threshold)}  {level:5.0f}"
        ),
    )
    line.clear()
    return meter.verdict(reading)
