"""A machine-wide cap on loaded speech models, and no model process outliving its stt (#106).

On 2026-09-29 parallel agents started stt over and over; every run loaded its own
large-v3-turbo onto Metal, thirteen of them wired 24.65 of 25.77 GB, and the Mac hard-reset.
The orphans kept coming after the process that started them had crashed.

Nothing here loads a model, touches a GPU or starts anything heavier than a sleeping Python
script: every whisper binary is `FAKE_WHISPER`, which writes down when it started and ended.
Tests that need a separate "stt" to kill run `CALLER` in a subprocess — the real
`WhisperCppBackend.decode` path, pointed at the fake checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from stt_cli import config, model_gate, proc, resources, vad
from stt_cli._errors import ProcessTimeout, ResourceError, UsageError
from stt_cli.backends import whispercpp
from stt_cli.backends.base import DecodeRequest

REPO = Path(__file__).resolve().parents[1]

FAKE_WHISPER = """\
import json, os, sys, time
from pathlib import Path

log = Path(os.environ["FAKE_WHISPER_LOG"])


def note(event):
    with log.open("a") as out:
        out.write(f"{event} {os.getpid()} {time.time():.4f}\\n")


note("start")
if os.environ.get("FAKE_WHISPER_HANG"):
    while True:
        time.sleep(60)
time.sleep(float(os.environ.get("FAKE_WHISPER_SECONDS", "0")))
if "-of" in sys.argv:
    Path(sys.argv[sys.argv.index("-of") + 1] + ".json").write_text('{"transcription": []}')
note("end")
"""

# A stand-in for `stt`: one decode through the real backend, in a process of its own.
CALLER = """\
import asyncio, sys
from pathlib import Path
from stt_cli.backends.base import DecodeRequest
from stt_cli.backends.whispercpp import WhisperCppBackend

backend = WhisperCppBackend(sys.argv[1])
asyncio.run(backend.decode(DecodeRequest(wav=Path(sys.argv[2]), model="large-v3-turbo")))
"""

# The same, for a child that holds no model slot: only the signal handling in `proc` stands
# between it and becoming an orphan.
PLAIN_CALLER = """\
import asyncio, sys
from stt_cli import proc

asyncio.run(proc.run([sys.argv[1]], timeout=600))
"""

# A whisper-server that answers HTTP and loads nothing.
FAKE_SERVER = """\
import http.server, sys

port = int(sys.argv[sys.argv.index("--port") + 1])


class Answer(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", port), Answer).serve_forever()
"""


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A whisper.cpp checkout whose binaries are `FAKE_WHISPER` (`FAKE_SERVER` for
    whisper-server), and 1 KB "models"."""
    root = tmp_path / "whisper.cpp"
    binaries = root / "build" / "bin"
    binaries.mkdir(parents=True)
    for name in ("whisper-cli", "whisper-vad-speech-segments"):
        exe = binaries / name
        exe.write_text(f"#!{sys.executable}\n{FAKE_WHISPER}")
        exe.chmod(0o755)
    server = binaries / "whisper-server"
    server.write_text(f"#!{sys.executable}\n{FAKE_SERVER}")
    server.chmod(0o755)
    (root / "models").mkdir()
    for name in ("large-v3-turbo", "base"):
        (root / "models" / f"ggml-{name}.bin").write_bytes(b"\0" * 1024)
    monkeypatch.setenv("STT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_WHISPER_LOG", str(tmp_path / "whisper.log"))
    return root


def _wav(path: Path, seconds: float) -> Path:
    """Silence in the format every engine is handed: 16 kHz mono 16-bit PCM."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(b"\0\0" * int(16_000 * seconds))
    return path


def _events(log: Path) -> list[tuple[str, int, float]]:
    if not log.exists():
        return []
    rows = [line.split() for line in log.read_text().splitlines() if line.strip()]
    return [(kind, int(pid), float(at)) for kind, pid, at in rows]


def _first_start(log: Path, *, within: float = 20.0) -> int:
    """The pid of the first fake whisper to start, once it has."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        started = [pid for kind, pid, _ in _events(log) if kind == "start"]
        if started:
            return started[0]
        time.sleep(0.02)
    raise AssertionError(f"no fake whisper started within {within}s")


def _most_at_once(log: Path) -> int:
    """The largest number of fake whispers that were running at the same moment."""
    changes = sorted(
        (at, 1 if kind == "start" else -1) for kind, _, at in _events(log)
    )  # an end sorts before a start at the same instant: back-to-back is not overlap
    running = most = 0
    for _, step in changes:
        running += step
        most = max(most, running)
    return most


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or a zombie waiting for its parent to reap it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _left_running(binary: Path, *, within: float) -> list[int]:
    """Processes started from ``binary`` that are still running after up to ``within``s.

    Found by command line rather than by the pid the fake writes down: a limit that fires
    before a loaded machine has even started the fake leaves no pid to ask about, and
    "nothing left running" is the claim either way.
    """
    deadline = time.monotonic() + within
    while True:
        found = subprocess.run(
            ["pgrep", "-f", str(binary)], capture_output=True, text=True, check=False
        ).stdout.split()
        alive = [int(pid) for pid in found if _alive(int(pid))]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.05)


