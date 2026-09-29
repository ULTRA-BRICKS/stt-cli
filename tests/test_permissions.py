"""The daemon asks macOS for its own permissions and waits for the click (#98).

No real TCC, launchd or System Settings here: the grants, the probe and the clock are
fakes, and what is asserted is what the user sees and how often they are asked.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import types

import pytest

from stt_cli._errors import EXIT_OK, EXIT_PERMISSION, PermissionDeniedError
from stt_cli.live import mic_service, permissions


class _Trust:
    """A permission that turns on after the daemon has polled it ``after`` times."""

    def __init__(self, after: int) -> None:
        self.after = after
        self.polls = 0
        self.asked = 0

    def granted(self) -> bool:
        self.polls += 1
        return self.polls > self.after

    def ask(self) -> None:
        self.asked += 1


def _grant(trust: _Trust) -> permissions.Grant:
    return permissions.Grant("accessibility", "waiting-accessibility", trust.granted, trust.ask)


async def _no_sleep(_seconds: float) -> None:
    return None


def _wait(trust: _Trust, *, notes: list[str] | None = None, sleep=_no_sleep) -> None:
    asyncio.run(
        permissions.await_grants(
            [_grant(trust)],
            principal="'python3.14'",
            key="/opt/python3.14",
            note=(notes if notes is not None else []).append,
            sleep=sleep,
        )
    )


def test_a_restarting_daemon_asks_once_per_principal(tmp_path, monkeypatch) -> None:
    """The crash-loop that ran 7273 times must never become 7273 prompts: every start of an
    untrusted daemon after the first waits silently, however often it is restarted."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    starts = [_Trust(after=3) for _ in range(4)]
    for trust in starts:
        _wait(trust)
    assert [trust.asked for trust in starts] == [1, 0, 0, 0]


def test_a_new_interpreter_is_a_new_principal_and_is_asked(tmp_path, monkeypatch) -> None:
    """`brew upgrade` moves python — macOS holds the moved binary to no grant, so it must be
    asked again rather than silently waiting on a toggle that is not in the list."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    assert permissions.claim_prompt("accessibility", "/Cellar/3.14.3/python3.14")
    assert not permissions.claim_prompt("accessibility", "/Cellar/3.14.3/python3.14")
    assert permissions.claim_prompt("accessibility", "/Cellar/3.14.4/python3.14")
    assert permissions.claim_prompt("microphone", "/Cellar/3.14.3/python3.14")


def test_polling_slows_once_nobody_is_looking_and_speeds_up_on_a_new_prompt(
    tmp_path, monkeypatch
) -> None:
    """Every Accessibility read is a fresh interpreter: fast while someone is likely looking
    for the toggle, slow after that — and fast again the moment `--fix` makes it ask."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    trust = _Trust(after=10**9)
    now = 0.0
    slept: list[tuple[float, float]] = []
    rearmed = False

    class _Stop(Exception):
        pass

    async def tick(seconds: float) -> None:
        nonlocal now, rearmed
        slept.append((now, seconds))
        now += seconds
        if now >= 400 and not rearmed:
            permissions.rearm()
            rearmed = True
        if now > 460:
            raise _Stop

    grant = permissions.Grant("accessibility", "waiting-accessibility", trust.granted, trust.ask)
    with pytest.raises(_Stop):
        asyncio.run(
            permissions.await_grants(
                [grant],
                principal="'python3.14'",
                key="/opt/python3.14",
                note=[].append,
                sleep=tick,
                clock=lambda: now,
            )
        )
    assert trust.asked == 2
    assert {s for at, s in slept if at < 298} == {permissions.POLL_SECONDS}
    assert {s for at, s in slept if 302 < at < 400} == {permissions.IDLE_POLL_SECONDS}
    assert {s for at, s in slept if at > 412} == {permissions.POLL_SECONDS}


