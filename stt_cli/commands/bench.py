"""Compare raw ASR engines on the same local, labelled WAV corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .. import benchmark
from .._errors import UsageError
from ..archive import write_atomic

NAME = "bench"
SUMMARY = "compare ASR accuracy, latency and failures on a labelled local corpus"


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="stt bench", description=SUMMARY)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--engine", action="append", required=True, metavar="ENGINE:MODEL")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise UsageError(what="benchmark output already exists", how="choose a new output filename")
    report = asyncio.run(
        benchmark.compare(
            benchmark.load_manifest(args.manifest),
            args.engine,
            repeats=args.repeats,
            timeout=args.timeout,
            seed=args.seed,
        )
    )
    write_atomic(args.output, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["complete"] else 10
