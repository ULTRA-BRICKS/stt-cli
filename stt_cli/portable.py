"""The few OS primitives stt needs that differ between POSIX and Windows.

File locks: stt serialises each read-modify-write of its config, dictionary and Hugging Face token —
and keeps dictation single-instance — with an exclusive lock on a sidecar file. POSIX uses
``flock(2)``. Windows has no ``fcntl``: there the first byte is locked with ``msvcrt.locking``,
which gives the same exclusion between stt processes (only stt ever touches these lock files).
Without this, the ``fcntl`` import alone made every ``stt`` command, ``--version`` included, fail on
Windows.

``O_NONBLOCK``: stt opens files with it so a FIFO planted where a file belongs returns instead of
hanging the command. Windows has no FIFOs in the filesystem and no such flag, so it is ``0`` there.
"""

from __future__ import annotations

import errno
import os
import sys
from typing import IO, Any, TypeAlias

O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)

Lockable: TypeAlias = int | IO[Any]  # a raw descriptor or an open file object


def _fd(target: Lockable) -> int:
    return target if isinstance(target, int) else target.fileno()


if sys.platform == "win32":
    import msvcrt

    def lock(target: Lockable, *, blocking: bool = True) -> None:
        """Take the lock; with ``blocking=False`` raise ``OSError`` at once if it is held."""
        fd = _fd(target)
        os.lseek(fd, 0, os.SEEK_SET)
        if not blocking:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        while True:  # LK_LOCK gives up after 10 one-second retries; flock(LOCK_EX) waits
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError as exc:
                if exc.errno not in _LOCK_HELD:  # a bad fd, a filesystem without locks: surface it
                    raise

    # What msvcrt.locking raises while another process holds the byte: EDEADLOCK once LK_LOCK's
    # retries run out, EACCES for LK_NBLCK.
    _LOCK_HELD = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}

    def unlock(target: Lockable) -> None:
        fd = _fd(target)
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def lock(target: Lockable, *, blocking: bool = True) -> None:
        """Take the lock; with ``blocking=False`` raise ``OSError`` at once if it is held."""
        fcntl.flock(_fd(target), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(target: Lockable) -> None:
        fcntl.flock(_fd(target), fcntl.LOCK_UN)