def test_rearm_makes_a_waiting_daemon_ask_again_without_a_restart(tmp_path, monkeypatch) -> None:
    """`stt mic --fix` clears the ledger while the daemon is mid-wait; the very same wait
    must notice on its next tick and ask again."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    first = _Trust(after=2)
    _wait(first)
    assert first.asked == 1, "the first start used up the one prompt"
    trust = _Trust(after=5)

    async def rearm_on_second_tick(_seconds: float, ticks=[0]) -> None:  # noqa: B006
        ticks[0] += 1
        if ticks[0] == 2:
            permissions.rearm()

    _wait(trust, sleep=rearm_on_second_tick)
    assert trust.asked == 1


def test_the_daemon_waits_then_proceeds_the_moment_trust_appears(tmp_path, monkeypatch) -> None:
    """Instead of exiting with code 8: say what to click, keep polling, and carry on with no
    restart once it is granted — reporting each state for `stt mic status` meanwhile."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    trust = _Trust(after=3)
    notes: list[str] = []
    seen: list[str | None] = []

    async def watch(_seconds: float) -> None:
        record = permissions.read_state()
        seen.append(record.state if record else None)

    _wait(trust, notes=notes, sleep=watch)
    assert trust.polls == 4, "polled until the grant appeared, then stopped"
    assert seen == ["waiting-accessibility"] * 2
    assert "turn on 'python3.14'" in notes[0]
    final = permissions.read_state()
    assert final is not None and final.state == "starting"


def test_a_granted_daemon_is_never_asked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    trust = _Trust(after=0)
    notes: list[str] = []
    _wait(trust, notes=notes)
    assert trust.asked == 0
    assert notes == []


def test_a_grant_frozen_in_process_is_watched_fresh_and_the_daemon_restarts_into_it(
    tmp_path, monkeypatch
) -> None:
    """Accessibility's in-process answer never changes after its first read: #98's live
    daemon sat on a turned-on toggle until the Mac was rebooted. The wait must watch a
    fresh process instead, then restart so everything after it runs where the grant is
    visible."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    frozen = _Trust(after=10**9)
    fresh = _Trust(after=2)
    restarts: list[str] = []
    ticks: list[float] = []

    async def bounded(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) > 20:
            raise AssertionError("the wait never noticed the grant")

    grant = permissions.Grant(
        "accessibility", "waiting-accessibility", frozen.granted, frozen.ask, recheck=fresh.granted
    )
    asyncio.run(
        permissions.await_grants(
            [grant],
            principal="'python3.14'",
            key="/opt/python3.14",
            note=[].append,
            sleep=bounded,
            restart=lambda: restarts.append("restart"),
        )
    )
    assert fresh.polls == 3, "noticed on the first fresh read after the click"
    assert restarts == ["restart"]


def test_the_daemon_restarts_itself_at_most_once_per_launch(monkeypatch) -> None:
    """Into the same command line, and only once: a fresh image that still reads "not
    granted" in-process carries on instead of replacing itself every two seconds."""
    monkeypatch.setenv(permissions._RESTARTED, "")
    execs: list[list[str]] = []
    monkeypatch.setattr(permissions.os, "execv", lambda _path, argv: execs.append(argv))
    permissions.restart_self()
    permissions.restart_self()
    assert execs == [[sys.executable, *sys.orig_argv[1:]]]


def test_a_denied_microphone_opens_the_pane_because_macos_never_asks_twice(monkeypatch) -> None:
    """Not asked yet: macOS's own Allow dialog is the one click. Already refused: macOS
    will not show that dialog again, so the pane with the toggle is the only way."""
    opened: list[str] = []
    requested: list[bool] = []
    monkeypatch.setattr(permissions, "request_microphone", lambda: requested.append(True))
    microphone = permissions.default_grants(opener=opened.append)[1]

    monkeypatch.setattr(permissions, "microphone_status", lambda: permissions.MIC_UNDETERMINED)
    microphone.ask()
    assert (requested, opened) == ([True], [])

    monkeypatch.setattr(permissions, "microphone_status", lambda: permissions.MIC_DENIED)
    microphone.ask()
    assert (requested, opened) == ([True], ["microphone"])


