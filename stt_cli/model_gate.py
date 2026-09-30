"""model_gate — how many speech models this machine holds at once, counted across every process.

WHY THIS EXISTS
    A whisper.cpp process loads its whole model onto Metal, and Metal buffers are WIRED
    memory: nothing can swap them out or compress them, and macOS does not count them as
    memory pressure. On 2026-09-29 parallel agents ran stt over and over; each run loaded its
    own 1.6 GB large-v3-turbo, thirteen of them wired 24.65 of 25.77 GB, the pressure level
    stayed "normal" to the end, and the Mac hard-reset when WindowServer stopped answering
    its watchdog (ticket #106). Every run was sequential inside itself. Nothing counted the
    models the OTHER runs held, because nothing could see them.

HOW
    A pool of N lock files that every process on the machine shares: stt's decodes, its VAD,
    the `stt mic` daemon's whisper-servers, and whatever agent-tools runs through its own
    GPU-slot wrapper — they all take slots from the same directory. Loading a model means
    holding an exclusive `flock` on one slot file for as long as the model is loaded. A flock
    belongs to the kernel, so a holder that dies, however it dies, SIGKILL included, lets go
    of it; there is no stale state for anybody to clean up. The slot's descriptor is also
    handed to the model process itself (`proc.run(hold=...)`), so a model that somehow
    outlives the stt that started it still counts against the pool until it exits.

    N is `$RIG_GPU_SLOTS` when set. Otherwise it comes from the machine and the model: a
    sixth of physical memory divided by the model file's size, clamped to 1..MAX_SLOTS. On
    the 24 GB Mac that crashed that is two large-v3-turbo — about 3.2 GB wired on top of the
    4-5 GB the system wires by itself. Each model probes its slots from the top down, so a
    small model that is allowed more of them (a VAD, a draft model) settles in the high ones
    and leaves slot 0 and 1 to the big ones — unless every slot above them is already taken.

    A run that finds every slot taken says so once on stderr and waits, up to WAIT_SECONDS,
    then fails naming who holds them — rather than loading anyway, and rather than piling up.

THE SHARED PROTOCOL
    Agreed with agent-tools' `agenttools_gpu_slots`, so both count against the same slots:
    the directory is `$RIG_GPU_SLOT_DIR` (default `~/.local/state/agent-tools/gpu-slots`),
    the files are `slot-<i>.lock`, a holder writes `pid=<pid> argv=<...>` into its file for
    diagnosis, and a process started under a slot finds its descriptor in
    `$RIG_GPU_SLOT_FD` and uses that slot instead of taking a second one.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import shlex
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from . import resources
from ._errors import ResourceError, UsageError

DIR_ENV = "RIG_GPU_SLOT_DIR"
COUNT_ENV = "RIG_GPU_SLOTS"
HELD_ENV = "RIG_GPU_SLOT_FD"

# The share of physical memory that loaded speech models may take between them. Measured on
# the machine that crashed: the system wires 4-5 GB by itself, and the GPU saturates on one
# decode anyway (see `pipeline._decode_spans`), so a third concurrent large model buys no
# speed and spends memory the rest of the machine needs.
RAM_SHARE = 6
# However small the model: a thousand VAD processes are a thousand processes.
MAX_SLOTS = 8
# How long a run waits for a slot before it gives up and says so.
WAIT_SECONDS = 30 * 60.0
POLL_SECONDS = 0.25
# How much of each holder's command the waiting notice shows.
HOLDER_WIDTH = 60


@dataclass(frozen=True, slots=True)
class Slot:
    """One held slot: the open descriptor whose flock IS the slot, and the file it locks."""

    fd: int
    path: Path
    # Taken over from the process that started this one (RIG_GPU_SLOT_FD), so not ours to
    # close.
    inherited: bool = False


# The inherited slot covers one model at a time, like any other slot.
_inherited_in_use = False
# The descriptors this process opened for slots of its own. A stale RIG_GPU_SLOT_FD can name
# a number that was closed when the wrapper's descriptor went, and that the next slot this
# process takes is given — which is then its own slot, not one to load a second model under.
_own: set[int] = set()


def directory() -> Path:
    explicit = os.environ.get(DIR_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".local" / "state" / "agent-tools" / "gpu-slots"


def count(model_bytes: int) -> int:
    """How many models of this size may be loaded on this machine at once."""
    explicit = os.environ.get(COUNT_ENV, "").strip()
    if explicit:
        try:
            given = int(explicit)
        except ValueError:
            given = 0
        if given < 1:
            raise UsageError(
                what=f"{COUNT_ENV}={explicit!r} is not a usable number of slots",
                why="it has to be a whole number, at least 1",
                how=f"unset {COUNT_ENV} to derive it from this machine's memory, or set e.g. 2",
            )
        return given
    ram = resources.total_ram()
    if ram <= 0 or model_bytes <= 0:
        return 1  # nothing to measure against: the one answer that cannot overcommit
    return max(1, min(MAX_SLOTS, ram // RAM_SHARE // model_bytes))


@contextlib.asynccontextmanager
async def slot(model_bytes: int, *, what: str) -> AsyncIterator[Slot]:
    """Hold one of the machine's speech-model slots for the length of the block."""
    held = await acquire(model_bytes, what=what)
    try:
        yield held
    finally:
        release(held)


