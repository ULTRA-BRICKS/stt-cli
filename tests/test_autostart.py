"""The background daemon turns itself on at the first `stt` run on macOS (#105): which
commands count as a run, what a decided record costs, the backoff between attempts, the
detached worker, and the guard in `conftest.py` that keeps the suite away from the real
launchd. What the worker does once it runs — enable, probe, record — is `_maybe_autostart_
daemon`, tested in `test_hotkey.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
import time
import types
from collections.abc import Iterator

import pytest

from stt_cli import cli, config
from stt_cli._errors import EXIT_INTERRUPTED
from stt_cli.commands import mic
from stt_cli.live import mic_service, permissions
from stt_cli.live.mic_service import AUTOSTART_TOKEN, SERVE_TOKEN

_REAL_SPAWN = mic._spawn_autostart_worker


@pytest.fixture
def first_run(tmp_path, monkeypatch, autostart_hook) -> list[str]:
    """A Mac with the daemon's service library installed and nothing decided yet, where
    starting the worker only records that it was started."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))  # no LaunchAgent, no stt.app of the real user's
    monkeypatch.setattr(mic.sys, "platform", "darwin")
    monkeypatch.setattr(mic, "_service_lib_installed", lambda: True)
    monkeypatch.setattr(permissions, "principal_name", lambda: "'python3.14'")
    started: list[str] = []
    monkeypatch.setattr(mic, "_spawn_autostart_worker", lambda: started.append("worker"))
    return started


def _attempts() -> mic._AutostartAttempts:
    return mic._AutostartAttempts.read()


def _autostart_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if "background dictation daemon" in line]


# ── the first run ─────────────────────────────────────────────────────────────────────


def test_the_first_stt_command_starts_the_worker_once_and_names_the_toggle(
    first_run, capsys, monkeypatch
) -> None:
    # No Swift compiler, so the worker cannot build `stt.app`: the interpreter is the principal.
    monkeypatch.setattr("stt_cli.live.helper.can_build", lambda: False)
    assert cli.main(["config", "list"]) == 0

    assert first_run == ["worker"]
    lines = _autostart_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert lines[0].startswith("stt: turning on the background dictation daemon")
    assert (
        "turn on 'python3.14' in System Settings > Privacy & Security > Accessibility" in lines[0]
    )
    assert "`stt mic disable` turns this off" in lines[0]
    assert _attempts().count == 1

    # The worker has not answered yet: the next command must not start a second one.
    assert cli.main(["config", "list"]) == 0
    assert first_run == ["worker"]
    assert _autostart_lines(capsys.readouterr().err) == []


def test_the_first_run_names_stt_when_the_worker_will_build_the_helper(
    first_run, capsys, monkeypatch
) -> None:
    """The worker builds `stt.app` before enabling (#99), so macOS then asks for "stt" —
    naming the interpreter would send the person looking for a row that is not there."""
    monkeypatch.setattr("stt_cli.live.helper.can_build", lambda: True)

    assert cli.main(["config", "list"]) == 0

    (line,) = _autostart_lines(capsys.readouterr().err)
    assert "turn on 'stt' in System Settings > Privacy & Security > Accessibility" in line


def test_a_daemon_enabled_before_the_helper_is_still_named_as_the_interpreter(
    first_run, capsys, monkeypatch, tmp_path
) -> None:
    """The worker leaves an already-installed job alone, so a daemon enabled before stt.app
    existed keeps running as Python — and that is the row macOS lists, buildable or not."""
    import plistlib

    monkeypatch.setattr("stt_cli.live.helper.can_build", lambda: True)
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    job = {"ProgramArguments": ["/opt/homebrew/bin/python3.14", "stt", "mic", "__serve"]}
    (agents / "com.agenttools.stt.mic.plist").write_bytes(plistlib.dumps(job))

    assert cli.main(["config", "list"]) == 0

    (line,) = _autostart_lines(capsys.readouterr().err)
    assert "turn on 'python3.14' in System Settings" in line