@contextlib.contextmanager
def _reaping(*pids: int):
    """Whatever a failed assertion leaves running is killed, so a red test leaks nothing."""
    try:
        yield
    finally:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


def _stt(script: str, *args: str, **env: str) -> subprocess.Popen[str]:
    """Start a stand-in `stt` in a process of its own."""
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        env={**os.environ, "PYTHONPATH": str(REPO), **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _hold_slot(index: int, note: str = "pid=4242 argv=somebody else\n") -> int:
    """Take a slot the way another process would, through a descriptor of its own."""
    pool = model_gate.directory()
    pool.mkdir(parents=True, exist_ok=True)
    fd = os.open(pool / f"slot-{index}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, note.encode())
    return fd


async def _enter(model_bytes: int = 1024) -> None:
    async with model_gate.slot(model_bytes, what="a test model"):
        pass


# ── how many ──────────────────────────────────────────────────────────────────


def test_a_24_gb_mac_holds_at_most_two_large_v3_turbo_at_once(monkeypatch) -> None:
    """The machine that crashed: hw.memsize 25,769,803,776 and a 1,624,555,275-byte model."""
    monkeypatch.setattr(resources, "total_ram", lambda: 25_769_803_776)
    assert model_gate.count(1_624_555_275) == 2
    assert model_gate.count(3_095_033_483) == 1, "large-v3 is twice the size: one at a time"
    # A small model gets more, but a bounded more: the Silero VAD's 0.9 MB is not a licence
    # for thousands of processes.
    assert model_gate.count(885_098) == model_gate.MAX_SLOTS


def test_unknown_memory_means_one_model_at_a_time(monkeypatch) -> None:
    monkeypatch.setattr(resources, "total_ram", lambda: 0)
    assert model_gate.count(1_624_555_275) == 1


def test_a_linux_machine_counts_its_memory_too(monkeypatch, tmp_path) -> None:
    """Linux has no hw.memsize. Asking sysctl for it made every Linux machine a one-slot
    machine, whatever it had."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       33554432 kB\nMemFree:         1048576 kB\n")
    monkeypatch.setattr(resources, "MEMINFO", meminfo)
    assert resources.total_ram() == 32 * 1024**3
    assert model_gate.count(1_624_555_275) == 3


def test_an_explicit_slot_count_wins_and_a_nonsense_one_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(resources, "total_ram", lambda: 25_769_803_776)
    monkeypatch.setenv("RIG_GPU_SLOTS", "5")
    assert model_gate.count(1_624_555_275) == 5
    for bad in ("0", "-1", "two"):
        monkeypatch.setenv("RIG_GPU_SLOTS", bad)
        with pytest.raises(UsageError):
            model_gate.count(1_624_555_275)


# ── the gate across processes ─────────────────────────────────────────────────


def test_parallel_runs_never_hold_more_models_than_there_are_slots(checkout, tmp_path) -> None:
    """The crash, in miniature: several stt runs at once. Two slots, never a third model."""
    wav = _wav(tmp_path / "clip.wav", 0.5)
    log = tmp_path / "whisper.log"
    callers = [
        _stt(CALLER, str(checkout), str(wav), RIG_GPU_SLOTS="2", FAKE_WHISPER_SECONDS="1.0")
        for _ in range(4)
    ]
    with _reaping(*(c.pid for c in callers)):
        for caller in callers:
            _, err = caller.communicate(timeout=60)
            assert caller.returncode == 0, err
    assert sum(kind == "end" for kind, _, _ in _events(log)) == 4, "every run was decoded"
    assert _most_at_once(log) <= 2


def test_two_slots_admit_two_models_at_once_and_not_a_third(monkeypatch) -> None:
    """The other half of the cap: it is a limit, not a queue of one."""
    monkeypatch.setenv("RIG_GPU_SLOTS", "2")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.3)

    async def three_at_once() -> None:
        async with model_gate.slot(1024, what="first"), model_gate.slot(1024, what="second"):
            with pytest.raises(ResourceError):
                await _enter()

    asyncio.run(three_at_once())


def test_a_small_model_settles_in_the_high_slots_and_leaves_the_low_ones_to_big_models(
    monkeypatch,
) -> None:
    """Each model probes its slots from the top down: the 0.9 MB VAD, allowed eight slots,
    takes slot 7, so two large-v3-turbo still fit on the 24 GB Mac beside it."""
    monkeypatch.setattr(resources, "total_ram", lambda: 25_769_803_776)
    turbo = 1_624_555_275
    held = [model_gate.attempt(885_098), model_gate.attempt(turbo), model_gate.attempt(turbo)]
    try:
        vad, first, second = held
        assert vad is not None and vad.path.name == f"slot-{model_gate.MAX_SLOTS - 1}.lock"
        assert first is not None and first.path.name == "slot-1.lock"
        assert second is not None and second.path.name == "slot-0.lock"
        assert model_gate.attempt(turbo) is None
    finally:
        for slot in held:
            if slot is not None:
                model_gate.release(slot)


def test_a_run_that_finds_every_slot_taken_says_so_waits_then_fails(monkeypatch, capsys) -> None:
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.6)
    held = _hold_slot(0)
    try:
        started = time.monotonic()
        with pytest.raises(ResourceError) as refused:
            asyncio.run(_enter())
        waited = time.monotonic() - started
    finally:
        os.close(held)
    assert waited >= 0.6, "it gave up without waiting"
    assert "pid=4242" in refused.value.why, "the error names who holds the slots"
    notices = [line for line in capsys.readouterr().err.splitlines() if "slot" in line]
    assert len(notices) == 1, f"one notice while waiting, not one per poll: {notices}"


def test_the_wait_names_the_command_that_holds_each_slot(monkeypatch, capsys) -> None:
    """A pid says nothing. `stt mic run` says the dictation daemon has the slot, and keeps it
    for as long as it runs — on a Mac allowed one large model, that is every decode's wait."""
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.3)
    held = _hold_slot(0, "pid=4242 argv=/Users/someone/.local/bin/stt mic run\n")
    try:
        with pytest.raises(ResourceError) as refused:
            asyncio.run(_enter())
    finally:
        os.close(held)
    assert "pid=4242 stt mic run" in refused.value.why
    assert "pid=4242 stt mic run" in capsys.readouterr().err


def test_a_slot_frees_the_moment_its_holder_is_killed(monkeypatch) -> None:
    """flock belongs to the kernel: a holder that is SIGKILLed leaves nothing stale behind."""
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    holder = _stt(
        "import asyncio, sys\n"
        "from stt_cli import model_gate\n"
        "async def main():\n"
        "    async with model_gate.slot(1024, what='x'):\n"
        "        print('held', flush=True)\n"
        "        await asyncio.sleep(600)\n"
        "asyncio.run(main())\n"
    )
    with _reaping(holder.pid):
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.3)
        with pytest.raises(ResourceError):
            asyncio.run(_enter())
        holder.kill()
        holder.wait()
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 5.0)
    asyncio.run(_enter())


