"""_tether — start a program that cannot outlive the stt process that asked for it.

Run as ``python -I -S _tether.py <owner-pid> <program> [args...]`` by `proc.start`, never
imported: it is a script so that it costs one interpreter start and no package import.

WHY
    stt cleans up its children on every way out it gets to run code on: an error, a
    cancellation, SIGINT, SIGTERM, SIGHUP (see `proc`). SIGKILL is the way out it does not
    get — and neither does a crash of the process hosting it, which is what happened on
    2026-09-29: the agent host died and the whisper-cli processes it had started kept
    running, holding their models on the GPU, while their callers' retry loops started more.
    macOS has no "kill me when my parent dies" flag (Linux's PR_SET_PDEATHSIG), so the
    child has to be watched from outside.

HOW
    This process forks. The PARENT execs the program in place, so the pid stt holds, the
    exit status it reads and the pipes it drains are the program's own, exactly as if it had
    been started directly. The CHILD is the watcher: it waits on both the owner (stt) and
    its own parent (the program) through kqueue, and whichever ends first decides it. The
    program ending means there is nothing left to guard, and the watcher leaves. The owner
    ending first means stt is gone without cleaning up, and the watcher SIGKILLs the whole
    process group — the program, anything the program started, and itself. `proc.start`
    gives every tethered program a session of its own, so that group is exactly theirs.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import sys
import time

# How often the watcher re-checks by hand. kqueue answers instantly; this is the fallback
# where there is no kqueue (Linux) and the safety net where it could miss an exit.
_RECHECK_SECONDS = 0.5


def main(argv: list[str]) -> None:
    owner, command = int(argv[1]), argv[2:]
    if os.getppid() != owner:
        os._exit(1)  # stt was gone before its program started: nobody left to run it for
    program = os.getpid()
    if os.fork() == 0:
        _watch(owner, program)
    try:
        os.execv(command[0], command)
    except OSError as exc:
        sys.stderr.write(f"stt: could not start {command[0]}: {exc}\n")
        os._exit(127)


def _watch(owner: int, program: int) -> None:
    """Outlive the program, and kill it if the owner goes first. Never returns."""
    # A signal meant for the program's group — stt's own polite SIGTERM before a SIGKILL, a
    # terminal hangup — must not take the watcher down first and leave the program unwatched.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    # Holding the program's pipes open would keep stt waiting for an end-of-file that only
    # comes when the watcher leaves too.
    quiet = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(quiet, fd)
    # Nor anything else it was handed — the slot's lock above all (`model_gate`). A watcher
    # holding it keeps the slot taken after the program has let go, and the next model to
    # ask is told every slot is in use.
    _close_from(3)
    if _owner_went_first(owner, program):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(0)


def _close_from(lowest: int) -> None:
    """Close every open descriptor numbered ``lowest`` or above."""
    try:
        open_now = [int(name) for name in os.listdir("/dev/fd")]
    except OSError:  # no /dev/fd (a Linux without /proc): every number there could be
        os.closerange(lowest, os.sysconf("SC_OPEN_MAX"))
        return
    for fd in open_now:
        # The listing's own descriptor is already gone again by now.
        with contextlib.suppress(OSError):
            if fd >= lowest:
                os.close(fd)


def _owner_went_first(owner: int, program: int) -> bool:
    """Block until the owner or the program ends; True when it was the owner."""
    if sys.platform == "darwin":
        queue = select.kqueue()
        for pid in (owner, program):
            try:
                queue.control([_exit_of(pid)], 0)
            except ProcessLookupError:
                return pid == owner
    while True:
        if sys.platform == "darwin":
            for event in queue.control(None, 2, _RECHECK_SECONDS):
                return bool(event.ident == owner)
        else:
            time.sleep(_RECHECK_SECONDS)
        if os.getppid() != program:
            return False  # the program exited, and this watcher was handed to launchd
        if not _running(owner):
            return True


if sys.platform == "darwin":

    def _exit_of(pid: int) -> select.kevent:
        return select.kevent(
            pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
            fflags=select.KQ_NOTE_EXIT,
        )


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # it exists; it is only not ours to signal
    return True


if __name__ == "__main__":
    main(sys.argv)
