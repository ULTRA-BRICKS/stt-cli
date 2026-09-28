"""mic — dictate into whatever window has focus, correcting as it goes.

``stt mic`` opens the microphone, and from then on what you say is typed where you are
already typing. Two models run at once: a small one answers immediately so words appear
while you are still speaking, and `large-v3-turbo` answers a second later and replaces them
with the version that has the proper nouns and the punctuation right. Your terminology
dictionary reaches both, so a project name comes out spelled the way you wrote it down.

Press any key yourself and stt stops correcting the sentence in flight and leaves it alone —
it will never delete a character it did not type. Pressing the hotkey again (or releasing
it, for a push-to-talk one) always ends the session; a system notification's Stop button
does too, when `terminal-notifier` is installed (optional — `stt doctor` reports it).

A bare `stt mic` (no arguments at all) is a literal alias for `stt mic --help` — see below
for every flag, including `run`/`start`/`status`/`stop`/`enable`/`disable`, which manage a
background daemon: it loads both models once and then idles with the microphone CLOSED
until a global hotkey (or two — see `stt mic run --help`) opens it for one burst at a time
— `stt mic run` keeps it in THIS shell instead of detaching. Any dictation flag (e.g.
`--device 1`, `--check`) dictates ad-hoc, right now, with no daemon involved — printing
the daemon's state and the configured hotkey(s) first, then opening the microphone.

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
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from .._errors import EXIT_ENGINE, EXIT_MISSING_DEP, EXIT_OK, EXIT_PERMISSION, SttError, UsageError
from ..live import hotkey as hotkey_mod
from ..live.mic_service import SERVE_TOKEN

if TYPE_CHECKING:
    from agenttools_service import ServiceManager

    from ..live.mic_service import LaunchdProbe

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

# The daemon's own Accessibility remediation, shared verbatim by `_status` (#54) and
# `_maybe_autostart_daemon` (#87) — both diagnose the SAME fact (a launchd-spawned process
# denied Accessibility, `probe.last_exit_code == EXIT_PERMISSION`) and must name the SAME
# fix, not two texts that drift. This is deliberately NOT `quartz.ACCESSIBILITY_HINT`
# (that one names "the terminal application running stt" — the ad-hoc CLI's own TCC
# identity): a launchd job execs a different binary under a different TCC identity than
# whatever terminal is asking, so the terminal's own grant is not evidence about the
# daemon's, and telling the user to re-check the terminal's grant would send them to
# check the wrong principal entirely.
_DAEMON_ACCESSIBILITY_FIX = (
    "grant Accessibility to the process launchd runs for the daemon (its python "
    "interpreter, or bin/stt — add it manually in System Settings > Privacy & Security > "
    "Accessibility if not listed; the terminal this ran in does not cover this separate, "
    "launchd-spawned process)"
)


def _exit_note(probe: LaunchdProbe) -> str:
    """ " (last exit code N)" or "" — the launchd-diagnosed exit code, formatted the SAME
    way everywhere a `LaunchdProbe` gets reported (`_daemon_status_line`, `_status` (#54),
    `_autostart_crash_loop_message` (#87)): one owner for the format instead of three
    copies that could drift on the next edit."""
    return "" if probe.last_exit_code is None else f" (last exit code {probe.last_exit_code})"


def run(argv: list[str]) -> int:
    if argv and argv[0] == SERVE_TOKEN:
        return _serve(argv[1:])
    if argv and argv[0] in _LIFECYCLE:
        return _lifecycle(argv)
    if not argv:
        # A bare `stt mic` is a literal alias for `stt mic --help` — argparse's own,
        # unchanged mechanism, not a custom screen. Alex, this session: "stt mic должен
        # открывать stt mic --help (как алиас)". See #85.
        argv = ["--help"]
    args = _parse(argv)
    # Listing first. `--check` was tested first and `--list-devices` is handled further in,
    # so `stt mic --check --list-devices` spent six seconds listening instead of printing the
    # list — the cheaper and more certain of the two answers losing to the slower one.
    if args.list_devices:
        return asyncio.run(_list_devices())
    _adhoc_banner(quiet=args.quiet)
    if args.check:
        return asyncio.run(_check(args))
    from ..live import dictation

    exit_code = asyncio.run(dictation.dictate(args))
    if exit_code == EXIT_OK:
        # A dictation that actually completed is proof Accessibility and the microphone
        # both work on this machine — see `_maybe_autostart_daemon` (#87).
        _maybe_autostart_daemon(quiet=args.quiet)
    return exit_code


def _adhoc_banner(*, quiet: bool) -> None:
    """Printed once, right before an ad-hoc (non-daemon) `stt mic` opens the microphone —
    the background daemon's real state and the configured hotkey, so whoever is dictating
    by hand right now also knows whether the hotkey they could rely on instead is armed.

    Alex, this session: "при запуске не в виде демона нужно запускать проверочное окно" —
    launched NOT as the daemon, THIS is where that check/preview screen belongs. A bare
    `stt mic` (no arguments at all) is a literal alias for `stt mic --help` instead —
    argparse's own, unchanged mechanism, not this screen; see #85. `--list-devices` never
    opens the microphone, so `run` skips this call for it entirely; `--check` and a real
    dictation both open the microphone, so both get the banner. Written to stderr, like
    every other status line/sound/notification this path has — and silenced by the same
    `--quiet`.
    """
    if quiet:
        return
    print(_daemon_status_line(), file=sys.stderr)
    print(_hotkey_lines(), file=sys.stderr)


def _default_manager() -> ServiceManager:
    """The `ServiceManager` for the daemon's OWN default flags — what `_daemon_status_line`
    and `_maybe_autostart_daemon` both need, and neither has any per-invocation daemon flag
    of its own to pass through. One place builds it instead of two."""
    return _manager(_daemon_parser().parse_args([]))


def _daemon_status_line() -> str:
    """One line: is the background daemon running, for `_adhoc_banner`. Reuses the same
    real-state distinction `_status` gives `stt mic status` (#54) — a genuinely alive
    daemon versus a launchd job registered but currently crash-looping — without
    `_status`'s own exit-code and multi-line diagnostic contract, because this is a summary
    line on a banner, not the `status` subcommand itself. Broad `except Exception`,
    deliberately: `_adhoc_banner` runs ahead of every ad-hoc dictation, and a launchctl
    subprocess error, a malformed pidfile, or any other probe failure must degrade to a
    plain line here rather than crash the banner (or the dictation behind it) — `stt mic
    status` is where the full diagnostic belongs.
    """
    try:
        from ..live import mic_service

        manager = _default_manager()
        st = manager.status()
        if st.state != "autostart":
            pid_note = f" (pid {st.pid})" if st.pid else ""
            return f"daemon:  running{pid_note}" if st.running else "daemon:  not running"
        probe = mic_service.probe_launchd(manager.service.label)
        if probe is not None and not probe.alive:
            exit_note = _exit_note(probe)
            return f"daemon:  enabled, but not currently running{exit_note} — see `stt mic status`"
        pid_note = f" (pid {probe.pid})" if probe is not None and probe.pid else ""
        return f"daemon:  running{pid_note} [autostart on]"
    except ImportError:
        return "daemon:  not available (agenttools_service is not installed)"
    except Exception:
        return "daemon:  status unavailable — see `stt mic status` for the full diagnostic"


def _hotkey_lines() -> str:
    """The two configured hotkey bindings — press (toggle) and hold (push-to-talk) — and
    one command to change each.

    `hotkey_press`/`hotkey_hold` are two INDEPENDENT bindings (`live/hotkey.py`'s own
    module docstring; `_daemon_parser`'s `--hotkey-press`/`--hotkey-hold` help): either can
    be armed or disabled on its own, so each gets its own line rather than the one line
    this function reported before the two-binding redesign (#79) landed.
    """
    from .. import config

    try:
        settings = config.load_settings()
    except SttError as broken:
        # A hand-corrupted config.json (bad JSON, an unknown key, a wrong-typed value, ...)
        # must degrade to a plain line here too, the same as `_daemon_status_line` already
        # does for any probe failure — this is the one screen meant to be safe to run
        # blind, and `stt config` diagnoses the real cause in full.
        return f"hotkey:  config unreadable — {broken}\n         run `stt config` to diagnose it"
    press = _one_hotkey_line(
        "press", settings.hotkey_press, hotkey_mod.resolve_press, style="toggle"
    )
    hold = _one_hotkey_line("hold", settings.hotkey_hold, hotkey_mod.resolve_hold, style="hold")
    return f"{press}\n{hold}"


def _one_hotkey_line(
    binding: str,
    configured: str | None,
    resolve: Callable[[str | None], hotkey_mod.Combo | None],
    *,
    style: str,
) -> str:
    """One `_hotkey_lines` line for a single binding (``"press"``/``"hold"``) — both
    bindings need identical invalid-spec / disabled / armed handling against their own
    field name and resolver, so this is written once rather than twice. Named ``style``,
    not ``arm``: this banner's own help text already uses "arm"/"armed" as a VERB
    ("`stt config set ... to arm it`"), and reusing it as the noun for "toggle"/"hold"
    would read ambiguously right beside that verb.
    """
    field = f"hotkey_{binding}"
    try:
        combo = resolve(configured)
    except hotkey_mod.HotkeyError as broken:
        # A hand-edited config.json can hold a syntactically-fine-but-unusable chord —
        # `config._validate` only checks the stored TYPE is `str`; `hotkey.validate` only
        # runs at `stt config set`/TUI time (`commands/config_cmd.py::validate_extra`).
        # `HotkeyError` is a `ValueError`, not the `SttError` the CLI's top-level `guard`
        # recognises — left uncaught here it would raw-traceback the one screen meant to be
        # safe to run blind.
        return (
            f"hotkey:  invalid stored {field} — {broken}\n"
            f"         fix it with `stt config set {field} ...` / `stt config` for the "
            "interactive screen"
        )
    except Exception:
        # `_daemon_status_line` degrades the same way on ANY probe failure, for the same
        # reason: this banner is the one screen meant to be safe to run blind. `resolve`
        # only ever raises `HotkeyError` today, but a future resolver bug should degrade
        # to a plain line here too, not crash the ad-hoc dictation running right behind it.
        return f"hotkey:  {field} unavailable — see `stt config` for the interactive screen"
    if combo is None:
        return f"hotkey:  {binding} disabled — `stt config set {field} ...` to arm it"
    state = "configured" if configured is not None else "the built-in default, not yet configured"
    return (
        f"hotkey:  {combo.spec} ({style}) — {state}\n"
        f"         change it with `stt config set {field} ...`, or `stt config` for the "
        "interactive screen"
    )


# Bounded: launchd takes a moment to actually spawn what `enable()` just told it to
# install, and a permission-denied daemon takes a moment to crash once spawned — probing
# once, immediately, would as often as not catch it mid-launch, neither alive yet nor
# diagnosably dead. ~3s worst case (6 * 0.5s) — long enough for either to happen, short
# enough that a successful ad-hoc dictation that just finished isn't left waiting on the
# daemon's own unrelated startup. See `_wait_for_daemon_liveness` (#87/#54).
_AUTOSTART_PROBE_ATTEMPTS = 6
_AUTOSTART_PROBE_INTERVAL = 0.5


def _wait_for_daemon_liveness(label: str) -> LaunchdProbe | None:
    """Poll `mic_service.probe_launchd` for a few seconds after `enable()`, sleeping only
    BETWEEN checks (same shape as `doctor._wait_for_trust`) — not a new liveness signal,
    the exact OS-authoritative probe `stt mic status` already uses to tell a genuinely
    alive daemon from one that is merely registered and crash-looping (#54). Returns as
    soon as the daemon is either alive OR has a diagnosed exit code to show for itself —
    once either is known there is nothing more useful to learn by waiting out the rest of
    the budget. Returns the LAST probe (possibly `None`, possibly alive=False with no
    diagnosed exit code yet) if the daemon never resolves either way inside the budget.

    Skips the sleeps entirely off macOS: `mic_service.probe_launchd` (called with no
    `runner`, exactly as it is here) answers `None` there by DESIGN, on every attempt,
    with nothing to learn by waiting out the budget — the caller already trusts
    `autostart_active()` alone on this platform (see its own comment on the call site),
    so sleeping through the whole bounded budget for an answer that can never change would
    only hold `locked_for_daemon_decision()`'s lock longer, blocking a concurrent `stt
    config set` for no benefit.
    """
    from ..live import mic_service

    if sys.platform != "darwin":
        return mic_service.probe_launchd(label)
    probe: LaunchdProbe | None = None
    for attempt in range(_AUTOSTART_PROBE_ATTEMPTS):
        probe = mic_service.probe_launchd(label)
        if probe is not None and (probe.alive or probe.last_exit_code is not None):
            return probe
        if attempt < _AUTOSTART_PROBE_ATTEMPTS - 1:
            time.sleep(_AUTOSTART_PROBE_INTERVAL)
    return probe


def _autostart_crash_loop_message(probe: LaunchdProbe | None, *, freshly_enabled: bool) -> str:
    """What to tell the user the moment auto-enable's own liveness check finds the daemon
    it just installed is NOT verified up — naming the real permission principal (per this
    session's TCC-scope investigation: the launchd-spawned process, not the ad-hoc one that
    just proved its OWN Accessibility/microphone access) rather than a generic "grant
    Accessibility" hint, and the same `_DAEMON_ACCESSIBILITY_FIX` wording `stt mic status`
    already gives for the identical diagnosis (#54), not a second text that could drift.
    `probe is None` means macOS itself never answered within the probe budget (`launchctl`
    unreachable/timed out/unrecognized output) — no exit-code evidence either way, so this
    says so plainly instead of guessing at a cause it does not have.

    `freshly_enabled` distinguishes "this call itself just ran `enable()`" from "the daemon
    was already enabled before this call" (autostart left on from a prior crash-loop this
    call is retrying, or an explicit `stt mic enable` from before this feature shipped) —
    `_maybe_autostart_daemon` only calls `manager.enable()` when `st.enabled` was false, so
    saying "was just enabled" in the already-enabled case would state something that never
    happened this call.
    """
    verb = "was just enabled" if freshly_enabled else "is enabled"
    if probe is None:
        return (
            f"the background daemon {verb}, but its liveness could not be "
            "verified (launchctl did not answer) — autostart is NOT confirmed working; "
            "`stt mic status` has the full diagnostic, and the next successful ad-hoc "
            "dictation checks again automatically."
        )
    exit_note = _exit_note(probe)
    if probe.last_exit_code == EXIT_PERMISSION:
        return (
            f"the background daemon {verb}, but it is crash-looping{exit_note}: "
            f"it exited denied Accessibility — {_DAEMON_ACCESSIBILITY_FIX}. autostart is "
            "NOT confirmed working yet; the next successful ad-hoc dictation retries this "
            "check automatically once that is granted, or run `stt mic disable && stt mic "
            "enable` yourself after granting it — `stt mic status` has the full diagnostic."
        )
    return (
        f"the background daemon {verb}, but has not come up yet{exit_note} — "
        "autostart is NOT confirmed working; `stt mic status` has the full diagnostic, and "
        "the next successful ad-hoc dictation checks again automatically."
    )


def _maybe_autostart_daemon(*, quiet: bool) -> None:
    """First successful ad-hoc dictation on THIS machine is proof Accessibility and the
    microphone both actually work here — the exact permission surface the background daemon
    needs (`quartz.require_accessibility()` is the first line of both `dictation.dictate`
    and `dictation.dictate_daemon`). That is the trigger this repurposes as "turn the daemon
    on", instead of requiring the user to separately remember `stt mic enable` — see #87.

    That proof is about the AD-HOC process's own TCC identity, never about the separately
    spawned launchd daemon's — #54 (and this session's own TCC-scope investigation) already
    established those two can genuinely differ, which is exactly how #54's crash-loop
    happens: `enable()` succeeding (the OS accepted the control command) is proof the OS
    autostart mechanism itself works, NOT that the daemon it just installed can actually
    run. So `enable()` succeeding is never enough by itself to call this DECIDED:
    `_wait_for_daemon_liveness` gives the daemon a brief, bounded chance to actually come up
    and reuses `mic_service.probe_launchd` (#54) — the same OS-authoritative liveness/
    exit-code probe `stt mic status` already uses — to find out for real. Only a VERIFIED
    alive daemon persists `True`; a daemon that comes up NOT alive (most commonly
    crash-looping on `EXIT_PERMISSION`) persists `config.MIC_AUTOSTART_RETRY` instead —
    distinct from both `True` (falsely claiming confirmed-working) and `False` (falsely
    claiming the user explicitly declined) — and the user is told the real cause and the
    real principal to fix immediately, not left to discover a silently crash-looping daemon
    later. `MIC_AUTOSTART_RETRY` is treated the SAME as `None` by the gate below, so the
    next successful ad-hoc dictation retries the whole sequence automatically.

    Fires until a real, verified decision lands: `mic_daemon_autostart` records that
    decision so a later run never overrides an explicit `stt mic enable`/`disable`, and
    never re-decides a CONFIRMED auto-enable either. EVERYTHING that can fail — the config
    read, the daemon calls, the liveness probe, AND the config write — is inside the one
    guarded region: a config.json write can itself raise (a lock file that cannot be
    opened, a file that grew past the size guard), and this promises to never break the
    dictation that just succeeded, not merely the daemon half of it.

    The whole check-then-enable-then-probe-then-persist sequence runs under
    `config.locked_for_daemon_decision()`, not just the final write: a status check followed
    by `manager.enable()` with no lock around either left a real window for a concurrent
    explicit `stt mic disable` to turn launchd off in between them — `save_setting_if_unset`
    alone stopped that from being recorded as a lie, but the daemon itself stayed re-enabled
    despite the config correctly reading `False` afterward. `_lifecycle` takes the SAME lock
    around its own enable/disable, so the two can no longer interleave.
    """
    from .. import config

    outcome: Literal["enabled", "retry"] | None = None
    crash: LaunchdProbe | None = None
    freshly_enabled = False
    try:
        with config.locked_for_daemon_decision():
            if config.load_settings().mic_daemon_autostart not in (
                None,
                config.MIC_AUTOSTART_RETRY,
            ):
                return
            manager = _default_manager()
            st = manager.status()
            # `st.enabled` — OS autostart actually installed — not `st.running`: a manager-
            # spawned or foreign process already being alive is not the same fact as autostart
            # being configured, and the whole point here is the LATTER. Keying this off
            # `running` let an already-running-but-never-enabled daemon record
            # `mic_daemon_autostart = True` without `enable()` ever being called — permanently
            # skipping the one thing this function exists to do, since the flag then blocks
            # every future check. Also remembered for the user-facing message below: a
            # daemon whose autostart was already on before this call (left running from a
            # prior crash-loop retry, or `stt mic enable` from before this feature shipped)
            # never had `enable()` called on it HERE, so telling the user it "was just
            # enabled" would state something that did not happen this call.
            freshly_enabled = not st.enabled
            if freshly_enabled:
                manager.enable()
            if not manager.autostart_active():
                # `enable()` itself never raises for a rejected OS control command (a sandboxed
                # launchctl, a missing binary, ...) — it silently falls back to a manager-owned
                # process instead and reports the real outcome through `autostart_active()`,
                # the SAME same-process signal the shared lib's own `run_action` checks before
                # it will print "enabled" (agenttools_service core.py: "Don't claim 'enabled'
                # when the OS rejected it"). Recording `True` here anyway would be a durable
                # lie, AND — because the flag then reads "decided" — would permanently skip
                # every future retry. Leave it unset so the next successful ad-hoc dictation
                # tries again.
                return
            probe = _wait_for_daemon_liveness(manager.service.label)
            # Verified genuinely alive, OR the one platform `probe_launchd` has nothing to
            # ask at all (`mic_service.probe_launchd` short-circuits to `None` off macOS by
            # design — a DIFFERENT fact from "macOS asked and got no usable answer" below,
            # even though both are a bare `None`). This is the SAME "no evidence, not
            # evidence of failure" fallback `_status`/`_daemon_status_line` already give
            # this exact platform gap elsewhere: `autostart_active()` already true is the
            # best liveness signal this platform has ever had, and #87's launchd-specific
            # verification is not a new bar a platform with no launchd has to clear. Both
            # cases persist the SAME confirmed-working state through the SAME call below —
            # two genuinely different reasons to trust it, not two different outcomes.
            confirmed = (probe is not None and probe.alive) or (
                probe is None and sys.platform != "darwin"
            )
            if confirmed:
                # Still the atomic check-and-set, on top of the outer lock above: the outer
                # lock keeps this whole function's daemon control exclusive with
                # `_lifecycle`'s; this is what actually performs the write and is what a
                # test can monkeypatch in isolation without needing to know the outer lock
                # exists at all.
                if config.save_setting_if_unset(
                    "mic_daemon_autostart", True, unset=(None, config.MIC_AUTOSTART_RETRY)
                ):
                    outcome = "enabled"
            else:
                # Either a diagnosed NOT-alive daemon on macOS, or macOS itself failed to
                # answer within the probe budget (`launchctl` unreachable, timed out, or an
                # unrecognized output shape — `probe_launchd`'s own docstring: "no evidence
                # ... must not be read as ... confirmed absent" — but by the SAME logic it
                # must not be read as confirmed ALIVE either). #87 requires the daemon be
                # VERIFIED alive before this can be called confirmed, and neither case is a
                # verification — silently trusting a probe that never answered would leave
                # a genuinely crash-looping daemon marked confirmed forever, exactly the
                # failure this function exists to prevent. NOT `True` (that would durably
                # claim a possibly-permanently-broken daemon is confirmed working) —
                # `MIC_AUTOSTART_RETRY` instead, so this is retried automatically rather
                # than silently wedged. Falls through to the notify code below the lock
                # (not an early `return`) so the user is told, the same as a confirmed
                # `True` is.
                if config.save_setting_if_unset(
                    "mic_daemon_autostart",
                    config.MIC_AUTOSTART_RETRY,
                    unset=(None, config.MIC_AUTOSTART_RETRY),
                ):
                    outcome, crash = "retry", probe
    except ImportError:
        return
    except Exception:  # best-effort; never break a dictation that just worked
        return
    from ..live import status

    if outcome == "enabled":
        verb = "is now on" if freshly_enabled else "is confirmed on"
        status.note(
            f"the background dictation daemon {verb} (autostart) — press the hotkey from "
            "any application to dictate without a terminal open; `stt mic disable` turns "
            "this off.",
            quiet=quiet,
        )
    elif outcome == "retry":
        status.note(
            _autostart_crash_loop_message(crash, freshly_enabled=freshly_enabled), quiet=quiet
        )


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
            "stops that second pass on the sentence in flight; Ctrl-C always finishes the "
            "session, and so does a notification's Stop button when `terminal-notifier` is "
            "installed (optional — `stt doctor` reports it). "
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
    "  note: `stt mic run` still works without it; a bare `stt mic` still shows help "
    "(unaffected — argparse never needs this lib), and an explicit dictation flag "
    "(`stt mic --device 1`, `stt mic --check`) still dictates/checks ad-hoc, its banner "
    "just naming the daemon 'not available' instead of a real state."
)


def _daemon_parser() -> argparse.ArgumentParser:
    """The flags the daemon itself takes — shared between `_lifecycle` (as top-level options
    on `run`/`start`/`status`/`stop`/`enable`/`disable`) and the hidden `__serve` entry those
    actions ultimately run or detach."""
    parser = argparse.ArgumentParser(
        prog="stt mic",
        description=f"{SUMMARY} — as a managed background daemon",
        epilog=(
            "run/start load both models once, then idle with the microphone CLOSED; a "
            "hotkey opens it for one burst at a time — a fast draft model answers first, "
            "then the accurate one retypes it corrected, same two-pass dictation as the "
            "ad-hoc path (see `stt mic --help`). any key you press yourself during a burst "
            "stops that second pass on the sentence in flight. --hotkey-press and "
            "--hotkey-hold are two INDEPENDENT bindings, both settable at once: pressing "
            "the press-chord again, or releasing the hold-chord, ends the burst it started "
            "— and a system notification's Stop button works too, for either one, from "
            "anywhere, when `terminal-notifier` is installed (optional — `stt doctor` "
            "reports it; the hotkey itself always works regardless). "
            "`stt mic stop` ends the daemon itself. needs Accessibility "
            "and Microphone: granted to the TERMINAL for `run` (foreground, this shell) — "
            "but `start`/`enable` detach into a launchd-spawned process with its OWN grant, "
            "separate from any terminal's, so `stt mic status` names that process "
            "specifically if it is what is missing the grant. System Settings > Privacy & "
            "Security. the pidfile lives at $XDG_STATE_HOME/stt/mic.pid (default "
            "~/.local/state/stt/mic.pid), the log at $XDG_CACHE_HOME/stt/mic.log (default "
            "~/.cache/stt/mic.log) — the first place to look if `stt mic status` says it is "
            "enabled but dictation never actually starts."
        ),
    )
    _add_dictation_flags(parser, daemon=True)
    parser.add_argument(
        "--hotkey-press",
        type=_hotkey_press_spec,
        default=None,
        help="press-style chord: press once to start, again to stop (default: "
        f"{hotkey_mod.DEFAULT_PRESS_HOTKEY}; `none` disables it; also settable with "
        "`stt config set hotkey_press ...`)",
    )
    parser.add_argument(
        "--hotkey-hold",
        type=_hotkey_hold_spec,
        default=None,
        help="hold-style chord: dictate only while held, push-to-talk (default: unset/"
        "disabled; also settable with `stt config set hotkey_hold ...`)",
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

    A truly bare `stt mic` (no lifecycle word, no flags at all) is UNCHANGED by any of
    this: it is a literal alias for `stt mic --help`, never a lifecycle action. An
    explicit dictation flag (`stt mic --device 1`, `stt mic --check`) is also unchanged: it
    still dictates/checks ad-hoc immediately (printing `_adhoc_banner` first — see #85).
    Everything here (the shared service lib, the daemon's own flags) is imported lazily,
    so neither of those paths ever pays for it.
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
    _refuse_colliding_hotkey_flags(ns)

    def _print_help_ok() -> int:
        parser.print_help()
        return 0

    if ns.action == "status":
        # The one action the shared lib cannot answer correctly for an OS-managed
        # (`enable`d) daemon — see `_status` and ticket #54.
        return _status(ns)
    if ns.action not in ("enable", "disable"):
        return int(dispatch(ns, on_no_subcommand=_print_help_ok))
    # `enable`/`disable` are the two actions `_maybe_autostart_daemon` (#87) can also take
    # on its own — both the real daemon-control call (`dispatch`, here) AND the config
    # write below run under the SAME lock that function holds around its own check-then-
    # enable-then-persist sequence, so an explicit decision here and an automatic one
    # there can never interleave: whichever gets the lock first finishes turning the
    # daemon on/off AND recording it before the other is allowed to start. Without this,
    # a `stt mic disable` landing between that function's own status check and its
    # `enable()` call could really re-enable the daemon the user had just turned off, even
    # though `save_setting_if_unset` kept the CONFIG record from lying about it.
    from .. import config

    with config.locked_for_daemon_decision():
        exit_code = int(dispatch(ns, on_no_subcommand=_print_help_ok))
        if exit_code == EXIT_OK:
            # An EXPLICIT decision always wins and is always recorded, whichever way it goes.
            # The daemon action ITSELF already succeeded (`exit_code == EXIT_OK`); a failure
            # only PERSISTING the record (a lock file that cannot be opened, config.json
            # grown past the size guard, an `OSError` out of the write itself — `ENOSPC` and
            # friends are not `SttError`s — ...) must not make this whole command look like
            # it failed — but it must not go unnoticed either: an un-persisted `disable`
            # leaves `mic_daemon_autostart` at `None`, so the very next successful ad-hoc
            # dictation would silently re-enable the daemon this command just turned off.
            try:
                config.save_setting("mic_daemon_autostart", ns.action == "enable")
            except (SttError, OSError) as broken:
                from ..live import status

                status.note(
                    f"`stt mic {ns.action}` succeeded, but recording that decision failed: "
                    f"{broken} — a later ad-hoc dictation may re-decide this on its own",
                    quiet=False,
                )
    return exit_code


def _refuse_colliding_hotkey_flags(ns: argparse.Namespace) -> None:
    """`--hotkey-press`/`--hotkey-hold` are each validated individually by their own
    `type=` callback (see `_hotkey_press_spec`/`_hotkey_hold_spec`), but a pair that shares
    a KEYCODE only shows up once both are known together — checked here, right after
    parsing, for every `_lifecycle` action (`enable`/`start`/`run`/...), not just `stt
    config set` (`hotkey.validate`) and daemon-start time (`dictation._resolved_hotkeys`).
    Uncaught, `stt mic enable --hotkey-press cmd+d --hotkey-hold ctrl+d` would install a
    launchd `LaunchAgent` with `KeepAlive` for a daemon that can never actually start (see
    `_resolved_hotkeys`'s own identical check) — a restart loop diagnosable only after the
    fact via `stt mic status` (ticket #54), cheaper to refuse here before anything is
    installed at all.

    Resolution is a no-op ONLY when NEITHER flag was given on this invocation AND the
    action is not one that installs/detaches a daemon (`enable`/`start`) — one flag alone
    is never enough to skip the check, because the OTHER binding still resolves to
    something real (the stored config value, or the built-in default) that can just as
    easily collide. An earlier version returned whenever either flag was `None`, which
    meant `stt mic enable --hotkey-hold cmd+shift+space` (leaving `--hotkey-press` unset)
    silently collided with the built-in `hotkey_press` default and was never refused.
    Mirrors the pattern `hotkey.validate` already uses via `config.load_settings()` for the
    identical problem at `stt config set` time: resolve whichever side was NOT given on the
    command line from stored config before comparing.

    Also refuses BOTH bindings resolving to disabled, for `enable`/`start` specifically —
    not a collision, but just as launchd-fatal (`dictation._resolved_hotkeys` refuses the
    identical state at daemon-start time: "no hotkey is configured"). Found by review: an
    earlier version of this function checked collisions only, so `stt config set
    hotkey_press none` followed by a bare `stt mic enable` (no flags at all — the FIRST
    early-return above used to fire unconditionally whenever neither flag was given on the
    command line, before the stored-both-disabled case was ever resolved) silently
    installed a `LaunchAgent` for a daemon with nothing that could ever open the
    microphone — an immediate, guaranteed crash loop on every restart. Scoped to
    `enable`/`start` only (not `status`/`stop`/`disable`, which have no reason to care
    whether a hotkey is configured, and not `run`, which already reports the identical
    failure immediately and visibly in the foreground via `_resolved_hotkeys` — not a
    silent background crash loop, so it does not need this same preflight).
    """
    installing = ns.action in {"enable", "start"}
    if not installing and ns.hotkey_press is None and ns.hotkey_hold is None:
        return
    from .. import config

    stored = config.load_settings()
    press_spec = ns.hotkey_press if ns.hotkey_press is not None else stored.hotkey_press
    hold_spec = ns.hotkey_hold if ns.hotkey_hold is not None else stored.hotkey_hold
    try:
        press = hotkey_mod.resolve_press(press_spec)
        hold = hotkey_mod.resolve_hold(hold_spec)
    except hotkey_mod.HotkeyError as broken:
        # The side pulled from stored config, not the one just given on this command line
        # (that one already passed its own `type=` callback) — a hand-edited `config.json`
        # that reached an invalid state anyway. Diagnosed here rather than left to crash,
        # same reasoning as `dictation._resolved_hotkeys`'s identical `try`/`except`.
        raise UsageError(
            what="the configured hotkey is invalid",
            why=str(broken),
            how="fix it with `stt config set hotkey_press ...` / "
            "`stt config set hotkey_hold ...`, or `stt config unset` either to go back to "
            "its default",
        ) from broken
    if press is not None and hold is not None and hotkey_mod.same_keycode(press, hold):
        raise UsageError(
            what="--hotkey-press and --hotkey-hold use the same key",
            why=f"{press.spec!r} and {hold.spec!r} both use the same physical key — a key "
            "release only reports which key was let go, not which modifiers were held "
            "with it, so releasing either could be misattributed to the other binding and "
            "leave the microphone open indefinitely",
            how="pick two different keys for --hotkey-press and --hotkey-hold",
        )
    if installing and press is None and hold is None:
        raise UsageError(
            what="no hotkey is configured",
            why="hotkey_press and hotkey_hold are both disabled, so the daemon would idle "
            "forever with nothing that could ever open the microphone",
            how="`stt config unset hotkey_press` to restore the default (cmd+shift+space), "
            "or `stt config set hotkey_press ...`/`hotkey_hold ...` to choose one yourself",
        )


def _reordered_for_argparse(argv: list[str]) -> list[str]:
    """`stt mic start --hotkey-press X` — the lifecycle word first, its flags after, the only
    order every example in this file's own help text and the README shows — does not parse
    as written. `_daemon_parser`'s flags live on the PARENT parser, but once `argv[0]` (the
    lifecycle word — guaranteed present and first; `run()` only reaches `_lifecycle` when it
    is) is consumed as the subcommand, argparse hands everything AFTER it to that
    subcommand's OWN subparser, which `add_service_subcommands` builds with no flags of its
    own — so `--hotkey-press X` there is reported as unrecognized. Moving the lifecycle word
    to the end lets the flags reach the parser that actually declares them, while `argv[0]`
    is still what `run()` looks at to decide `_lifecycle` should run at all — the
    user-facing order never changes, only what this function hands to `argparse.parse_args`.
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
    exit_note = _exit_note(probe)
    if probe.last_exit_code == EXIT_PERMISSION:
        print(
            f"mic: not running — crash-looping{exit_note}: the daemon exited denied Accessibility"
        )
        print(f"     fix: {_DAEMON_ACCESSIBILITY_FIX}, then `stt mic disable && stt mic enable`")
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
    if ns.hotkey_press is not None:
        argv += ["--hotkey-press", ns.hotkey_press]
    if ns.hotkey_hold is not None:
        argv += ["--hotkey-hold", ns.hotkey_hold]
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


def _hotkey_press_spec(given: str) -> str:
    """`--hotkey-press` refusing a spec `hotkey.resolve_press` cannot use, at parse time,
    not at daemon startup. `stt mic enable` bakes the argv this validates into a launchd
    `LaunchAgent` with `KeepAlive`; an unreachable `--hotkey-press` that only failed once
    the daemon actually started would have crash-looped it — every restart reloading both
    models before dying on the same invalid spec.
    """
    try:
        hotkey_mod.resolve_press(given)
    except hotkey_mod.HotkeyError as broken:
        raise argparse.ArgumentTypeError(str(broken)) from broken
    return given


def _hotkey_hold_spec(given: str) -> str:
    """`--hotkey-hold`'s own version of `_hotkey_press_spec` — same reasoning, the other
    binding."""
    try:
        hotkey_mod.resolve_hold(given)
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