def test_a_closed_standard_error_does_not_turn_the_commands_exit_code_into_a_traceback(
    first_run, monkeypatch
) -> None:
    """`stt doctor 2>&1 | head`: the pipe is gone by the time the note is printed, and
    `guard` has already settled the command's own exit code."""

    def closed(message: str, *, quiet: bool = False) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr("stt_cli.live.status.note", closed)
    monkeypatch.setattr(cli, "_dispatch", lambda args: 0)

    assert cli.main(["doctor"]) == 0
    assert first_run == ["worker"]


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "list"],
        ["doctor"],
        ["rec.m4a"],
        ["mic", "--check"],
        ["mic", "--device", "1"],
    ],
)
def test_any_other_command_is_a_first_run(first_run, monkeypatch, argv) -> None:
    monkeypatch.setattr(cli, "_dispatch", lambda args: 0)
    cli.main(argv)
    assert first_run == ["worker"]


def test_a_command_that_failed_is_still_a_first_run(first_run, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_dispatch", lambda args: 2)
    assert cli.main(["rec.m4a"]) == 2
    assert first_run == ["worker"]


def test_a_mistyped_command_is_still_a_first_run(first_run, capsys) -> None:
    """A deliberate decision: whoever typed `stt doctorr` has started using stt."""
    assert cli.main(["doctorr"]) == 4
    assert first_run == ["worker"]
    assert len(_autostart_lines(capsys.readouterr().err)) == 1


@pytest.mark.parametrize(
    ("argv", "quiet"),
    [
        (["rec.m4a", "-q"], True),
        (["rec.m4a", "--quiet"], True),
        (["rec.m4a", "--qui"], True),
        (["rec.m4a", "-qo", "out.txt"], True),
        (["mic", "--check", "-q"], True),
        (["rec.m4a", "-v"], False),
        (["rec.m4a", "-l", "q"], False),
        (["--", "-q"], False),
    ],
)
def test_quiet_drops_the_line_but_still_starts_the_worker(
    first_run, monkeypatch, capsys, argv, quiet
) -> None:
    monkeypatch.setattr(cli, "_dispatch", lambda args: 0)
    assert cli.main(argv) == 0
    assert first_run == ["worker"]
    assert _attempts().count == 1
    assert len(_autostart_lines(capsys.readouterr().err)) == (0 if quiet else 1)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--help"],
        ["-h"],
        ["help"],
        ["--version"],
        ["-V"],
        ["version"],
        ["mic"],
        *[["mic", word] for word in ("run", "start", "stop", "status", "enable", "disable")],
        ["mic", SERVE_TOKEN],
        ["mic", AUTOSTART_TOKEN],
    ],
)
def test_asking_what_stt_is_or_managing_the_daemon_is_not_a_first_run(
    first_run, monkeypatch, capsys, argv
) -> None:
    monkeypatch.setattr(cli, "_dispatch", lambda args: 0)
    cli.main(argv)
    assert first_run == []
    assert _autostart_lines(capsys.readouterr().err) == []