def test_a_record_left_by_a_dead_process_describes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    permissions.write_state("waiting-accessibility", "'python3.14'")
    me = permissions.read_state()
    assert me is not None
    assert permissions.state_for(me.pid) == me
    assert permissions.state_for(me.pid + 1) is None


# ---------------------------------------------------------------------------------------
# `stt mic status` and the banner
# ---------------------------------------------------------------------------------------


class _AutostartManager:
    service = argparse.Namespace(label="com.agenttools.stt.mic", logfile="/fake/mic.log")

    def __init__(self, *, enabled: bool = True, active: bool = True) -> None:
        self.enabled = enabled
        self.active = active
        self.enable_calls = 0

    def status(self) -> object:
        return argparse.Namespace(
            state="autostart" if self.enabled else "stopped",
            enabled=self.enabled,
            running=False,
            pid=None,
        )

    def enable(self) -> None:
        self.enable_calls += 1
        self.enabled = True

    def autostart_active(self) -> bool:
        return self.active


def _record_for(pid: int, state: permissions.DaemonState) -> None:
    """What daemon ``pid`` would have written about itself."""
    permissions._write_json(
        permissions._state_path(), {"pid": pid, "state": state, "principal": "'python3.14'"}
    )


def _fake_lib(monkeypatch) -> None:
    fake = types.ModuleType("agenttools_service")
    fake.run_action = lambda manager, action: 0
    monkeypatch.setitem(sys.modules, "agenttools_service", fake)


def test_status_names_the_toggle_instead_of_a_crash_loop(tmp_path, monkeypatch, capsys) -> None:
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    _record_for(4242, "waiting-accessibility")
    _fake_lib(monkeypatch)
    monkeypatch.setattr(mic, "_manager", lambda ns: _AutostartManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=8),
    )

    assert mic._status(argparse.Namespace()) == EXIT_PERMISSION
    out = capsys.readouterr().out
    assert out.startswith("mic: waiting for Accessibility — turn on 'python3.14'")
    assert "crash-looping" not in out
    assert "exit code" not in out

    monkeypatch.setattr(mic, "_default_manager", lambda: _AutostartManager())
    assert mic._daemon_status_line().startswith("daemon:  waiting for Accessibility")


def test_status_is_plain_running_once_the_daemon_is_ready(tmp_path, monkeypatch, capsys) -> None:
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    _record_for(4242, "ready")
    _fake_lib(monkeypatch)
    monkeypatch.setattr(mic, "_manager", lambda ns: _AutostartManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None),
    )
    assert mic._status(argparse.Namespace()) == EXIT_OK
    assert capsys.readouterr().out == "mic: running (pid 4242) [autostart on]\n"


# ---------------------------------------------------------------------------------------
# `stt mic --fix`
# ---------------------------------------------------------------------------------------


class _Daemon:
    """launchd plus the daemon, advancing one step per `sleep`: absent until enabled, then
    alive and waiting for the click, then (after ``click_at`` ticks) ready."""

    def __init__(self, manager: _AutostartManager, *, click_at: int) -> None:
        self.manager = manager
        self.click_at = click_at
        self.ticks = 0
        self.kicked: list[str] = []
        self.kick_accepted = True
        self.now = 0.0

    def probe(self, _label: str) -> mic_service.LaunchdProbe | None:
        if not self.manager.enabled:
            return None
        return mic_service.LaunchdProbe(alive=True, pid=777, last_exit_code=None)

    def kickstart(self, label: str) -> bool:
        self.kicked.append(label)
        return self.kick_accepted

    def sleep(self, seconds: float) -> None:
        self.ticks += 1
        self.now += seconds
        state: permissions.DaemonState = (
            "ready" if self.ticks >= self.click_at else "waiting-accessibility"
        )
        _record_for(777, state)