async def acquire(model_bytes: int, *, what: str) -> Slot:
    """Take a slot for a model of ``model_bytes``, waiting for one if they are all held.

    For a holder whose lifetime is not one block — a whisper-server that stays loaded for a
    whole dictation session. Everything else uses :func:`slot`.
    """
    pool = _pool()
    total = count(model_bytes)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECONDS
    told = False
    while True:
        taken = _attempt(pool, total)
        if taken is not None:
            return taken
        left = deadline - loop.time()
        if left <= 0:
            raise ResourceError(
                what=f"no speech-model slot came free in {WAIT_SECONDS / 60:.0f} min",
                why=(
                    f"all {total} slot(s) in {pool} stayed held ({_holders(pool, total)}), and "
                    f"loading {what} beside them could run this machine out of memory"
                ),
                how=(
                    "let the other transcriptions finish, or start fewer at once; "
                    f"{COUNT_ENV}=<n> allows more if this machine has memory to spare"
                ),
            )
        if not told:
            print(
                f"stt: all {total} speech-model slot(s) on this machine are in use "
                f"({_holders(pool, total)}); waiting up to {WAIT_SECONDS / 60:.0f} min "
                f"before loading {what}",
                file=sys.stderr,
                flush=True,
            )
            told = True
        await asyncio.sleep(min(POLL_SECONDS, left))


def attempt(model_bytes: int) -> Slot | None:
    """A slot for a model of ``model_bytes`` if one is free right now; ``None`` if not.

    For a model that is better left out than waited for: `stt mic`'s draft.
    """
    return _attempt(_pool(), count(model_bytes))


def release(held: Slot) -> None:
    """Give the slot back. The flock goes with the last descriptor, the model's included."""
    global _inherited_in_use
    if held.inherited:
        _inherited_in_use = False
        return
    _own.discard(held.fd)
    with contextlib.suppress(OSError):
        os.ftruncate(held.fd, 0)
    os.close(held.fd)


def _pool() -> Path:
    pool = directory()
    try:
        pool.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise _unusable(pool, exc) from exc
    return pool


def _attempt(pool: Path, total: int) -> Slot | None:
    """One look for a free slot: the wrapper's, when it is not in use already, then the pool's.

    The wrapper's on every look, not once before a wait: when it is the slot being waited
    for, it comes free here and never in the pool, where the wrapper holds it for good.
    """
    global _inherited_in_use
    if not _inherited_in_use:
        inherited = _inherited(pool)
        if inherited is not None:
            _inherited_in_use = True
            return inherited
    for index in reversed(range(total)):
        taken = _take(pool / f"slot-{index}.lock")
        if taken is not None:
            return taken
    return None


def _take(path: Path) -> Slot | None:
    """Lock ``path`` if nobody holds it; ``None`` if somebody does."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise _unusable(path.parent, exc) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid={os.getpid()} argv={shlex.join(sys.argv)}\n".encode(), 0)
    _own.add(fd)
    return Slot(fd, path)


def _inherited(pool: Path) -> Slot | None:
    """The slot a wrapper took for this process, if RIG_GPU_SLOT_FD really names one.

    Checked against the pool's files rather than trusted: an environment variable outlives
    the descriptor it described, and a number that now names some unrelated open file must
    not let a model in without a slot.
    """
    named = os.environ.get(HELD_ENV, "").strip()
    if not named.isdigit() or int(named) in _own:
        return None
    fd = int(named)
    try:
        held = os.fstat(fd)
    except OSError:
        return None
    for candidate in pool.glob("slot-*.lock"):
        with contextlib.suppress(OSError):
            here = candidate.stat()
            if (here.st_dev, here.st_ino) == (held.st_dev, held.st_ino):
                return Slot(fd, candidate, inherited=True)
    return None


def _holders(pool: Path, total: int) -> str:
    """Who holds the slots, from what each holder wrote into its file."""
    said: list[str] = []
    for index in range(total):
        with contextlib.suppress(OSError):
            first = (pool / f"slot-{index}.lock").read_text("utf-8", "replace").split("\n")[0]
            if first.strip():
                said.append(_holder(first))
    return ", ".join(said) or "holders unknown"


def _holder(line: str) -> str:
    """``pid=<pid> argv=<command>`` as ``pid=<pid> <program name> <args>``, cut to fit a line.

    The command is the part worth reading: a pid says nothing, while `stt mic run` says the
    dictation daemon holds the slot and will for as long as it runs.
    """
    pid, _, argv = line.partition(" argv=")
    try:
        words = shlex.split(argv)
    except ValueError:  # quoting cut short: still words
        words = argv.split()
    if not words:
        return pid
    command = " ".join([Path(words[0]).name, *words[1:]])
    if len(command) > HOLDER_WIDTH:
        command = command[: HOLDER_WIDTH - 1] + "…"
    return f"{pid} {command}"


def _unusable(pool: Path, exc: OSError) -> ResourceError:
    return ResourceError(
        what="stt cannot use the speech-model slot directory",
        why=f"{pool}: {exc.strerror or exc}",
        how=f"check the permissions on {pool}, or point {DIR_ENV} at a writable directory",
    )