def test_a_command_interrupted_with_ctrl_c_is_not_a_first_run(first_run, monkeypatch) -> None:
    def interrupted(args: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_dispatch", interrupted)
    assert cli.main(["rec.m4a"]) == EXIT_INTERRUPTED
    assert first_run == []


@pytest.mark.parametrize("argv", [["mic", "--help"], ["config", "--no-such-option"]])
def test_a_commands_own_help_or_usage_error_is_not_a_first_run(first_run, capsys, argv) -> None:
    """Both leave through argparse's SystemExit, which neither `guard` nor `cli.main` may
    catch on the way to the hook."""
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert first_run == []
    assert _autostart_lines(capsys.readouterr().err) == []


def test_nothing_happens_off_macos(first_run, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mic.sys, "platform", "linux")
    assert cli.main(["config", "list"]) == 0
    assert first_run == []
    assert not (tmp_path / mic._AUTOSTART_ATTEMPTS).exists()


def test_without_the_service_library_nothing_is_stamped_started_or_printed(
    first_run, monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.setattr(mic, "_service_lib_installed", lambda: False)
    assert cli.main(["config", "list"]) == 0
    assert first_run == []
    assert not (tmp_path / mic._AUTOSTART_ATTEMPTS).exists()
    assert _autostart_lines(capsys.readouterr().err) == []


# ── a decided record ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("decided", [True, False])
def test_a_decided_record_takes_no_lock_starts_nothing_and_runs_no_launchctl(
    first_run, monkeypatch, capsys, tmp_path, decided
) -> None:
    """Criterion 3 of #105. The conftest guard fails this test at teardown if anything ran
    `launchctl`; the lock and the daemon manager are watched here directly."""
    config.save_setting("mic_daemon_autostart", decided)
    touched: list[str] = []

    @contextlib.contextmanager
    def watched_lock(target: object) -> Iterator[None]:
        touched.append("lock")
        yield

    def watched_manager(ns: object) -> object:
        touched.append("manager")
        raise AssertionError("a decided record must not build a daemon manager")

    def watched_probe(label: str) -> None:
        touched.append("probe")

    monkeypatch.setattr(config, "_editing_the_settings", watched_lock)
    monkeypatch.setattr(mic, "_manager", watched_manager)
    monkeypatch.setattr(mic_service, "probe_launchd", watched_probe)

    assert cli.main(["config", "list"]) == 0

    assert touched == []
    assert first_run == []
    assert not (tmp_path / mic._AUTOSTART_ATTEMPTS).exists()
    assert _autostart_lines(capsys.readouterr().err) == []


def _fake_service_lib(monkeypatch) -> None:
    """Just enough `agenttools_service` for `stt mic enable`/`disable` to run and succeed
    without a real launchd."""

    def add_service_subcommands(subs, *, manager_factory, service_name) -> None:
        for name in ("run", "start", "status", "stop", "enable", "disable"):
            subs.add_parser(name)

    fake = types.ModuleType("agenttools_service")
    fake.add_service_subcommands = add_service_subcommands
    fake.dispatch = lambda ns, on_no_subcommand=None: 0
    monkeypatch.setitem(sys.modules, "agenttools_service", fake)


@pytest.mark.parametrize("before", [None, True], ids=["first-command", "after-autostart"])
def test_an_explicit_disable_is_never_overridden_by_a_later_command(
    first_run, monkeypatch, before
) -> None:
    """Criterion 2 of #105: `stt mic disable` as the very first command, and after an
    earlier autostart had turned the daemon on."""
    if before is not None:
        config.save_setting("mic_daemon_autostart", before)
    _fake_service_lib(monkeypatch)

    assert cli.main(["mic", "disable"]) == 0
    monkeypatch.setattr(cli, "_dispatch", lambda args: 0)
    for argv in (["config", "list"], ["mic", "--check"], ["doctor"]):
        cli.main(argv)

    assert first_run == []
    assert config.load_settings().mic_daemon_autostart is False


def test_a_disable_that_lands_before_the_worker_runs_still_wins(first_run, monkeypatch) -> None:
    """The worker starts detached and may run a moment after the command that started it;
    a `stt mic disable` typed in that moment must not be undone by it."""
    assert cli.main(["config", "list"]) == 0
    assert first_run == ["worker"]
    config.save_setting("mic_daemon_autostart", False)

    calls: list[str] = []

    class _Manager:
        service = argparse.Namespace(
            label="com.agenttools.stt.mic", logfile=config.app_home() / "mic.log"
        )

        def status(self) -> object:
            calls.append("status")
            return argparse.Namespace(enabled=False, running=False)

        def enable(self) -> None:
            calls.append("enable")

    monkeypatch.setattr(mic, "_manager", lambda ns: _Manager())

    assert mic._autostart_worker() == 0

    assert calls == []
    assert config.load_settings().mic_daemon_autostart is False


# ── the backoff ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("count", "elapsed", "due"),
    [
        (0, 0.0, True),
        (1, 599.0, False),
        (1, 600.0, True),
        (2, 1199.0, False),
        (2, 1200.0, True),
        (3, 2399.0, False),
        (3, 2400.0, True),
        (40, 86399.0, False),
        (40, 86400.0, True),
        (1, -5.0, True),
    ],
    ids=[
        "first-always",
        "ten-minutes-not-yet",
        "ten-minutes",
        "doubled-not-yet",
        "doubled",
        "doubled-again-not-yet",
        "doubled-again",
        "capped-not-yet",
        "capped-at-a-day",
        "clock-set-back",
    ],
)
def test_the_backoff_starts_at_ten_minutes_doubles_and_stops_at_a_day(count, elapsed, due) -> None:
    requested = 1_000_000.0
    attempts = mic._AutostartAttempts(count=count, requested=requested, finished=requested)
    assert attempts.due(requested + elapsed) is due


@pytest.mark.parametrize("record", [None, config.MIC_AUTOSTART_RETRY])
def test_an_attempt_that_did_not_confirm_is_retried_only_after_the_backoff(
    first_run, capsys, record
) -> None:
    if record is not None:
        config.save_setting("mic_daemon_autostart", record)
    now = time.time()
    mic._AutostartAttempts(count=1, requested=now - 590, finished=now - 585).write()

    assert cli.main(["config", "list"]) == 0
    assert first_run == []
    assert _autostart_lines(capsys.readouterr().err) == []

    mic._AutostartAttempts(count=1, requested=now - 610, finished=now - 605).write()
    assert cli.main(["config", "list"]) == 0
    assert first_run == ["worker"]
    lines = _autostart_lines(capsys.readouterr().err)
    assert len(lines) == 1 and "trying again to turn on" in lines[0]
    assert _attempts().count == 2


def test_an_impossible_attempts_record_counts_as_no_record(first_run, tmp_path) -> None:
    """A negative count, hand-edited or half-written, must not make every later check
    raise and the daemon silently never come on."""
    (tmp_path / mic._AUTOSTART_ATTEMPTS).write_text(
        '{"count": -1, "requested": 0.0, "finished": 0.0}\n', encoding="utf-8"
    )
    assert cli.main(["config", "list"]) == 0
    assert first_run == ["worker"]
    assert _attempts().count == 1


def test_a_worker_that_could_not_be_started_costs_no_backoff(
    first_run, monkeypatch, capsys
) -> None:
    """A fork refused or an interpreter gone is no attempt: the next command tries again
    at once, rather than ten minutes later for an attempt that never ran."""

    def refused() -> None:
        raise BlockingIOError(35, "Resource temporarily unavailable")

    monkeypatch.setattr(mic, "_spawn_autostart_worker", refused)
    assert cli.main(["config", "list"]) == 0
    assert _autostart_lines(capsys.readouterr().err) == [], "nothing started, nothing said"
    assert _attempts().count == 0

    monkeypatch.setattr(mic, "_spawn_autostart_worker", lambda: first_run.append("worker"))
    assert cli.main(["config", "list"]) == 0
    assert first_run == ["worker"]
    assert _attempts().count == 1


# ── the worker ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def worker(tmp_path, monkeypatch) -> types.SimpleNamespace:
    """`_autostart_worker` with the enable-probe-record step replaced by a count of its
    runs, recording a verified daemon when the test sets `confirms`, `"retry"` otherwise."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    seen = types.SimpleNamespace(attempts=0, confirms=False)
    service = argparse.Namespace(label="com.agenttools.stt.mic", logfile=tmp_path / "mic.log")
    monkeypatch.setattr(mic, "_manager", lambda ns: argparse.Namespace(service=service))

    def attempt(*, quiet: bool) -> None:
        seen.attempts += 1
        print("stt: what the attempt had to say", file=sys.stderr)
        verdict = True if seen.confirms else config.MIC_AUTOSTART_RETRY
        config.save_setting("mic_daemon_autostart", verdict)

    monkeypatch.setattr(mic, "_maybe_autostart_daemon", attempt)
    return seen


def test_two_workers_for_one_request_attempt_once(worker, tmp_path) -> None:
    mic._AutostartAttempts(count=1, requested=time.time() - 1).write()

    assert mic._autostart_worker() == 0
    assert mic._autostart_worker() == 0

    assert worker.attempts == 1
    assert _attempts().finished >= _attempts().requested
    assert "what the attempt had to say" in (tmp_path / "mic.log").read_text(encoding="utf-8")


def test_a_worker_with_no_request_does_nothing(worker) -> None:
    assert mic._autostart_worker() == 0
    assert worker.attempts == 0


def test_an_unconfirmed_attempt_keeps_the_count_and_a_confirmed_one_resets_it(worker) -> None:
    mic._AutostartAttempts(count=3, requested=time.time() - 1).write()
    assert mic._autostart_worker() == 0
    assert _attempts().count == 3

    worker.confirms = True
    mic._AutostartAttempts(count=4, requested=time.time() - 1).write()
    assert mic._autostart_worker() == 0
    assert _attempts().count == 0
    assert config.load_settings().mic_daemon_autostart is True


def _manager_that(monkeypatch, tmp_path, *, builds: bool) -> None:
    """The daemon's manager as a broken but present `agenttools_service` leaves it: it
    cannot be built at all, or it can and every question to launchd fails."""

    def status() -> None:
        raise RuntimeError("launchd did not answer")

    def manager(ns: argparse.Namespace) -> argparse.Namespace:
        if not builds:
            raise RuntimeError("agenttools_service is broken")
        service = argparse.Namespace(label="com.agenttools.stt.mic", logfile=tmp_path / "mic.log")
        return argparse.Namespace(service=service, status=status)

    monkeypatch.setattr(mic, "_manager", manager)


@pytest.mark.parametrize("builds", [False, True], ids=["manager-fails", "attempt-swallows"])
def test_a_worker_that_could_not_attempt_keeps_the_count_and_decides_nothing(
    tmp_path, monkeypatch, builds
) -> None:
    """Nothing was enabled and nothing was recorded, so the backoff must go on from where
    it was — not start over as if a daemon had been verified — and the request must be
    marked finished, or no later worker would ever answer the next one."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    _manager_that(monkeypatch, tmp_path, builds=builds)
    mic._AutostartAttempts(count=2, requested=time.time() - 1).write()

    with contextlib.suppress(RuntimeError):
        mic._autostart_worker()

    assert _attempts().count == 2
    assert _attempts().finished >= _attempts().requested
    assert config.load_settings().mic_daemon_autostart is None


def test_the_worker_never_imports_from_the_directory_the_command_ran_in(
    monkeypatch, tmp_path
) -> None:
    """An installed stt's worker is `python -m stt_cli.cli`; `-m` would import a checkout or
    a planted package in the caller's directory first. A module planted there must not be
    importable with the environment the worker is started with."""
    started: list[dict] = []
    with monkeypatch.context() as patched:
        patched.setattr(subprocess, "Popen", lambda argv, **kwargs: started.append(kwargs))
        _REAL_SPAWN()
    (tmp_path / "planted_stt_probe.py").write_text("", encoding="utf-8")

    found = subprocess.run(
        [sys.executable, "-m", "planted_stt_probe"],
        cwd=tmp_path,
        env=started[0]["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert found.returncode != 0 and "No module named planted_stt_probe" in found.stderr


# ── the guard in conftest.py ──────────────────────────────────────────────────────────


def test_the_guard_refuses_launchctl_and_the_worker(_never_the_real_launchd) -> None:
    """Criterion 4 of #105. Neither command can do anything if the guard is gone: the first
    does not exist and the second is an interpreter told to do nothing, so without the
    guard this test fails instead of reaching launchd."""
    with pytest.raises(AssertionError, match="launchctl"):
        subprocess.Popen(["/nonexistent/launchctl", "bootout", "gui/0/com.agenttools.stt.mic"])
    with pytest.raises(AssertionError, match=AUTOSTART_TOKEN):
        subprocess.run([sys.executable, "-c", "pass", "mic", AUTOSTART_TOKEN], check=False)

    assert [argv[-1] for argv in _never_the_real_launchd] == [
        "gui/0/com.agenttools.stt.mic",
        AUTOSTART_TOKEN,
    ]
    _never_the_real_launchd.clear()  # both were this test's own, and expected


def test_the_guard_refuses_launchctl_given_as_one_command_line(_never_the_real_launchd) -> None:
    """`Popen("launchctl ...", shell=True)`: the program is the first word of the string,
    not the basename of all of it. A program that does not exist, so nothing can start if
    the guard misses it."""
    with pytest.raises(AssertionError, match="launchctl"):
        subprocess.Popen("/nonexistent/launchctl bootout gui/0/com.agenttools.stt.mic", shell=True)

    assert len(_never_the_real_launchd) == 1
    _never_the_real_launchd.clear()  # this test's own, and expected


def test_the_guard_sees_a_worker_start_the_code_swallowed(
    first_run, monkeypatch, capsys, _never_the_real_launchd
) -> None:
    """`autostart_after` swallows every failure, the guard's refusal included, so the
    guard records the attempt and fails the test at teardown. Pointed at a program that
    does not exist, so nothing can start if the guard is gone either."""
    monkeypatch.setattr(mic, "_spawn_autostart_worker", _REAL_SPAWN)
    monkeypatch.setattr(
        mic_service, "autostart_worker_argv", lambda: ["/nonexistent/stt", "mic", AUTOSTART_TOKEN]
    )

    assert cli.main(["config", "list"]) == 0

    assert _never_the_real_launchd == [["/nonexistent/stt", "mic", AUTOSTART_TOKEN]]
    assert _autostart_lines(capsys.readouterr().err) == [], "nothing started, nothing said"
    _never_the_real_launchd.clear()  # this test's own, and expected