def test_a_slot_already_held_by_a_wrapper_is_used_rather_than_doubled(monkeypatch) -> None:
    """`agenttools_gpu_slots run -- stt …` takes a slot and says so in RIG_GPU_SLOT_FD. With
    one slot, taking a second would wait for ever on the wrapper's own slot."""
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.3)
    wrapper = _hold_slot(0)
    try:
        monkeypatch.setenv("RIG_GPU_SLOT_FD", str(wrapper))
        asyncio.run(_enter())

        async def two_at_once() -> None:
            async with model_gate.slot(1024, what="first"):
                await _enter()

        with pytest.raises(ResourceError):
            asyncio.run(two_at_once())
    finally:
        os.close(wrapper)


def test_a_run_waiting_behind_the_wrapper_slot_takes_it_when_it_is_given_back(monkeypatch) -> None:
    """Two decodes at once under a one-slot wrapper — the cross-check variants do this. The
    second waits for the first to hand the wrapper's slot back, not for the pool, where the
    only slot is the wrapper's own and never comes free."""
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 3.0)
    wrapper = _hold_slot(0)
    try:
        monkeypatch.setenv("RIG_GPU_SLOT_FD", str(wrapper))

        async def first() -> None:
            async with model_gate.slot(1024, what="first"):
                await asyncio.sleep(0.3)

        async def one_after_the_other() -> None:
            await asyncio.gather(first(), _enter())

        started = time.monotonic()
        asyncio.run(one_after_the_other())
        assert time.monotonic() - started < 2.0, "the second waited out most of its limit"
    finally:
        os.close(wrapper)