def _fix_env(tmp_path, monkeypatch, manager: _AutostartManager, *, click_at: int) -> _Daemon:
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mic, "_default_manager", lambda: manager)
    daemon = _Daemon(manager, click_at=click_at)
    monkeypatch.setattr(mic_service, "probe_launchd", daemon.probe)
    monkeypatch.setattr(mic_service, "kickstart", daemon.kickstart)
    return daemon


def test_fix_enables_rearms_waits_for_the_click_and_verifies(tmp_path, monkeypatch, capsys) -> None:
    """The whole of Alex's ask: one command, then only the click. It turns the daemon on,
    makes it ask again, says once which toggle to click, and ends on a verified ready."""
    from stt_cli import config
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=False)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=4)
    permissions.claim_prompt("accessibility", permissions.principal_key())  # asked before

    code = mic._fix(yes=True, clock=lambda: daemon.now, sleep=daemon.sleep)

    out = capsys.readouterr().out.splitlines()
    assert code == EXIT_OK
    assert manager.enable_calls == 1
    assert config.load_settings().mic_daemon_autostart is True
    assert permissions.claim_prompt("accessibility", permissions.principal_key()), (
        "--fix must re-arm the daemon's prompt, or a missed prompt could never come back"
    )
    assert [line for line in out if "waiting for Accessibility" in line] == [out[0]], (
        "the click is named exactly once, not on every poll"
    )
    assert out[-1].startswith("mic: ready — the background daemon (pid 777)")
    assert len(out) == 2


def test_fix_restarts_a_daemon_that_is_down(tmp_path, monkeypatch, capsys) -> None:
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=1)
    down = iter([mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=8)])
    monkeypatch.setattr(
        mic_service, "probe_launchd", lambda label: next(down, None) or daemon.probe(label)
    )

    assert mic._fix(yes=True, clock=lambda: daemon.now, sleep=daemon.sleep) == EXIT_OK
    assert daemon.kicked == ["com.agenttools.stt.mic"]
    assert manager.enable_calls == 0


def test_fix_says_so_when_launchd_will_not_restart_the_daemon(tmp_path, monkeypatch) -> None:
    """A refused restart leaves nothing to watch; ten minutes of watching it would end in a
    wrong "not ready yet"."""
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=1)
    daemon.kick_accepted = False
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=None),
    )

    with pytest.raises(PermissionDeniedError) as refused:
        mic._fix(yes=True, clock=lambda: daemon.now, sleep=daemon.sleep)
    assert "kickstart" in refused.value.why
    assert daemon.now == 0, "said at once, not after watching"


def test_fix_will_not_install_a_daemon_that_has_no_hotkey(tmp_path, monkeypatch) -> None:
    """`stt mic enable` refuses a configuration with no usable hotkey, because that daemon
    can never start and launchd would restart it forever; `--fix` must refuse it too."""
    from stt_cli._errors import UsageError
    from stt_cli.commands import mic
    from stt_cli.commands.config_cmd import _set

    manager = _AutostartManager(enabled=False)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=1)
    _set("hotkey_press", "none")

    with pytest.raises(UsageError, match="no hotkey is configured"):
        mic._fix(yes=True, clock=lambda: daemon.now, sleep=daemon.sleep)
    assert manager.enable_calls == 0


def test_fix_respects_an_explicit_disable_unless_told_yes(tmp_path, monkeypatch, capsys) -> None:
    from stt_cli import config
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=False)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=1)
    config.save_setting("mic_daemon_autostart", False)

    code = mic._fix(
        yes=False, clock=lambda: daemon.now, sleep=daemon.sleep, confirm=lambda q: False
    )
    assert code == EXIT_OK
    assert manager.enable_calls == 0
    assert config.load_settings().mic_daemon_autostart is False

    assert mic._fix(yes=True, clock=lambda: daemon.now, sleep=daemon.sleep) == EXIT_OK
    assert manager.enable_calls == 1


def test_fix_gives_up_watching_with_a_diagnosis_not_a_hang(tmp_path, monkeypatch) -> None:
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=10**9)

    with pytest.raises(PermissionDeniedError) as refused:
        mic._fix(yes=True, clock=lambda: daemon.now, sleep=daemon.sleep)
    assert "waiting for Accessibility" in refused.value.why
    assert "stt mic status" in refused.value.how


