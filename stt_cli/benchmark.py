"""Reproducible, local-only raw ASR comparisons. No transcript cache or LLM correction.

Latency includes the backend's process startup/model loading; preparation and the
first decode are recorded separately. Missing/failed engines never become a zero WER.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import platform
import random
import statistics
import time
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast


def normalize(text: str) -> str:
    """Version 1: NFKC, casefold, punctuation to spaces, collapse whitespace.

    Deliberately no English-only number expansion or Russian ё substitution. The
    identical rule is applied to both the reference and every engine's hypothesis.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(
        "".join(" " if unicodedata.category(char)[0] in "PS" else char for char in text).split()
    )


def distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Levenshtein distance, Myers bit vectors (Python integers have arbitrary width)."""
    if not a:
        return len(b)
    masks: dict[str, int] = {}
    for index, token in enumerate(a):
        masks[token] = masks.get(token, 0) | (1 << index)
    positive, negative, score = ~0, 0, len(a)
    high = 1 << (len(a) - 1)
    for token in b:
        equal = masks.get(token, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        plus = negative | ~(horizontal | positive)
        minus = positive & horizontal
        score += bool(plus & high) - bool(minus & high)
        plus = (plus << 1) | 1
        minus <<= 1
        positive = minus | ~(vertical | plus)
        negative = plus & vertical
    return score


def errors(reference: str, hypothesis: str) -> dict[str, int | float | None]:
    ref, hyp = normalize(reference), normalize(hypothesis)
    words, predicted = ref.split(), hyp.split()
    chars, output = ref.replace(" ", ""), hyp.replace(" ", "")
    we, ce = distance(words, predicted), distance(chars, output)
    return {
        "word_errors": we,
        "reference_words": len(words),
        "character_errors": ce,
        "reference_characters": len(chars),
        "wer": we / len(words) if words else None,
        "cer": ce / len(chars) if chars else None,
        "silence_insertions": len(predicted) if not words else 0,
    }


def load_manifest(path: Path) -> list[dict[str, Any]]:
    from ._errors import UsageError

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cases = data["cases"]
        if not isinstance(cases, list) or not cases:
            raise ValueError("cases must be a nonempty array")
        ids: set[str] = set()
        for case in cases:
            if not isinstance(case, dict):
                raise ValueError("a case must be an object")
            for field in ("id", "audio", "reference", "language"):
                if not isinstance(case.get(field), str):
                    raise ValueError(f"{field} must be a string (reference may be empty)")
            if not case["id"] or case["id"] in ids:
                raise ValueError("case ids must be nonempty and unique")
            ids.add(case["id"])
            if not case["language"]:
                raise ValueError("each case needs its spoken language")
            audio = Path(case["audio"]).expanduser()
            case["audio"] = str((path.parent / audio).resolve())
            if not Path(case["audio"]).is_file():
                raise ValueError(f"missing audio for case {case['id']}")
        return cast(list[dict[str, Any]], cases)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UsageError(
            what="invalid benchmark manifest",
            why=str(exc),
            how="see docs/voz.md for the manifest format",
        ) from exc


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [r for r in rows if r["phase"] == "measured"]
    good = [r for r in measured if r["ok"]]
    latencies = sorted(r["wall_sec"] for r in good)
    # Score each case only once, not once per repetition. An error in its first measured
    # trial stays a missing case, even if a later retry happens to succeed.
    first: dict[str, dict[str, Any]] = {}
    for row in measured:
        first.setdefault(row["case"], row)
    scored = [r["errors"] for r in first.values() if r["ok"]]
    ref_words = sum(r["reference_words"] for r in scored)
    ref_chars = sum(r["reference_characters"] for r in scored)
    seconds = sum(r["duration_sec"] for r in good)
    wall = sum(r["wall_sec"] for r in good)
    texts: dict[str, set[str]] = {}
    for row in good:
        texts.setdefault(row["case"], set()).add(row["hypothesis"])
    return {
        "trials": len(measured),
        "failures": len(measured) - len(good),
        "first_pass_failures": sum(r["phase"] == "first_pass" and not r["ok"] for r in rows),
        "quality_cases": len(scored),
        "missing_quality_cases": len(first) - len(scored),
        "wer": (
            sum(r["word_errors"] for r in scored if r["reference_words"]) / ref_words
            if ref_words
            else None
        ),
        "cer": (
            sum(r["character_errors"] for r in scored if r["reference_characters"]) / ref_chars
            if ref_chars
            else None
        ),
        "silence_insertions": sum(r["silence_insertions"] for r in scored),
        "median_wall_sec": statistics.median(latencies) if latencies else None,
        "p95_wall_sec": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
        "rtf": wall / seconds if seconds else None,
        "speedup": seconds / wall if wall else None,
        "nondeterministic_cases": sum(len(values) > 1 for values in texts.values()),
    }


async def compare(
    cases: list[dict[str, Any]],
    engines: list[str],
    *,
    repeats: int = 5,
    timeout: float = 300,
    seed: int = 0,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from . import registry
    from ._errors import SttError, UsageError
    from .backends import DecodeRequest, create
    from .backends.voz import _wav_duration

    if not cases:
        raise UsageError(what="benchmark corpus is empty")
    if repeats < 1 or not math.isfinite(timeout) or timeout <= 0:
        raise UsageError(what="repeats and timeout must be positive")
    if not engines or len(set(engines)) != len(engines):
        raise UsageError(what="choose one or more unique engine:model pairs")
    configured = []
    for label in engines:
        name, sep, model = label.partition(":")
        if not sep:
            raise UsageError(what=f"expected engine:model, got {label!r}")
        registry.require_for_engine(model, name)
        configured.append((label, create(name), model))
    report: dict[str, Any] = {
        "schema_version": 1,
        "normalization": "nfkc-casefold-punctuation-space-v1",
        "environment": {
            "os": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "scope": "raw backend decode including process/model load; not full stt pipeline",
        "repeats": repeats,
        "seed": seed,
        "preparation": {},
        "cases": [],
        "rows": [],
    }
    # Inputs are already normalized PCM16 WAV. This makes their hashes the actual bytes
    # every model hears, rather than a source file re-encoded differently per engine.
    prepared = []
    for case in cases:
        path = Path(case["audio"])
        duration = _wav_duration(path)
        with path.open("rb") as audio:
            digest = hashlib.file_digest(audio, "sha256").hexdigest()
        expected = case.get("sha256")
        if expected is not None and digest != expected:
            raise UsageError(what=f"audio checksum mismatch for {case['id']}")
        metadata = {**case, "sha256": digest, "duration_sec": duration}
        report["cases"].append(metadata)
        prepared.append(metadata)

    def emit(event: str, **data: Any) -> None:
        if on_event is not None:
            on_event({"event": event, **data})

    emit("started", **report, engines=engines)
    usable = []
    for label, backend, model in configured:
        emit("preparation_started", engine=label)
        began = time.perf_counter()
        try:
            await asyncio.wait_for(backend.ensure_model(model), timeout=max(timeout, 1200))
        except (SttError, TimeoutError) as exc:
            report["preparation"][label] = {
                "ok": False,
                "wall_sec": time.perf_counter() - began,
                "error_type": type(exc).__name__,
                "error": exc.what if isinstance(exc, SttError) else "preparation timed out",
            }
            emit("preparation_finished", engine=label, result=report["preparation"][label])
            continue
        report["preparation"][label] = {
            "ok": True,
            "wall_sec": time.perf_counter() - began,
            "runtime": getattr(backend, "runtime_info", {}),
        }
        emit("preparation_finished", engine=label, result=report["preparation"][label])
        usable.append((label, backend, model))
    rng = random.Random(seed)
    for repeat in range(repeats + 1):
        order = [(case, engine) for case in prepared for engine in usable]
        rng.shuffle(order)
        for case, (label, backend, model) in order:
            row: dict[str, Any] = {
                "case": case["id"],
                "engine": label,
                "repeat": repeat,
                "phase": "first_pass" if repeat == 0 else "measured",
                "duration_sec": case["duration_sec"],
            }
            emit("trial_started", **row)
            began = time.perf_counter()
            try:
                segments = await asyncio.wait_for(
                    backend.decode(
                        DecodeRequest(
                            wav=Path(case["audio"]), model=model, language=case["language"]
                        )
                    ),
                    timeout=timeout,
                )
                row["wall_sec"] = time.perf_counter() - began
                text = " ".join(s.text for s in segments)
                row.update(ok=True, hypothesis=text, errors=errors(case["reference"], text))
                row["backend_metrics"] = dict(getattr(backend, "last_metrics", {}))
            except (SttError, TimeoutError) as exc:
                row.update(
                    ok=False,
                    wall_sec=time.perf_counter() - began,
                    error_type=type(exc).__name__,
                    error=exc.what if isinstance(exc, SttError) else "decode timed out",
                )
            report["rows"].append(row)
            emit("trial_finished", **row)
    report["summary"] = {
        label: {
            **summarize([r for r in report["rows"] if r["engine"] == label]),
            "available": report["preparation"][label]["ok"],
        }
        for label in engines
    }
    report["complete"] = all(
        row["available"] and row["failures"] == 0 and row["first_pass_failures"] == 0
        for row in report["summary"].values()
    )
    emit("completed", complete=report["complete"], summary=report["summary"])
    return report