def test_a_stale_slot_descriptor_does_not_open_the_gate(monkeypatch, tmp_path) -> None:
    """An inherited RIG_GPU_SLOT_FD that names some other open file is not a slot."""
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.3)
    held = _hold_slot(0)
    unrelated = os.open(tmp_path / "unrelated", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        monkeypatch.setenv("RIG_GPU_SLOT_FD", str(unrelated))
        with pytest.raises(ResourceError):
            asyncio.run(_enter())
    finally:
        os.close(held)
        os.close(unrelated)


def test_a_stale_slot_descriptor_that_now_names_our_own_slot_does_not_double_it(
    monkeypatch,
) -> None:
    """A stale RIG_GPU_SLOT_FD names a closed descriptor, and the next descriptor stt opens
    gets that number — for its own slot. That slot is still one slot, not a wrapper's slot
    to load a second model under."""
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.3)

    async def two_at_once() -> None:
        closed = os.open(os.devnull, os.O_RDONLY)
        os.close(closed)
        monkeypatch.setenv("RIG_GPU_SLOT_FD", str(closed))
        async with model_gate.slot(1024, what="first") as held:
            assert held.fd == closed, "the scenario: our own slot got the stale number"
            with pytest.raises(ResourceError):
                await _enter()

    asyncio.run(two_at_once())


# ── nothing outlives stt ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sig", [signal.SIGKILL, signal.SIGTERM, signal.SIGHUP, signal.SIGINT], ids=lambda s: s.name
)
def test_no_model_process_outlives_its_stt(checkout, tmp_path, sig) -> None:
    wav = _wav(tmp_path / "clip.wav", 0.5)
    caller = _stt(CALLER, str(checkout), str(wav), FAKE_WHISPER_HANG="1")
    with _reaping(caller.pid):
        whisper = _first_start(tmp_path / "whisper.log")
        with _reaping(whisper):
            caller.send_signal(sig)
            caller.communicate(timeout=20)
            assert _gone_within(whisper, 5.0), f"whisper-cli {whisper} survived {sig.name}"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP], ids=lambda s: s.name)
def test_a_terminated_stt_ends_its_children_before_it_goes(tmp_path, sig) -> None:
    """The layer that does not depend on the tether: stt's own handler kills the child's
    whole process group on the way out, and stt still exits the way that signal means."""
    log = tmp_path / "whisper.log"
    sleeper = tmp_path / "sleeper"
    sleeper.write_text(f"#!{sys.executable}\n{FAKE_WHISPER}")
    sleeper.chmod(0o755)
    caller = _stt(PLAIN_CALLER, str(sleeper), FAKE_WHISPER_LOG=str(log), FAKE_WHISPER_HANG="1")
    with _reaping(caller.pid):
        child = _first_start(log)
        with _reaping(child):
            caller.send_signal(sig)
            caller.communicate(timeout=20)
            assert _gone_within(child, 5.0), f"child {child} survived {sig.name}"
    assert caller.returncode == 128 + sig