def test_fix_restarts_an_older_daemon_once_then_leaves_it_running(
    tmp_path, monkeypatch, capsys
) -> None:
    """A live daemon that never writes a record runs code from before #98. One restart
    moves it onto the current code if its LaunchAgent points there; if it comes back the
    same, it was enabled from an older stt, and killing it every ten seconds (possibly
    mid-dictation) would change nothing."""
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=10**9)

    def silent(seconds: float) -> None:
        daemon.now += seconds  # alive the whole time, never a state record

    assert mic._fix(yes=True, clock=lambda: daemon.now, sleep=silent) == EXIT_OK
    assert daemon.kicked == ["com.agenttools.stt.mic"]
    assert "older stt" in capsys.readouterr().out


def test_fix_leaves_running_an_older_daemon_that_once_exited_denied(
    tmp_path, monkeypatch, capsys
) -> None:
    """launchd keeps reporting the last exit code while a process runs. An older daemon
    that once exited denied Accessibility but has been up since the grant is running, not
    refusing; `--fix` must say so, not fail telling the user it keeps exiting."""
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=10**9)
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=777, last_exit_code=8),
    )

    def silent(seconds: float) -> None:
        daemon.now += seconds  # alive the whole time, never a state record

    assert mic._fix(yes=True, clock=lambda: daemon.now, sleep=silent) == EXIT_OK
    assert "running (pid 777), but it is an older stt" in capsys.readouterr().out


def test_fix_refuses_a_dictation_option_instead_of_dropping_it(monkeypatch) -> None:
    """`--fix` turns on the daemon as installed; `--device 2` next to it would be silently
    thrown away, so it is refused with the command that does honour it."""
    from stt_cli._errors import UsageError
    from stt_cli.commands import mic

    monkeypatch.setattr(mic, "_fix", lambda **_kw: pytest.fail("--fix dropped --device 2"))
    with pytest.raises(UsageError) as refused:
        mic.run(["--fix", "--device", "2"])
    assert "--device" in refused.value.what
    assert "stt mic enable" in refused.value.how


def test_fix_names_the_reinstall_for_an_older_daemon_that_keeps_exiting(
    tmp_path, monkeypatch
) -> None:
    """A daemon enabled from a stt before #98 exits with code 8 instead of waiting, and a
    restart only brings the same code back. Said within seconds — not after watching it
    crash for ten minutes — and launchd's brief restarts in between do not reset the count."""
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=10**9)

    def crash_looping(_label: str) -> mic_service.LaunchdProbe:
        tick = int(daemon.now)
        alive = tick % 10 == 5  # up for a moment every ten seconds, then exits again
        return mic_service.LaunchdProbe(alive=alive, pid=tick if alive else None, last_exit_code=8)

    monkeypatch.setattr(mic_service, "probe_launchd", crash_looping)

    def tick(seconds: float) -> None:
        daemon.now += seconds  # never writes a state record: it is the old code

    with pytest.raises(PermissionDeniedError) as refused:
        mic._fix(yes=True, clock=lambda: daemon.now, sleep=tick)
    assert "stt mic disable && stt mic enable" in refused.value.how
    assert "last exit code 8" in refused.value.why
    assert daemon.now < 30, "diagnosed in seconds, not after the ten-minute watch"


