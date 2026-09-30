"""proc — one async way to run an external program.

Every heavy thing stt-cli does is another process: ffmpeg, ffprobe, a whisper binary, an
LLM CLI. Funnelling them through one helper buys three things the pipeline depends on:
a missing binary becomes a diagnosed :class:`MissingDependencyError` instead of a bare
``FileNotFoundError``; a hung child dies on a timeout instead of wedging the run forever
(long jobs are the norm here, so no call is ever left unbounded); and because the helper
is ``async``, independent children — several output formats, several decoding variants —
actually run at the same time instead of queueing behind each other.

NOTHING OUTLIVES STT
    On 2026-09-29 whisper-cli processes outlived the process that started them by half an
    hour, each holding a model on the GPU, and helped wire the machine's memory full (#106).
    So every child is ended on every way out of the call that owns it: an error, a timeout,
    a cancellation. While any child runs, SIGTERM and SIGHUP (and SIGINT, where nothing else
    handles it) first kill every child and then end stt the way the signal would have.

    A child that loads a speech model goes further. SIGKILL cannot be caught, so it starts
    through `_tether.py`, which watches stt from outside and takes the child down when stt
    disappears; and it runs in a session — a process group — of its own, and that whole
    group is what gets killed, so nothing the model process started stays behind. Every
    other child stays in stt's own process group, so that whatever ends that group — Ctrl-\\
    in a terminal, a harness's killpg on a timeout — ends the child with it. A session of
    its own without a tether would take it out of reach of both.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

from ._errors import EngineError, MissingDependencyError, ProcessTimeout

if TYPE_CHECKING:
    from .model_gate import Slot

# Generous, but never absent: a wedged ffmpeg or LLM CLI must fail loudly rather than hold
# the run open until someone notices tomorrow. A speech model gets a limit that fits its
# audio instead: see `budget`.
DEFAULT_TIMEOUT = 3 * 60 * 60.0

# What a speech-model process gets before its audio starts to count: reading a 1.6 GB model
# off a cold disk and compiling its Metal kernels, on a machine that may be busy.
LOAD_SECONDS = 120.0

TETHER = Path(__file__).with_name("_tether.py")

_Handler = Callable[[int, FrameType | None], Any] | int | signal.Handlers | None

# Children still running, each with the process group that is killed as a whole to end it:
# its own, for a tethered model process; None for any other child, which runs in stt's.
_running: dict[asyncio.subprocess.Process, int | None] = {}
# The handlers `_end_with_children` stands in for while children run.
_displaced: dict[signal.Signals, _Handler] = {}
_ENDINGS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def budget(audio_seconds: float, *, pace: float) -> float:
    """The time limit for a speech-model process over ``audio_seconds`` of audio.

    LOAD_SECONDS to get going, then ``pace`` seconds of wall time for every second of audio.
    It replaced one flat limit for every chunk — three hours, sized for the longest
    recording — under which a whisper-cli stuck on a few seconds of audio sat on its model
    for 26 minutes without anybody reaping it.
    """
    return LOAD_SECONDS + pace * max(audio_seconds, 0.0)


@dataclass(slots=True)
class Result:
    """What a finished child process left behind."""

    code: int
    stdout: str
    stderr: str
    argv: list[str]

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, lines: int = 12) -> str:
        blob = (self.stderr or self.stdout).strip()
        return "\n".join(blob.splitlines()[-lines:])


def which(binary: str) -> str | None:
    return shutil.which(binary)


def require(binary: str, *, install_hint: str) -> str:
    """Resolve ``binary`` on PATH or explain exactly how to get it."""
    found = shutil.which(binary)
    if found:
        return found
    raise MissingDependencyError(
        what=f"required tool not found: {binary}",
        why=f"{binary} is not on PATH",
        how=install_hint,
    )


async def start(
    argv: list[str],
    *,
    stdin: int | None = asyncio.subprocess.DEVNULL,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    hold: Slot | None = None,
) -> asyncio.subprocess.Process:
    """Launch ``argv``, and keep track of it until it is finished.

    ``hold`` is the speech-model slot (`model_gate`) the child loads its model under. The
    child inherits the slot's lock, so the slot stays taken for exactly as long as the model
    is loaded, and it is tethered: it dies with stt, SIGKILL included (see `_tether.py`). It
    also leads a process group of its own, which is what `end` and every other way out
    kill. A child without ``hold`` runs in stt's own group (see the module's docstring).

    The caller owns the process from here and finishes it with `end`; `run` does both.
    """
    command = list(argv)
    inherit: tuple[int, ...] = ()
    if hold is not None:
        program = shutil.which(command[0])
        if program is None:
            raise _missing(command[0])
        command = [sys.executable, "-I", "-S", str(TETHER), str(os.getpid()), program, *argv[1:]]
        inherit = (hold.fd,)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=str(cwd) if cwd else None,
            env=env,
            start_new_session=hold is not None,
            pass_fds=inherit,
        )
    except FileNotFoundError as exc:
        raise _missing(argv[0]) from exc
    _adopt(process, group=process.pid if hold is not None else None)
    return process


async def run(
    argv: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
    check: bool = False,
    hold: Slot | None = None,
) -> Result:
    """Run ``argv`` to completion, capturing both streams. ``hold``: see `start`."""
    merged_env = {**os.environ, **(env or {})} if env else None
    process = await start(
        argv,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=merged_env,
        hold=hold,
    )
    payload = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        out, err = await asyncio.wait_for(process.communicate(payload), timeout=timeout)
    except TimeoutError as exc:
        raise ProcessTimeout(
            what=f"{Path(argv[0]).name} timed out after {_span(timeout)}",
            why="the process produced no result within the allowed time",
            how=(
                "re-run with a smaller model or split the input; raise the timeout if "
                "the job really is that long"
            ),
        ) from exc
    finally:
        await _finish(process)

    result = Result(
        code=process.returncode or 0,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
        argv=list(argv),
    )
    if check and not result.ok:
        raise EngineError(
            what=f"{Path(argv[0]).name} exited with code {result.code}",
            why=result.tail() or "the process wrote nothing to stderr",
            how="re-run with -v to see the full command and output",
        )
    return result


async def drain_stderr(process: asyncio.subprocess.Process, into: deque[str]) -> None:
    """Read what a child says on stderr for as long as it says anything, keeping the last few.

    Draining is not optional bookkeeping. A pipe nobody reads fills up and then blocks the
    writer, so a child that has plenty to say stops mid-work with no error anywhere — which
    is how a speech model would answer a few hundred sentences and then simply stop. The
    lines are kept in a bounded deque because the only ones worth having are the last ones,
    said on the way out.

    Written once here rather than once per child: the microphone and each speech model had
    the same ten lines, and `end` was extracted to this module for the same reason.
    """
    assert process.stderr is not None
    while True:
        line = await process.stderr.readline()
        if not line:
            return
        said = line.decode("utf-8", "replace").strip()
        if said:
            into.append(said)


async def end(process: asyncio.subprocess.Process, *, grace: float) -> None:
    """Ask a child to stop, and insist if it will not.

    The same six lines were written twice — once for the microphone and once for each speech
    model — with different grace periods and one of them able to drift from the other. What
    they share is the shape and every one of its edge cases: a child that has already exited
    (nothing to do), one that exits between the check and the signal (`ProcessLookupError`,
    which is success, not failure), and one that ignores `SIGTERM` (killed, and then WAITED
    for — an unreaped child is a zombie, and dictation starts and stops these all day). A
    model process `start` launched is asked as a whole group, so nothing it started stays
    behind.
    """
    try:
        if process.returncode is not None:
            return
        _send(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except TimeoutError:  # pragma: no cover - a child ignoring a TERM
            _send(process, signal.SIGKILL)
            await process.wait()
    finally:
        _let_go(process)


async def _finish(process: asyncio.subprocess.Process) -> None:
    """However the call that owned ``process`` ended, leave nothing of it running.

    The kill is sent before the first await, so a second cancellation arriving while this
    waits can only stop the waiting, never the killing.
    """
    try:
        if process.returncode is None:
            _send(process, signal.SIGKILL)
            await process.wait()
    finally:
        _let_go(process)


def _send(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    """Signal the child's whole group, when it has one of its own — else just the child.

    Only while it has not been reaped: until then its pid, and so its group id, cannot have
    been handed to anybody else.
    """
    group = _running.get(process)
    try:
        if group is not None:
            os.killpg(group, sig)
        elif sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:  # it exited between the check and here
        pass


def _adopt(process: asyncio.subprocess.Process, *, group: int | None) -> None:
    if not _running:
        _arm()
    _running[process] = group


def _let_go(process: asyncio.subprocess.Process) -> None:
    _running.pop(process, None)
    if not _running:
        _disarm()


def _arm() -> None:
    """Make the signals that would end stt outright end its children first.

    Only where the signal still has its default meaning. A handler somebody installed — the
    one `asyncio.run` puts on SIGINT to cancel the main task, the ones `stt mic` puts on
    SIGTERM and SIGINT to finish the sentence in flight — is a graceful exit that runs
    through `run`'s and `end`'s own cleanup, and cutting it short would lose the sentence.
    An ignored signal (`nohup`) stays ignored.
    """
    if threading.current_thread() is not threading.main_thread():
        return  # only the main thread may set handlers; `_tether.py` still covers the rest
    for sig in _ENDINGS:
        current = signal.getsignal(sig)
        if current is signal.SIG_DFL or current is signal.default_int_handler:
            _displaced[sig] = current
            signal.signal(sig, _end_with_children)


def _disarm() -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for sig, previous in _displaced.items():
        if signal.getsignal(sig) is _end_with_children:
            signal.signal(sig, previous)
    _displaced.clear()


def _end_with_children(signum: int, frame: FrameType | None) -> None:
    """Kill every running child (see `_send`), then end the way this signal would have."""
    del frame
    for process in list(_running):
        if process.returncode is None:
            _send(process, signal.SIGKILL)
    if _displaced.get(signal.Signals(signum)) is signal.default_int_handler:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def _missing(binary: str) -> MissingDependencyError:
    return MissingDependencyError(
        what=f"could not run {binary}",
        why="the executable does not exist on PATH",
        how=f"install {binary}, or point stt at it explicitly",
    )


def _span(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 120 else f"{seconds:.0f}s"