def test_cancelling_a_decode_kills_its_model_process(checkout, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_WHISPER_HANG", "1")
    backend = whispercpp.WhisperCppBackend(str(checkout))
    request = DecodeRequest(wav=_wav(tmp_path / "clip.wav", 0.5), model="large-v3-turbo")

    async def cancel_mid_decode() -> int:
        decoding = asyncio.create_task(backend.decode(request))
        whisper = await asyncio.to_thread(_first_start, tmp_path / "whisper.log")
        decoding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await decoding
        return whisper

    whisper = asyncio.run(cancel_mid_decode())
    with _reaping(whisper):
        assert _gone_within(whisper, 5.0)


def test_an_orphaned_model_process_still_holds_its_slot(checkout, tmp_path, monkeypatch) -> None:
    """The last layer. Take away both of the others — the tether's watcher and any chance
    for stt to clean up — and the orphan still counts against the pool until it exits,
    because it holds the slot's lock itself."""
    wav = _wav(tmp_path / "clip.wav", 0.5)
    caller = _stt(CALLER, str(checkout), str(wav), RIG_GPU_SLOTS="1", FAKE_WHISPER_HANG="1")
    with _reaping(caller.pid):
        whisper = _first_start(tmp_path / "whisper.log")
        with _reaping(whisper):
            watchers = subprocess.run(
                ["pgrep", "-P", str(whisper)], capture_output=True, text=True, check=False
            ).stdout.split()
            assert watchers, "a tethered model process has a watcher beside it"
            for watcher in watchers:
                os.kill(int(watcher), signal.SIGKILL)
            caller.kill()
            caller.communicate(timeout=20)
            assert _alive(whisper), "with every other layer gone the orphan is still running"

            monkeypatch.setenv("RIG_GPU_SLOTS", "1")
            monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.3)
            with pytest.raises(ResourceError):
                asyncio.run(_enter())

            os.kill(whisper, signal.SIGKILL)
            assert _gone_within(whisper, 5.0)
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 5.0)
    asyncio.run(_enter())


def _mic_args(**overrides: object) -> argparse.Namespace:
    given: dict[str, object] = {
        "model": "large-v3-turbo",
        "draft": True,
        "draft_model": "base",
        "language": "en",
        "quiet": False,
        "threads": 0,
    }
    return argparse.Namespace(**{**given, **overrides})


@pytest.mark.skipif(proc.which("lsof") is None, reason="whisper-server's start asks lsof")
def test_a_dictation_holds_its_model_slots_until_it_ends(checkout, monkeypatch) -> None:
    """`stt mic` keeps whisper-server models loaded for its whole life, so they count."""
    from stt_cli.live import dictation

    monkeypatch.setenv("RIG_GPU_SLOTS", "2")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.3)
    settings = config.Settings(whispercpp_root=str(checkout))

    async def dictate_then_end() -> None:
        async with dictation._models(_mic_args(), settings) as models:
            assert models.draft is not None
            with pytest.raises(ResourceError):
                await _enter()
        await _enter()

    asyncio.run(dictate_then_end())


@pytest.mark.skipif(proc.which("lsof") is None, reason="whisper-server's start asks lsof")
def test_a_dictation_with_no_second_slot_starts_without_its_draft(
    checkout, monkeypatch, capsys
) -> None:
    """The draft is a convenience. With one slot, waiting for a second would be waiting on
    this same session's settled model, which it keeps until it ends: half an hour, then no
    dictation at all."""
    from stt_cli.live import dictation

    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 0.5)
    settings = config.Settings(whispercpp_root=str(checkout))

    async def dictate() -> None:
        async with dictation._models(_mic_args(), settings) as models:
            assert models.draft is None
            assert models.settled is not None

    asyncio.run(dictate())
    assert "base" in capsys.readouterr().err, "and it says the fast first guess is off"


def test_a_slot_is_free_once_its_model_process_lets_go_of_it(monkeypatch) -> None:
    """The tether's watcher keeps no copy of the slot's lock. When stt and the model process
    have both let go, the slot is free — even with the watcher still watching — instead of
    making the next back-to-back decode wait and say every slot is in use."""
    monkeypatch.setenv("RIG_GPU_SLOTS", "1")
    monkeypatch.setattr(model_gate, "WAIT_SECONDS", 1.0)

    async def let_go_while_running() -> None:
        held = await model_gate.acquire(1024, what="first")
        code = f"import os, time; os.close({held.fd}); print('let go', flush=True); time.sleep(30)"
        program = await proc.start([sys.executable, "-c", code], hold=held)
        try:
            assert program.stdout is not None
            assert (await program.stdout.readline()).strip() == b"let go"
            model_gate.release(held)
            await _enter()
        finally:
            await proc.end(program, grace=1.0)

    asyncio.run(let_go_while_running())