def test_fix_says_the_daemon_keeps_failing_instead_of_waiting_ten_minutes(
    tmp_path, monkeypatch
) -> None:
    """A current daemon that loads for a few seconds and then dies (a model deleted after
    `enable`) is in launchd's restart loop, not waiting for a click. `--fix` says so within
    seconds and points at the log, instead of ten silent minutes ending in a permission
    error for what is not a permission problem."""
    from stt_cli._errors import EngineError
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=10**9)

    def loads_then_dies(_label: str) -> mic_service.LaunchdProbe:
        tick = int(daemon.now)
        alive = tick % 10 < 6  # six seconds loading the models, then exit 10
        if alive:
            _record_for(1000 + tick // 10, "starting")
        return mic_service.LaunchdProbe(
            alive=alive, pid=1000 + tick // 10 if alive else None, last_exit_code=10
        )

    monkeypatch.setattr(mic_service, "probe_launchd", loads_then_dies)

    def tick(seconds: float) -> None:
        daemon.now += seconds

    with pytest.raises(EngineError) as failed:
        mic._fix(yes=True, clock=lambda: daemon.now, sleep=tick)
    assert "last exit code 10" in failed.value.why
    assert "/fake/mic.log" in failed.value.how
    assert daemon.now < 30, "diagnosed in seconds, not after the ten-minute watch"


def test_the_restart_fix_makes_is_not_counted_as_the_daemon_failing(
    tmp_path, monkeypatch, capsys
) -> None:
    """An older daemon, down when `--fix` starts, is restarted by `--fix` and then, since it
    writes no record, restarted once more — and seen down for a moment by that restart.
    That is `--fix`'s own doing, not launchd's restart loop."""
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=10**9)
    polls = 0
    seen_down_after_restart = False

    def older_daemon(_label: str) -> mic_service.LaunchdProbe:
        nonlocal polls, seen_down_after_restart
        polls += 1
        if polls <= 2:  # `--fix`'s own look, then the watcher's first: down, exited denied
            return mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=8)
        if len(daemon.kicked) < 2:
            return mic_service.LaunchdProbe(alive=True, pid=5, last_exit_code=8)
        if not seen_down_after_restart:
            seen_down_after_restart = True  # killed by the restart, up again next poll
            return mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=15)
        return mic_service.LaunchdProbe(alive=True, pid=6, last_exit_code=15)

    monkeypatch.setattr(mic_service, "probe_launchd", older_daemon)

    def silent(seconds: float) -> None:
        daemon.now += seconds  # the old code never writes a state record

    assert mic._fix(yes=True, clock=lambda: daemon.now, sleep=silent) == EXIT_OK
    assert "running (pid 6), but it is an older stt" in capsys.readouterr().out


def test_one_crash_long_after_another_is_not_a_restart_loop(tmp_path, monkeypatch, capsys) -> None:
    """Down once when `--fix` starts, then up and waiting four minutes for the click, then
    down once more: two failures minutes apart are two failures, not launchd's loop, and
    `--fix` keeps watching until the daemon comes back ready."""
    from stt_cli.commands import mic

    manager = _AutostartManager(enabled=True)
    daemon = _fix_env(tmp_path, monkeypatch, manager, click_at=10**9)

    def probe(_label: str) -> mic_service.LaunchdProbe:
        if daemon.now < 1:
            return mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=10)
        if daemon.now < 240:
            _record_for(7, "waiting-accessibility")
            return mic_service.LaunchdProbe(alive=True, pid=7, last_exit_code=10)
        if daemon.now < 241:
            return mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=137)
        _record_for(8, "ready")
        return mic_service.LaunchdProbe(alive=True, pid=8, last_exit_code=137)

    monkeypatch.setattr(mic_service, "probe_launchd", probe)

    def tick(seconds: float) -> None:
        daemon.now += seconds

    assert mic._fix(yes=True, clock=lambda: daemon.now, sleep=tick) == EXIT_OK
    assert "mic: ready — the background daemon (pid 8)" in capsys.readouterr().out


def test_the_toggle_named_is_the_interpreter_a_venv_python_points_to(tmp_path, monkeypatch) -> None:
    """A venv's `python` is a symlink to the real `python3.14`, and System Settings lists
    the real one; naming the symlink would send the user looking for a toggle that is not
    there."""
    real = tmp_path / "python3.14"
    real.touch()
    venv_python = tmp_path / "python"
    venv_python.symlink_to(real)
    monkeypatch.setattr(sys, "executable", str(venv_python))

    assert permissions.principal_name() == "'python3.14'"


