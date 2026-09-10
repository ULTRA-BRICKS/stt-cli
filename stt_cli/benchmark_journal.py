"""Private, append-only benchmark evidence that survives an interrupted run.

Each complete JSON line is flushed and fsynced before the next trial starts. A
forcible termination can leave a partial final line; earlier complete lines and
an unmatched trial_started event remain useful. A journal is not a success report.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from ._errors import UsageError


class Journal:
    """Exclusively create a mode-0600 JSONL file; never overwrite another run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: TextIO | None = None
        self._sequence = 0

    def __enter__(self) -> Journal:
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                self._stream = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                os.close(fd)
                raise
        except OSError as exc:
            raise UsageError(
                what="cannot create benchmark journal",
                why=str(exc),
                how="choose a new output filename in a writable directory",
            ) from exc
        return self

    def emit(self, event: dict[str, Any]) -> None:
        """Checkpoint outside timed model work. Speech stays in this private file."""
        if self._stream is None:
            raise RuntimeError("benchmark journal is not open")
        record = {**event, "sequence": self._sequence, "recorded_unix_sec": time.time()}
        try:
            self._stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except OSError as exc:
            raise UsageError(
                what="cannot checkpoint benchmark progress",
                why=str(exc),
                how="check free space and permissions before starting another run",
            ) from exc
        self._sequence += 1

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if kind is not None:
                # Type only: arbitrary exception text may contain secrets or speech.
                # A failed disk must not replace the original exception/cancellation.
                with contextlib.suppress(OSError, UsageError):
                    self.emit({"event": "aborted", "error_type": kind.__name__})
        finally:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
