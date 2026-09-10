"""Compare raw ASR engines on the same local, labelled WAV corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .. import benchmark
from .._errors import ProcessTimeout, UsageError
from ..benchmark_journal import Journal

NAME = "bench"
SUMMARY = "compare ASR accuracy, latency and failures on a labelled local corpus"


def _write_report(path: Path, text: str) -> None:
    """Publish a complete, private report without replacing an existing file."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".stt-bench-", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(text)
        # Unlike replace(), link() fails if someone created the destination during a run.
        # Both names are on the same volume; readers never see an incomplete report.
        os.link(temporary, path)
    except OSError as exc:
        raise UsageError(
            what="cannot publish benchmark report",
            why=str(exc),
            how="choose a new filename in a writable directory",
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="stt bench", description=SUMMARY)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--engine", action="append", required=True, metavar="ENGINE:MODEL")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="optional whole-run deadline, including model preparation; preserves the journal",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.max_seconds is not None and (
        not math.isfinite(args.max_seconds) or args.max_seconds <= 0
    ):
        raise UsageError(what="--max-seconds must be finite and positive")
    if args.output.exists() or args.output.is_symlink():
        raise UsageError(what="benchmark output already exists", how="choose a new output filename")
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(what="cannot create benchmark output directory", why=str(exc)) from exc
    cases = benchmark.load_manifest(args.manifest)
    journal_path = Path(str(args.output) + ".events.jsonl")

    async def execute(journal: Journal) -> dict[str, Any]:
        deadline = asyncio.timeout(args.max_seconds)
        try:
            async with deadline:
                return await benchmark.compare(
                    cases,
                    args.engine,
                    repeats=args.repeats,
                    timeout=args.timeout,
                    seed=args.seed,
                    on_event=journal.emit,
                )
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            raise ProcessTimeout(
                what="benchmark exceeded its whole-run time budget",
                why=f"completed trials and the last started operation are in {journal_path}",
                how="inspect the journal; use fewer cases or a larger --max-seconds budget",
            ) from exc

    with Journal(journal_path) as journal:
        report = asyncio.run(execute(journal))
    _write_report(args.output, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["complete"] else 10