def test_yes_alone_is_refused_rather_than_dictating(monkeypatch) -> None:
    from stt_cli._errors import UsageError
    from stt_cli.commands import mic
    from stt_cli.live import dictation

    monkeypatch.setattr(mic, "_fix", lambda **_kw: pytest.fail("-y alone ran --fix"))
    monkeypatch.setattr(dictation, "dictate", lambda _args: pytest.fail("-y alone dictated"))
    with pytest.raises(UsageError) as refused:
        mic.run(["-y"])
    assert "--fix" in refused.value.what


def test_fix_beside_list_devices_is_refused_not_answered_as_a_listing(monkeypatch) -> None:
    """`--list-devices` is otherwise checked first; next to `--fix` that would silently skip
    the fix the user asked for."""
    from stt_cli._errors import UsageError
    from stt_cli.commands import mic

    monkeypatch.setattr(mic, "_fix", lambda **_kw: pytest.fail("--list-devices dropped"))
    monkeypatch.setattr(mic, "_list_devices", lambda: pytest.fail("--fix dropped"))
    with pytest.raises(UsageError) as refused:
        mic.run(["--fix", "--list-devices"])
    assert "--list-devices" in refused.value.what


def test_an_alive_daemon_that_writes_no_record_settles_in_a_second(tmp_path, monkeypatch) -> None:
    """Only a daemon from before #98 says nothing about its permissions; the ad-hoc session
    that just finished must not wait out the whole liveness budget for a record that will
    never come."""
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    probe = mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None)
    monkeypatch.setattr(mic_service, "probe_launchd", lambda label: probe)
    slept: list[float] = []
    monkeypatch.setattr(mic.time, "sleep", slept.append)

    assert mic._wait_for_daemon_liveness("com.agenttools.stt.mic") is probe
    assert sum(slept) <= 1.0


@pytest.mark.parametrize("launchd", [True, False])
def test_only_the_launchd_daemon_waits_for_a_click(monkeypatch, launchd: bool) -> None:
    """The launchd daemon is the principal macOS attributes the prompt to, and nobody is at
    a terminal to read a refusal — it waits. Started from a terminal, the terminal is the
    principal and someone is waiting on an answer — it refuses at once."""
    from stt_cli.live import dictation, quartz

    label = "com.agenttools.stt.mic"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dictation, "_daemon_label", lambda: label)
    monkeypatch.setenv("XPC_SERVICE_NAME", label if launchd else "application.com.apple.Terminal")
    waited: list[str] = []

    async def await_grants(grants, **_kw) -> None:
        waited.append("waited")

    def refuse() -> None:
        raise PermissionDeniedError(what="no Accessibility", why="-", how="-")

    monkeypatch.setattr(permissions, "await_grants", await_grants)
    monkeypatch.setattr(permissions, "default_grants", lambda: [])
    monkeypatch.setattr(quartz, "require_accessibility", refuse)

    if launchd:
        asyncio.run(dictation._permissions())
        assert waited == ["waited"]
    else:
        with pytest.raises(PermissionDeniedError):
            asyncio.run(dictation._permissions())
        assert waited == []


def test_the_daemon_finds_its_tools_on_the_path_it_was_enabled_from(monkeypatch) -> None:
    """launchd's own PATH is /usr/bin:/bin:/usr/sbin:/sbin — no Homebrew ffmpeg. A daemon
    that finally got its permissions must not then die on "ffmpeg not found": the enabling
    shell's PATH is baked into its argv and in force before the daemon starts."""
    from stt_cli.commands import mic
    from stt_cli.live import dictation

    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    argv = mic_service._serve_argv([])
    serve_args = argv[argv.index(mic_service.SERVE_TOKEN) + 1 :]

    seen: list[str] = []

    async def fake_daemon(_args: argparse.Namespace) -> None:
        import os

        seen.append(os.environ["PATH"])

    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(dictation, "dictate_daemon", fake_daemon)
    assert mic._serve(serve_args) == EXIT_OK
    assert seen == ["/opt/homebrew/bin:/usr/bin"]
