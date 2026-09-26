"""stt_cli.portable: the file lock behaves the same on POSIX (flock) and Windows (msvcrt)."""

from __future__ import annotations

import errno
import os
import sys

import pytest

from stt_cli import portable


def test_lock_unlock_roundtrip(tmp_path) -> None:
    path = tmp_path / "x.lock"
    with path.open("a") as handle:
        portable.lock(handle)
        portable.unlock(handle)
        portable.lock(handle.fileno(), blocking=False)  # free again, and a raw fd works too
        portable.unlock(handle.fileno())


def test_a_held_lock_refuses_a_non_blocking_second_open(tmp_path) -> None:
    path = tmp_path / "x.lock"
    with path.open("a") as first, path.open("a") as second:
        portable.lock(first)
        with pytest.raises(OSError):
            portable.lock(second, blocking=False)
        portable.unlock(first)
        portable.lock(second, blocking=False)
        portable.unlock(second)


@pytest.mark.skipif(sys.platform != "win32", reason="the msvcrt branch")
def test_a_permanent_lock_error_surfaces_instead_of_spinning(tmp_path, monkeypatch) -> None:
    import msvcrt

    def broken(fd: int, mode: int, nbytes: int) -> None:
        raise OSError(errno.EBADF, "not a lockable file")

    monkeypatch.setattr(msvcrt, "locking", broken)
    fd = os.open(tmp_path / "x.lock", os.O_CREAT | os.O_RDWR)
    try:
        with pytest.raises(OSError) as exc:
            portable.lock(fd)
        assert exc.value.errno == errno.EBADF
    finally:
        os.close(fd)


def test_o_nonblock_is_a_flag_everywhere() -> None:
    assert isinstance(portable.O_NONBLOCK, int)
    assert portable.O_NONBLOCK == getattr(os, "O_NONBLOCK", 0)
