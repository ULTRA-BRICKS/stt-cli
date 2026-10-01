"""mic — dictate into whatever window has focus, correcting as it goes.

``stt mic`` opens the microphone, and from then on what you say is typed where you are
already typing. Two models run at once: a small one answers immediately so words appear
while you are still speaking, and `large-v3-turbo` answers a second later and replaces them
with the version that has the proper nouns and the punctuation right. Your terminology
dictionary reaches both, so a project name comes out spelled the way you wrote it down.

Press any key yourself and stt stops correcting the sentence in flight and leaves it alone —
it will never delete a character it did not type. Pressing the hotkey again (or releasing
it, for a push-to-talk one) always ends the session; so does the Stop button on the
notification every session posts, which stt draws with its own small helper app, `stt.app`
(`stt mic helper` builds it; `stt mic enable` and `stt doctor --fix` do too).

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
import contextlib
import importlib.util
import json
import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .._errors import (
    EXIT_ENGINE,
    EXIT_MISSING_DEP,
    EXIT_OK,
    EXIT_PERMISSION,
    EngineError,
    MissingDependencyError,
    PermissionDeniedError,
    SttError,
    UsageError,
)
from ..live import hotkey as hotkey_mod
from ..live.mic_service import AUTOSTART_TOKEN, PATH_FLAG, SERVE_TOKEN

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
# `stt mic helper`: build and install `stt.app` (see `live/helper.py`, #99).
HELPER = "helper"

# `agenttools_service.run_action`'s own convention (its docstring: "3 when status/stop find
# nothing running") — `_status` returns it too when IT is the one deciding nothing is
# running, so a script branching on `stt mic status`'s exit code sees one convention either
# way, not a different number depending on which code path answered.
_EXIT_NOT_RUNNING = 3

# The daemon's own Accessibility remediation, shared verbatim by `_status` (#54) and
# `_maybe_autostart_daemon` (#87). Deliberately NOT `quartz.ACCESSIBILITY_HINT`, which
# names the terminal: a launchd job is a different principal from whatever terminal is
# asking (#80), and only the daemon itself can raise the prompt for it — which is what
# `stt mic --fix` makes it do (#98).
_DAEMON_ACCESSIBILITY_FIX = (
    "run `stt mic --fix` — the daemon asks macOS for its own permission and System "
    "Settings opens on the one toggle to click"
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
    if argv and argv[0] == AUTOSTART_TOKEN:
        return _autostart_worker()
    if argv and argv[0] in _LIFECYCLE:
        return _lifecycle(argv)
    if argv and argv[0] == HELPER:
        return _helper_command(argv[1:])
    if not argv:
        # A bare `stt mic` is a literal alias for `stt mic --help` — argparse's own,
        # unchanged mechanism, not a custom screen. Alex, this session: "stt mic должен
        # открывать stt mic --help (как алиас)". See #85.
        argv = ["--help"]
    args = _parse(argv)
    if args.fix or args.yes:
        _refuse_options_beside_fix(args)
        return _fix(yes=args.yes)
    # Listing first. `--check` was tested first and `--list-devices` is handled further in,
    # so `stt mic --check --list-devices` spent six seconds listening instead of printing the
    # list — the cheaper and more certain of the two answers losing to the slower one.
    if args.list_devices:
        return asyncio.run(_list_devices())
    _adhoc_banner(quiet=args.quiet)
    if args.check:
        return asyncio.run(_check(args))
    from ..live import dictation

    return asyncio.run(dictation.dictate(args))


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
            if not st.running:
                return "daemon:  not running"
            waiting = _waiting_line(st.pid)
            if waiting is not None:
                return f"daemon:  {waiting}"
            pid_note = f" (pid {st.pid})" if st.pid else ""
            return f"daemon:  running{pid_note}"
        probe = mic_service.probe_launchd(manager.service.label)
        if probe is not None and not probe.alive:
            exit_note = _exit_note(probe)
            return f"daemon:  enabled, but not currently running{exit_note} — see `stt mic status`"
        if probe is not None:
            waiting = _waiting_line(probe.pid)
            if waiting is not None:
                return f"daemon:  {waiting}"
        pid_note = f" (pid {probe.pid})" if probe is not None and probe.pid else ""
        return f"daemon:  running{pid_note} [autostart on]"
    except ImportError:
        return "daemon:  not available (agenttools_service is not installed)"
    except Exception:
        return "daemon:  status unavailable — see `stt mic status` for the full diagnostic"


def _waiting_line(pid: int | None) -> str | None:
    """ "waiting for Accessibility — turn on 'python3.14' …" when the live daemon ``pid``
    is holding for a permission click (#98), else None. Its own record, matched on pid,
    is the only witness: the terminal asking is a different principal (#80)."""
    from ..live import permissions

    record = permissions.state_for(pid)
    return None if record is None else permissions.waiting_text(record.state, record.principal)


def _hotkey_lines() -> str:
    """The configured hotkey bindings — press (toggle), hold (push-to-talk) and stop — with
    one command to change each, then the allowed modifiers, then the Globe-key advice when
    `fn` alone is the hold binding (#101).

    `hotkey_press`/`hotkey_hold`/`hotkey_stop` are INDEPENDENT bindings (`live/hotkey.py`'s
    own module docstring; `_daemon_parser`'s `--hotkey-*` help): each can be set on its own,
    so each gets its own line.
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
    stop = _one_hotkey_line(
        "stop",
        settings.hotkey_stop,
        hotkey_mod.resolve_stop,
        style="stop",
        when_unset="hotkey:  stop — press the toggle chord again (hotkey_stop is `same`)",
    )
    lines = [press, hold, stop, f"hotkey:  {hotkey_mod.MODIFIER_RULE}"]
    advice = _globe_key_line(settings.hotkey_hold)
    if advice is not None:
        lines.append(advice)
    return "\n".join(lines)


def _globe_key_line(configured: str | None) -> str | None:
    """The Globe-key advice when `fn` alone is the hold binding (see
    `hotkey.globe_key_advice`), or nothing. An invalid stored hold already has its own line
    above, so a spec that does not resolve simply gets no advice here."""
    try:
        advice = hotkey_mod.globe_key_advice(hotkey_mod.resolve_hold(configured))
    except hotkey_mod.HotkeyError:
        return None
    return None if advice is None else f"hotkey:  {advice}"


def _one_hotkey_line(
    binding: str,
    configured: str | None,
    resolve: Callable[[str | None], hotkey_mod.Combo | None],
    *,
    style: str,
    when_unset: str | None = None,
) -> str:
    """One `_hotkey_lines` line for a single binding (``"press"``/``"hold"``/``"stop"``) —
    each needs identical invalid-spec / unset / armed handling against its own field name
    and resolver, so this is written once. ``when_unset`` replaces the "disabled" line for a
    binding whose unset value is not "off" (`hotkey_stop`'s `same`). Named ``style``, not
    ``arm``: this banner's own help text already uses "arm"/"armed" as a VERB ("`stt config
    set ... to arm it`"), and reusing it as the noun would read ambiguously beside it.
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
        if when_unset is not None:
            return when_unset
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
# diagnosably dead. ~3s worst case (6 * 0.5s), spent in the detached worker
# (`_autostart_worker`), never in the command that triggered it, but under the decision
# lock a concurrent `stt config set` waits on. See `_wait_for_daemon_liveness` (#87/#54).
_AUTOSTART_PROBE_ATTEMPTS = 6
_AUTOSTART_PROBE_INTERVAL = 0.5
# Alive sightings (about a second) before a daemon that has written no state record is
# taken as settled: the current code writes one within its first second.
_AUTOSTART_RECORD_SIGHTINGS = 3


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
    from ..live import permissions

    probe: LaunchdProbe | None = None
    alive_sightings = 0
    for attempt in range(_AUTOSTART_PROBE_ATTEMPTS):
        probe = mic_service.probe_launchd(label)
        alive_sightings += probe is not None and probe.alive
        # Alive is settled once the daemon has said what it is doing (#98: it may be
        # holding for a permission click, which the caller has to tell the user about) —
        # or after a second of saying nothing, which only a daemon from before #98 does.
        if probe is not None and (
            (probe.alive and permissions.state_for(probe.pid) is not None)
            or alive_sightings >= _AUTOSTART_RECORD_SIGHTINGS
            or (not probe.alive and probe.last_exit_code is not None)
        ):
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
            f"`stt mic status` has the full diagnostic, and {_AUTOSTART_RETRIES}."
        )
    exit_note = _exit_note(probe)
    if probe.last_exit_code == EXIT_PERMISSION:
        return (
            f"the background daemon {verb}, but it is crash-looping{exit_note}: "
            f"it exited denied Accessibility — {_DAEMON_ACCESSIBILITY_FIX}. autostart is "
            "NOT confirmed working yet — `stt mic status` has the full diagnostic."
        )
    return (
        f"the background daemon {verb}, but has not come up yet{exit_note} — "
        "autostart is NOT confirmed working; `stt mic status` has the full diagnostic, and "
        f"{_AUTOSTART_RETRIES}."
    )


def _maybe_autostart_daemon(*, quiet: bool) -> None:
    """Turn the background daemon on, verify it came up, and record the outcome — the body
    of `_autostart_worker`, which `autostart_after` starts after a person's first `stt`
    command on this Mac (#105) instead of requiring them to remember `stt mic enable`. It
    used to run only after a successful ad-hoc dictation (#87), and that dictation was
    itself blocked on the permissions only a running daemon can ask for (#98).

    Nothing about the process that triggered this proves anything about the separately
    spawned launchd daemon's own TCC identity — #54 (and this session's own TCC-scope
    investigation) established those two can genuinely differ, which is exactly how #54's
    crash-loop happens: `enable()` succeeding (the OS accepted the control command) is proof
    the OS autostart mechanism itself works, NOT that the daemon it just installed can
    actually run. So `enable()` succeeding is never enough by itself to call this DECIDED:
    `_wait_for_daemon_liveness` gives the daemon a brief, bounded chance to actually come up
    and reuses `mic_service.probe_launchd` (#54) — the same OS-authoritative liveness/
    exit-code probe `stt mic status` already uses — to find out for real. Only a VERIFIED
    alive daemon persists `True`; a daemon that comes up NOT alive (most commonly
    crash-looping on `EXIT_PERMISSION`) persists `config.MIC_AUTOSTART_RETRY` instead —
    distinct from both `True` (falsely claiming confirmed-working) and `False` (falsely
    claiming the user explicitly declined) — and the real cause and the real principal to
    fix are noted (in the daemon's log, where the detached worker's standard error goes).
    `MIC_AUTOSTART_RETRY` is treated the SAME as `None` by the gate below, so a later `stt`
    command retries the whole sequence, on `_AutostartAttempts`' backoff.

    Fires until a real, verified decision lands: `mic_daemon_autostart` records that
    decision so a later run never overrides an explicit `stt mic enable`/`disable`, and
    never re-decides a CONFIRMED auto-enable either. EVERYTHING that can fail — the config
    read, the daemon calls, the liveness probe, AND the config write — is inside the one
    guarded region: a config.json write can itself raise (a lock file that cannot be
    opened, a file that grew past the size guard), and a failure here must end quietly.

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
    waiting: str | None = None
    from ..live import helper

    # Before the argv is built: with `stt.app` installed, launchd starts it rather than the
    # interpreter, and the daemon's permissions belong to "stt" (#99). Outside the lock — a
    # first build is a Swift compile, and `stt mic disable` in another shell must not wait
    # behind it.
    helper.ensure()
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
                # every future retry. Leave it unset so a later `stt` command tries again,
                # on the same backoff as a recorded retry.
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
                    waiting = _waiting_line(probe.pid) if probe is not None else None
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
    except Exception:  # best-effort; a failure here must end quietly
        return
    from ..live import status

    if outcome == "enabled":
        verb = "is now on" if freshly_enabled else "is confirmed on"
        status.note(
            f"the background dictation daemon {verb} (autostart) — "
            + (
                f"{waiting}; after that click, the hotkey works from any application"
                if waiting is not None
                else "press the hotkey from any application to dictate without a terminal open"
            )
            + "; `stt mic disable` turns this off.",
            quiet=quiet,
        )
    elif outcome == "retry":
        status.note(
            _autostart_crash_loop_message(crash, freshly_enabled=freshly_enabled), quiet=quiet
        )


# ── the daemon turns itself on at the first `stt` run on macOS (#105) ─────────────────

# Said wherever an unconfirmed attempt is reported: when the next one happens.
_AUTOSTART_RETRIES = (
    "a later `stt` command tries again by itself, waiting longer after each failed try"
)
# The backoff between attempts that did not end with a verified-alive daemon: ten minutes
# after the first, doubling, never more than a day. A daemon that cannot come alive (one
# denied its permissions, a launchctl that refuses) must not cost every later command a
# worker and three seconds of probing under the decision lock.
_AUTOSTART_RETRY_FIRST_SECONDS = 600.0
_AUTOSTART_RETRY_CAP_SECONDS = 86400.0
_AUTOSTART_ATTEMPTS = "mic-autostart-attempts.json"
# `stt mic` words after which turning the daemon on would second-guess what the person
# just did to it (`stop`, `status`, `disable`, ...), and the hidden entries that ARE the
# daemon and its autostart worker.
_NOT_A_FIRST_RUN = frozenset({*_LIFECYCLE, SERVE_TOKEN, AUTOSTART_TOKEN})


@dataclass(frozen=True)
class _AutostartAttempts:
    """The backoff record beside `mic_daemon_autostart`, in `STT_HOME`: how many attempts
    have been requested since the daemon last came up verified, when the latest request
    was made, and when a worker last finished one. Read only while the record is undecided
    (`None` or `MIC_AUTOSTART_RETRY`) — a decided record costs one config read and nothing
    else."""

    count: int = 0
    requested: float = 0.0
    finished: float = 0.0

    @staticmethod
    def _path() -> Path:
        from .. import config

        return config.app_home() / _AUTOSTART_ATTEMPTS

    @classmethod
    def read(cls) -> _AutostartAttempts:
        """The stored record, or a fresh one when it is missing, unreadable or impossible (a
        negative count) — a lost record costs at most one extra attempt, where a kept bad one
        would make every later `due` raise, silently, and the daemon never come on."""
        try:
            raw = json.loads(cls._path().read_text(encoding="utf-8"))
            count = int(raw["count"])
            if count < 0:
                return cls()
            return cls(count, float(raw["requested"]), float(raw["finished"]))
        except (OSError, ValueError, TypeError, KeyError):
            return cls()

    def write(self) -> None:
        from ..archive import write_atomic

        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(asdict(self), sort_keys=True) + "\n")

    def due(self, now: float) -> bool:
        """May another attempt start at ``now``? Always for the first; after that only once
        the backoff since the latest request has passed. A request stamped in the future
        (the clock was set back) does not hold anything off."""
        if self.count == 0 or now < self.requested:
            return True
        wait = _AUTOSTART_RETRY_FIRST_SECONDS * (1 << min(self.count - 1, 16))
        return now >= self.requested + min(wait, _AUTOSTART_RETRY_CAP_SECONDS)


def autostart_after(argv: list[str]) -> None:
    """Called by `cli.main` after every dispatched command: on macOS, while nobody has
    decided about the background daemon (`mic_daemon_autostart` unset, or a `retry` whose
    backoff has passed), start `_autostart_worker` detached and say, in one line, which
    toggle macOS is about to ask for (#105). Runs AFTER the command, so a first-ever `stt
    mic disable` or `enable` has already decided by the time this looks.

    Never delays the command that triggered it: turning the daemon on and verifying it
    takes up to about three seconds, all of it in the worker, which this does not wait for.
    A decided record costs one config read — no lock, no launchctl, no worker. Anything
    that goes wrong here is swallowed: the command that just ran has already answered.
    """
    if sys.platform != "darwin" or (
        argv[:1] == ["mic"] and (len(argv) == 1 or argv[1] in _NOT_A_FIRST_RUN)
    ):
        return
    try:
        from .. import config

        record = config.load_settings().mic_daemon_autostart
        if record is not None and record != config.MIC_AUTOSTART_RETRY:
            return
        attempts = _AutostartAttempts.read()
        now = time.time()
        if not attempts.due(now) or not _service_lib_installed():
            return
        from ..live import helper, permissions, status

        # The worker builds `stt.app` before enabling, and then macOS asks for "stt", not
        # for this interpreter (#99) — unless a daemon enabled before the helper existed is
        # already installed: the worker leaves that job alone, and it still runs as Python.
        builds = helper.installed() is not None or helper.can_build()
        principal = (
            permissions.HELPER_PRINCIPAL
            if builds and _daemon_outside_helper() is None
            else permissions.principal_name()
        )
        again = "turning on" if attempts.count == 0 else "trying again to turn on"
        message = (
            f"{again} the background dictation daemon, so the hotkey works from any "
            f"application — when macOS asks, turn on {principal} in System "
            "Settings > Privacy & Security > Accessibility and allow its microphone; `stt mic "
            "disable` turns this off."
        )
        # Stamped before the worker starts, so a command run while it works finds this
        # attempt in flight rather than starting another. A worker that could not even be
        # started (fork refused, interpreter gone) is no attempt: the stamp is taken back,
        # so the next command tries again instead of waiting out a backoff nobody earned.
        _AutostartAttempts(attempts.count + 1, now, attempts.finished).write()
        try:
            _spawn_autostart_worker()
        except Exception:
            attempts.write()
            return
        # Inside the `try` too: `stt ... 2>&1 | head` has closed standard error by now, and
        # the command's exit code, which `guard` already settled, must not become a traceback.
        status.note(message, quiet=_asked_quiet(argv))
    except Exception:
        return


def _service_lib_installed() -> bool:
    """Whether `agenttools_service`, which every daemon action needs, can be imported —
    asked without importing it, so a machine without it pays nothing and starts nothing."""
    return importlib.util.find_spec("agenttools_service") is not None


def _asked_quiet(argv: list[str]) -> bool:
    """Did the command that just ran take `-q`/`--quiet`? Then the one line about the daemon
    is not printed either; the worker still starts. Only `stt mic` and a transcription have
    the flag, and a command whose options did not parse never reaches this, so reading argv
    the way their parsers do is enough: `--quiet` or a prefix argparse accepts for it (no
    other option starts with `--q`), `-q` alone or leading a cluster (`-qo out.txt`), and
    nothing after `--`, where `-q` is a file."""
    for token in argv:
        if token == "--":
            return False
        if token.startswith("--") and len(token) > 2 and "--quiet".startswith(token):
            return True
        if token.startswith("-q"):
            return True
    return False


def _spawn_autostart_worker() -> None:
    """`stt mic __autostart` in a session of its own, detached from this terminal, with
    nothing read back: the outcome is the record it writes (#105). With `PYTHONSAFEPATH`:
    an installed stt's worker is `python -m stt_cli.cli`, and `-m` otherwise imports from
    the directory the command was typed in first, so a checkout or a planted `stt_cli` there
    would be the code that decides about the daemon. The directory itself is kept, since a
    relative `STT_HOME` is relative to it."""
    import subprocess

    from ..live import mic_service

    subprocess.Popen(
        mic_service.autostart_worker_argv(),
        env={**os.environ, "PYTHONSAFEPATH": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _autostart_worker() -> int:
    """The detached half of `autostart_after`: `_maybe_autostart_daemon` under the decision
    lock, with what it has to say appended to the daemon's own log, since its standard error
    is nobody's terminal; `stt mic status` then shows the daemon it left behind.

    Under the lock it first asks whether any worker has finished since the latest request:
    two commands started together may each start one, and only the first may probe. Then
    it records the attempt as finished, resetting the count once the daemon is verified,
    so the backoff starts over from the beginning if the record is ever unset again.
    """
    from .. import config
    from ..live import helper

    # Outside the lock, for the reason `_maybe_autostart_daemon` gives; it finds it built.
    helper.ensure(say=False)
    with config.locked_for_daemon_decision():
        attempts = _AutostartAttempts.read()
        if attempts.finished >= attempts.requested:
            return EXIT_OK
        try:
            logfile = _default_manager().service.logfile
            logfile.parent.mkdir(parents=True, exist_ok=True)
            with open(logfile, "a", encoding="utf-8") as log, contextlib.redirect_stderr(log):
                _maybe_autostart_daemon(quiet=False)
        finally:
            confirmed = config.load_settings().mic_daemon_autostart is True
            count = 0 if confirmed else attempts.count
            _AutostartAttempts(count, attempts.requested, time.time()).write()
    return EXIT_OK


# `--fix` waits on a person, so its budget is a person's: ten minutes to find the window
# and click. The daemon keeps waiting after that regardless; this only stops watching it.
_FIX_TIMEOUT_SECONDS = 600.0
_FIX_POLL_SECONDS = 1.0
# A live daemon that has written no state record by now is running code from before
# #98 (the current code writes one within its first second). It is restarted once, into
# whatever its LaunchAgent names — the current code, unless it was enabled from an older
# stt, in which case another restart would only bring the same old code back.
_FIX_LEGACY_GRACE_SECONDS = 10.0
# launchd relaunches a daemon that exits after ten seconds. One still down, or down again,
# this long after it was first seen down after a failed exit is in that loop.
_FIX_RESTART_LOOP_SECONDS = 10.0


def _refuse_options_beside_fix(args: argparse.Namespace) -> None:
    """`--fix` turns on the daemon as it is installed, so any other option next to it would
    be silently dropped — and `-y` alone answers a question nothing else asks. Both are
    refused instead. Read from the parsed namespace, so an abbreviation argparse accepted
    (`--fi`) counts as what it means."""
    if not args.fix:
        raise UsageError(
            what="`-y` only goes with `--fix`",
            why="it answers `--fix`'s one question in advance; nothing else asks one",
            how="`stt mic --fix -y`",
        )
    defaults = vars(_parse([]))
    given = [
        f"--{name.replace('_', '-')}"
        for name, value in vars(args).items()
        if name not in {"fix", "yes"} and value != defaults.get(name)
    ]
    if given:
        raise UsageError(
            what=f"`stt mic --fix` takes no other options (got: {' '.join(given)})",
            why="it turns on the background daemon as it is installed; the daemon's "
            "options are set when it is enabled",
            how="`stt mic enable <options>` installs the daemon with them, then run "
            "`stt mic --fix`",
        )


def _fix(
    *,
    yes: bool,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    confirm: Callable[[str], bool] | None = None,
) -> int:
    """`stt mic --fix`: everything but the click (#98).

    Alex: "Я просил сделать --fix чтобы мне было достаточно только кнопку нажать и все что
    нужно само сделается". So: the daemon is turned on if it is off, restarted if it is
    down, and told to ask macOS again (`permissions.rearm` — the daemon's waiting loop
    re-reads its ledger every tick, so a live one asks within ten seconds, no restart).
    The asking is the DAEMON's: a prompt raised from here would be attributed to the
    terminal (#80). This then watches the daemon's own record and launchd's liveness
    probe (#54) until it is ready, printing each click the user is waiting on once.
    """
    if sys.platform != "darwin":
        raise UsageError(
            what="`stt mic --fix` only applies on macOS",
            why="it has the background daemon ask macOS for Accessibility and Microphone, "
            "which other systems do not gate this way",
            how="`stt mic enable` starts the background daemon",
        )
    from .. import config
    from ..live import mic_service, permissions

    try:
        manager = _default_manager()
    except ImportError as missing:
        raise MissingDependencyError(
            what="`stt mic --fix` needs the shared 'agenttools_service' lib",
            why="the background daemon is installed and started through it",
            how="pip install -e <agent-tools>/lib/agenttools_daemon "
            "-e <agent-tools>/lib/agenttools_service",
        ) from missing
    label = manager.service.label
    # Asked before the lock, never inside it: a person reading a y/N question must not hold
    # up a concurrent `stt config set` or ad-hoc dictation waiting on the same lock.
    if (
        not yes
        and not manager.status().enabled
        and config.load_settings().mic_daemon_autostart is False
        and not (confirm or _confirm)(
            "the background daemon was turned off with `stt mic disable` — turn it back on, "
            "with its default settings? [y/N] "
        )
    ):
        print("mic: left off — nothing changed")
        return EXIT_OK
    with config.locked_for_daemon_decision():
        st = manager.status()
        permissions.rearm()
        if not st.enabled:
            # The same refusal `stt mic enable` gives: a daemon with no usable hotkey can
            # never start, and installing it would only buy launchd's restart loop.
            _refuse_colliding_hotkey_flags(
                argparse.Namespace(
                    action="enable", hotkey_press=None, hotkey_hold=None, hotkey_stop=None
                )
            )
            manager.enable()
            if not manager.autostart_active():
                raise PermissionDeniedError(
                    what="macOS did not accept the background daemon",
                    why="`launchctl load` of its LaunchAgent failed",
                    how=f"run `stt mic enable` to see launchctl's answer; the log is "
                    f"{manager.service.logfile}",
                )
            config.save_setting("mic_daemon_autostart", True)
        else:
            probe = mic_service.probe_launchd(label)
            if (probe is None or not probe.alive) and not mic_service.kickstart(label):
                raise PermissionDeniedError(
                    what="macOS did not restart the background daemon",
                    why="`launchctl kickstart` of its LaunchAgent failed",
                    how="`stt mic disable && stt mic enable` reinstalls it; the log is "
                    f"{manager.service.logfile}",
                )
    return _await_ready(label, str(manager.service.logfile), clock=clock, sleep=sleep)


def _await_ready(
    label: str, logfile: str, *, clock: Callable[[], float], sleep: Callable[[float], None]
) -> int:
    """Watch the daemon until its own record says ready, saying each wait once."""
    from ..live import mic_service, permissions

    deadline = clock() + _FIX_TIMEOUT_SECONDS
    said: str | None = None
    unrecorded: tuple[int, float] | None = None
    up: tuple[int, float] | None = None
    first_down: float | None = None
    kicked = False
    while True:
        probe = mic_service.probe_launchd(label)
        pid = probe.pid if probe is not None and probe.alive else None
        record = permissions.state_for(pid)
        if pid is not None and (up is None or up[0] != pid):
            up = (pid, clock())
        if up is not None and pid == up[0] and clock() - up[1] >= _FIX_RESTART_LOOP_SECONDS:
            # Up longer than a restart loop lets it stay up: a later exit is a new failure,
            # not more of an old one.
            first_down = None
        if pid is None and probe is not None and probe.last_exit_code not in (None, EXIT_OK):
            # Down after a failed exit. Once is a restart in progress; still or again down
            # this long after is launchd's restart loop, which a brief sighting of each new
            # process does not end. Waiting on it would take the whole ten minutes to say
            # what is already certain.
            if first_down is None:
                first_down = clock()
            elif clock() - first_down >= _FIX_RESTART_LOOP_SECONDS:
                if probe.last_exit_code == EXIT_PERMISSION:
                    # The current daemon waits for the click and never exits with this
                    # code; only one from before #98 does.
                    raise PermissionDeniedError(
                        what="the background daemon is an older stt that exits instead of "
                        "waiting for the click",
                        why=f"it keeps exiting denied Accessibility{_exit_note(probe)}; it "
                        "was enabled from a stt before this one, and restarting it only "
                        "brings the same code back",
                        how="`stt mic disable && stt mic enable` reinstalls it from this "
                        "stt, which asks macOS and waits; then run `stt mic --fix` again",
                    )
                raise EngineError(
                    what="the background daemon keeps exiting",
                    why=f"launchd restarts it and it fails again{_exit_note(probe)}, and not "
                    "for want of a permission",
                    how=f"its log says why: {logfile}",
                )
        if record is not None:
            if record.state == "ready":
                print(
                    f"mic: ready — the background daemon (pid {pid}) has its permissions; "
                    f"{_press_hint()}"
                )
                return EXIT_OK
            line = permissions.waiting_text(record.state, record.principal) or (
                "permissions granted — loading the models (up to a minute)"
            )
            if line != said:
                print(f"mic: {line}")
                said = line
        elif pid is not None:
            if unrecorded is None or unrecorded[0] != pid:
                unrecorded = (pid, clock())
            elif clock() - unrecorded[1] >= _FIX_LEGACY_GRACE_SECONDS:
                if kicked:
                    # Still alive after a restart, so it got past its own Accessibility
                    # check (older code exits when it is missing) — it works, it just
                    # cannot say so. Killing it again would change nothing.
                    print(
                        f"mic: running (pid {pid}), but it is an older stt that cannot "
                        "report its permissions — `stt mic disable && stt mic enable` "
                        "reinstalls it from this one"
                    )
                    return EXIT_OK
                mic_service.kickstart(label)
                kicked = True
                unrecorded = None
                first_down = None  # the restart just made is not a failure of its own
        if clock() >= deadline:
            if said is None:
                last_exit = "" if probe is None or probe.alive else _exit_note(probe)
                said = f"not running{last_exit}"
            raise PermissionDeniedError(
                what="the background daemon is not ready yet",
                why=f"after ten minutes it is still: {said}",
                how="it keeps waiting by itself and carries on after the click — "
                f"`stt mic status` shows where it is; the log is {logfile}",
            )
        sleep(_FIX_POLL_SECONDS)


def _press_hint() -> str:
    """ "press cmd+shift+space in any application" — or a pointer when it cannot be read."""
    from .. import config

    try:
        combo = hotkey_mod.resolve_press(config.load_settings().hotkey_press)
    except (hotkey_mod.HotkeyError, SttError):
        combo = None
    return (
        f"press {combo.spec} in any application to dictate"
        if combo is not None
        else "press your hotkey in any application to dictate"
    )


def _confirm(question: str) -> bool:
    """A yes/no on the terminal; no terminal, no yes — `-y` is how a script says yes."""
    if not sys.stdin.isatty():
        return False
    return input(question).strip().lower() in {"y", "yes"}


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
            "session, and so does the Stop button on the notification each session posts "
            "(drawn by stt's own helper app — `stt mic helper` builds it). "
            "needs Accessibility and Microphone granted to the TERMINAL running stt (not "
            "stt itself) — System Settings > Privacy & Security. if nothing appears at all, "
            "`stt mic --check` shows what the microphone actually hears against the bar it "
            "has to clear. run/start/status/stop/enable/disable run the same dictation as a "
            "background daemon behind a hotkey instead — see `stt mic run --help`. the "
            "daemon asks macOS for its OWN Accessibility and Microphone and waits for your "
            "click; `stt mic --fix` turns it on and makes it ask again if you missed it. "
            f"the daemon's hotkeys — {hotkey_mod.MODIFIER_RULE} (`stt mic run --help`)"
        ),
    )
    _add_dictation_flags(parser, daemon=False)
    parser.add_argument(
        "--check",
        action="store_true",
        help="open the microphone, show what it hears, and say whether that is enough",
    )
    parser.add_argument("--list-devices", action="store_true", help="show microphones and exit")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="make the background daemon work: turn it on, have it ask macOS for its own "
        "permissions, open System Settings on the toggle to click, and wait until it is ready",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="with --fix: never ask a question first"
    )
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
            "--hotkey-hold are INDEPENDENT bindings, both settable at once, and "
            "--hotkey-stop is an optional third chord for press bursts only: pressing the "
            "press-chord again (or --hotkey-stop), or releasing the hold-chord, ends the "
            "burst it started — and so does the Stop button on the notification each "
            "burst posts, for either one, from anywhere (drawn by stt's own helper app, "
            "`stt mic helper`; the hotkey itself always works regardless). hotkeys — "
            f"{hotkey_mod.MODIFIER_RULE}. a press chord may not be an editing shortcut you "
            "press while dictating (cmd+z, cmd+c, cmd+v, cmd+a, cmd+s, cmd+arrows, ...). "
            "`stt mic stop` ends the daemon itself. needs Accessibility "
            "and Microphone: granted to the TERMINAL for `run` (foreground, this shell) — "
            "but `enable` hands the daemon to launchd, which runs it as its OWN principal "
            "(the python interpreter), separate from any terminal's: that daemon asks macOS "
            "for both itself, opens System Settings on the toggle, and waits for your click "
            "— `stt mic status` says what it is waiting for, and `stt mic --fix` makes it "
            "ask again. the pidfile lives at $XDG_STATE_HOME/stt/mic.pid (default "
            "~/.local/state/stt/mic.pid), the log at $XDG_CACHE_HOME/stt/mic.log (default "
            "~/.cache/stt/mic.log) — the first place to look if `stt mic status` says it is "
            "enabled but dictation never actually starts."
        ),
    )
    _add_dictation_flags(parser, daemon=True)
    # Baked in by `mic_service._serve_argv`, never typed: the PATH the daemon finds its
    # tools on once launchd, with its own bare PATH, is the one running it.
    parser.add_argument(PATH_FLAG, dest="search_path", default=None, help=argparse.SUPPRESS)
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
        help="hold-style chord: dictate only while held, push-to-talk; `fn` alone works too "
        '— set System Settings > Keyboard > "Press 🌐 key to" to "Do Nothing" first '
        "(default: unset/disabled; also settable with `stt config set hotkey_hold ...`)",
    )
    parser.add_argument(
        "--hotkey-stop",
        type=_hotkey_stop_spec,
        default=None,
        help="an extra chord that ends a press-style burst, never a hold one (default: "
        f"`{hotkey_mod.STOP_SAME}` — press the press chord again; also settable with "
        "`stt config set hotkey_stop ...`)",
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
    if ns.action in {"enable", "start", "run"}:
        from ..live import helper

        # Before `_manager` builds the daemon's argv, which starts with `stt.app` once it
        # exists — and so that the daemon's bursts have their Stop button (#99).
        helper.ensure()
    if ns.action in {"enable", "start"} and ns.hotkey_hold is not None:
        # `run` says this itself when the daemon comes up; a detached one only says it to
        # its log file, which nobody reads at the moment they bind fn.
        advice = hotkey_mod.globe_key_advice(hotkey_mod.resolve_hold(ns.hotkey_hold))
        if advice is not None:
            print(advice, file=sys.stderr)

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
            # leaves `mic_daemon_autostart` at `None`, so the very next `stt` command would
            # silently re-enable the daemon this command just turned off (#105).
            try:
                config.save_setting("mic_daemon_autostart", ns.action == "enable")
            except (SttError, OSError) as broken:
                from ..live import status

                status.note(
                    f"`stt mic {ns.action}` succeeded, but recording that decision failed: "
                    f"{broken} — a later `stt` command may re-decide this on its own",
                    quiet=False,
                )
    return exit_code


def _refuse_colliding_hotkey_flags(ns: argparse.Namespace) -> None:
    """`--hotkey-press`/`--hotkey-hold`/`--hotkey-stop` are each validated individually by
    their own `type=` callback (the three `_hotkey_*_spec`), but two that share a KEYCODE only
    show up once all are known together — checked here, right after parsing, for every
    `_lifecycle` action (`enable`/`start`/`run`/...), not just `stt config set`
    (`hotkey.validate`) and daemon-start time (`dictation._resolved_hotkeys`).
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
    given = {name: getattr(ns, name, None) for name in hotkey_mod.BINDINGS}
    if not installing and all(spec is None for spec in given.values()):
        return
    from .. import config

    stored = config.load_settings()
    resolved: dict[str, hotkey_mod.Combo | None] = {}
    for name in hotkey_mod.BINDINGS:
        spec = given[name] if given[name] is not None else getattr(stored, name)
        try:
            combo = hotkey_mod.resolve(name, spec)
        except hotkey_mod.HotkeyError as broken:
            # The side pulled from stored config, not one just given on this command line
            # (that one already passed its own `type=` callback) — a hand-edited
            # `config.json` that reached an invalid state anyway. Diagnosed here rather than
            # left to crash, same reasoning as `dictation._resolved_hotkeys`.
            raise UsageError(
                what="the configured hotkey is invalid",
                why=str(broken),
                how=f"fix it with {hotkey_mod.how_to_fix(name)}, or `stt config unset {name}` "
                "to go back to its default",
            ) from broken
        resolved["--" + name.replace("_", "-")] = combo
    hotkey_mod.refuse_shared_key(
        resolved,
        how="pick a different key for each of --hotkey-press, --hotkey-hold and --hotkey-stop",
    )
    if installing and resolved["--hotkey-press"] is None and resolved["--hotkey-hold"] is None:
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


def _helper_command(argv: list[str]) -> int:
    """`stt mic helper [--rebuild]`: build `stt.app` from the Swift source shipped with stt
    and install it in ~/Applications. It draws the Stop button on every burst's notification
    and is what macOS grants the background daemon's permissions to (`live/helper.py`)."""
    from ..live import helper

    parser = argparse.ArgumentParser(
        prog="stt mic helper",
        description=(
            f"build {helper.APP_NAME} — stt's own small app that draws the Stop button on "
            "the notification every dictation posts, and that macOS grants the background "
            "daemon's Accessibility and Microphone to — from the Swift source shipped with "
            f"stt, and install it as {helper.app_path()}. needs the Swift compiler "
            "(xcode-select --install). `stt mic enable` and `stt doctor --fix` run this too."
        ),
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="build even when the installed copy is current"
    )
    ns = parser.parse_args(argv)
    if sys.platform != "darwin":
        raise UsageError(
            what=f"{helper.APP_NAME} is macOS-only",
            why="it is a macOS application bundle",
            how="nothing to do on this platform: the hotkey stops a dictation",
        )
    if not ns.rebuild and helper.state() == "current":
        built = None
    else:
        # `None` here too when another stt built it while this one waited for the lock.
        built = helper.build(force=ns.rebuild)
    if built is None:
        print(f"{helper.app_path()} is up to date")
    else:
        print(f"installed {built.app} ({helper.describe(built.signing)})")
    moved = _daemon_outside_helper()
    if moved is not None:
        print(
            f"the background daemon still runs as {moved}; `stt mic disable && stt mic enable` "
            f"moves it into {helper.APP_NAME}, and macOS then asks once for Accessibility and "
            'Microphone for "stt" — grants a Python upgrade no longer takes away'
        )
    return EXIT_OK


def _daemon_outside_helper() -> str | None:
    """The program an installed LaunchAgent starts, when that is not `stt.app` — the daemon
    enabled before the helper existed, still holding its permissions under Python."""
    import plistlib

    from ..live import helper

    unit = Path.home() / "Library" / "LaunchAgents" / "com.agenttools.stt.mic.plist"
    try:
        with unit.open("rb") as handle:
            program = plistlib.load(handle).get("ProgramArguments", [None])[0]
    except (OSError, plistlib.InvalidFileException, ValueError, IndexError):
        return None
    if not isinstance(program, str) or program == str(helper.executable()):
        return None
    return program


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
        waiting = _waiting_line(st.pid) if st.running else None
        if waiting is not None:
            print(f"mic: {waiting}")
            return EXIT_PERMISSION
        return int(run_action(manager, "status"))

    probe = mic_service.probe_launchd(manager.service.label)
    if probe is None:
        # launchctl itself unreachable (or off macOS): no better answer than the shared
        # lib's own OS-managed-is-presumed-running read.
        return int(run_action(manager, "status"))
    if probe.alive:
        # A live process may still be holding for a permission click (#98) — its own
        # record says so; otherwise the shared lib has no pid for an OS-managed instance
        # and launchctl's is the better answer.
        waiting = _waiting_line(probe.pid)
        if waiting is not None:
            print(f"mic: {waiting}")
            print(f"     pid {probe.pid} [autostart on] — it carries on by itself after the click")
            return EXIT_PERMISSION
        print(f"mic: running (pid {probe.pid}) [autostart on]")
        return EXIT_OK

    # Registered and enabled, but launchd has no live process for it right now (#54). The
    # reason, when known, comes from the DAEMON's own exit code — never `quartz.trusted()`
    # of this CLI process, which answers for the terminal's principal, not the daemon's.
    # A daemon from before #98 still exits with `EXIT_PERMISSION` when not trusted.
    exit_note = _exit_note(probe)
    if probe.last_exit_code == EXIT_PERMISSION:
        print(
            f"mic: not running — crash-looping{exit_note}: the daemon exited denied Accessibility"
        )
        print(f"     fix: {_DAEMON_ACCESSIBILITY_FIX}")
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
    if ns.hotkey_stop is not None:
        argv += ["--hotkey-stop", ns.hotkey_stop]
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
    if args.search_path:
        os.environ["PATH"] = args.search_path
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


def _hotkey_stop_spec(given: str) -> str:
    """`--hotkey-stop`'s own version of `_hotkey_press_spec` — checked with the press rule,
    since a stop chord is pressed, not held."""
    try:
        hotkey_mod.resolve_stop(given)
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