def test_a_group_kill_of_stt_reaches_the_children_that_hold_no_model(tmp_path) -> None:
    """ffmpeg, an LLM CLI, the diarization worker are not tethered, so they stay in stt's
    own process group, where whatever ends that group — Ctrl-\\ in a terminal, a harness's
    killpg on a timeout — ends them with it."""
    log = tmp_path / "whisper.log"
    sleeper = tmp_path / "sleeper"
    sleeper.write_text(f"#!{sys.executable}\n{FAKE_WHISPER}")
    sleeper.chmod(0o755)
    caller = subprocess.Popen(
        [sys.executable, "-c", PLAIN_CALLER, str(sleeper)],
        env={
            **os.environ,
            "PYTHONPATH": str(REPO),
            "FAKE_WHISPER_LOG": str(log),
            "FAKE_WHISPER_HANG": "1",
        },
        start_new_session=True,
    )
    with _reaping(caller.pid):
        child = _first_start(log)
        with _reaping(child):
            os.killpg(caller.pid, signal.SIGKILL)
            caller.wait(timeout=20)
            assert _gone_within(child, 5.0), f"child {child} outlived its stt's group"


# ── time limits that fit the audio ────────────────────────────────────────────


def test_a_hung_decode_fails_within_its_budget_and_its_process_is_gone(
    checkout, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_WHISPER_HANG", "1")
    monkeypatch.setattr(proc, "LOAD_SECONDS", 1.0)
    backend = whispercpp.WhisperCppBackend(str(checkout))
    request = DecodeRequest(wav=_wav(tmp_path / "clip.wav", 0.05), model="large-v3-turbo")
    bound = proc.budget(0.05, pace=whispercpp.DECODE_PACE)

    started = time.monotonic()
    with pytest.raises(ProcessTimeout):
        asyncio.run(backend.decode(request))
    took = time.monotonic() - started

    left = _left_running(checkout / "build" / "bin" / "whisper-cli", within=5.0)
    with _reaping(*left):
        assert took < bound + 3.0, f"a {bound:.1f}s budget took {took:.1f}s to fire"
        assert left == []


def test_the_decode_budget_grows_with_the_chunk(checkout, tmp_path, monkeypatch) -> None:
    """The same one-second decode fails on a sliver of audio and finishes on a longer
    chunk: the limit follows the audio, not a flat three hours."""
    monkeypatch.setenv("FAKE_WHISPER_SECONDS", "1.0")
    monkeypatch.setattr(proc, "LOAD_SECONDS", 0.5)
    backend = whispercpp.WhisperCppBackend(str(checkout))

    sliver = DecodeRequest(wav=_wav(tmp_path / "sliver.wav", 0.02), model="large-v3-turbo")
    with pytest.raises(ProcessTimeout):
        asyncio.run(backend.decode(sliver))

    chunk = DecodeRequest(wav=_wav(tmp_path / "chunk.wav", 1.0), model="large-v3-turbo")
    assert asyncio.run(backend.decode(chunk)) == []


def test_a_hung_vad_fails_within_its_budget_and_its_process_is_gone(
    checkout, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_WHISPER_HANG", "1")
    monkeypatch.setattr(proc, "LOAD_SECONDS", 1.0)
    wav = _wav(tmp_path / "clip.wav", 0.5)
    bound = proc.budget(0.5, pace=vad.VAD_PACE)
    detector = checkout / "build" / "bin" / "whisper-vad-speech-segments"

    started = time.monotonic()
    with pytest.raises(ProcessTimeout):
        asyncio.run(
            vad.detect(
                wav,
                0.5,
                mode="silero",
                silero_binary=detector,
                silero_model=checkout / "models" / "ggml-large-v3-turbo.bin",
            )
        )
    took = time.monotonic() - started

    left = _left_running(detector, within=5.0)
    with _reaping(*left):
        assert took < bound + 3.0, f"a {bound:.1f}s budget took {took:.1f}s to fire"
        assert left == []
