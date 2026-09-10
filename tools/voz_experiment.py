"""Small public-corpus experiment; VM fallback results are never ANE measurements.

Usage: python tools/voz_experiment.py /path/to/LibriSpeech/test-clean report
Run from an installed checkout. Corpus: https://www.openslr.org/12, CC BY 4.0.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import random
import sys
import wave
from pathlib import Path

from stt_cli import benchmark, proc
from stt_cli.backends import DecodeRequest, create


async def main(root: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    cases = []
    # One short, deterministically selected utterance per speaker, then a seeded sample.
    candidates = []
    for speaker in sorted(root.iterdir()):
        if not speaker.is_dir():
            continue
        for transcript in sorted(speaker.rglob("*.trans.txt")):
            found = False
            for line in transcript.read_text().splitlines():
                identifier, text = line.split(" ", 1)
                if 8 <= len(text.split()) <= 20:
                    candidates.append((transcript.parent / f"{identifier}.flac", text))
                    found = True
                    break
            if found:
                break
    selected = random.Random(37).sample(candidates, min(8, len(candidates)))
    if not selected:
        raise ValueError("no corpus recordings found")
    for source, text in selected:
        target = (output / (source.stem + ".wav")).resolve()
        await proc.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(source),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
            timeout=30,
            check=True,
        )
        cases.append({"id": source.stem, "audio": str(target), "language": "en", "reference": text})
    silent = (output / "silence.wav").resolve()
    with wave.open(str(silent), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000 * 5)
    cases.append({"id": "silence", "audio": str(silent), "language": "en", "reference": ""})
    (output / "corpus.json").write_text(json.dumps({"cases": cases}, indent=2))
    probes = {}
    ready = []
    requested = ["voz:voz", "whispercpp:large-v3-turbo", "mlx:large-v3-turbo"]
    for label in requested:
        name, model = label.split(":", 1)
        try:
            engine = create(name)
            await asyncio.wait_for(engine.ensure_model(model), timeout=240)
            segments = await asyncio.wait_for(
                engine.decode(DecodeRequest(Path(cases[0]["audio"]), model, "en")), timeout=240
            )
            probes[label] = {"ok": True, "text": " ".join(s.text for s in segments)}
            ready.append(label)
        except Exception as exc:
            probes[label] = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                # This experiment contains ONLY public licensed recordings.
                "detail": getattr(exc, "why", ""),
            }
        (output / "probe.json").write_text(json.dumps(probes, indent=2))
        print(json.dumps({"probe": label, **probes[label]}), flush=True)
    if "voz:voz" in ready:
        native = await proc.run(
            [os.environ["STT_VOZ_BIN"], "voz", str(silent), "--json"], timeout=60
        )
        (output / "native-silence.json").write_text(
            json.dumps({"exit_code": native.code, "stdout": native.stdout, "stderr": native.stderr})
        )
    if ready:
        report = await benchmark.compare(cases, ready, repeats=1, timeout=90, seed=37)
    else:
        report = {"complete": False, "rows": [], "summary": {}}
    report["probes"] = probes
    for label in requested:
        if label not in report["summary"]:
            report["summary"][label] = {**benchmark.summarize([]), "available": False}
    report["complete"] = report["complete"] and len(ready) == len(requested)
    report["experiment"] = {
        "corpus": "LibriSpeech test-clean, eight speakers, seeded short subset; plus silence",
        "license": "CC BY 4.0",
        "source": "https://www.openslr.org/12",
        "attribution": "Vassil Panayotov, Daniel Povey, Guoguo Chen, Sanjeev Khudanpur; LibriVox",
        "scope": "small English read-speech pilot, NOT Russian/meetings/ANE validation",
        "whispercpp_compute": "explicit --no-gpu CPU baseline in hosted VM",
        "ane_probe": json.loads((output / "ane.json").read_text()),
        "mlx_whisper_version": importlib.metadata.version("mlx-whisper"),
        "mac_model": (await proc.run(["sysctl", "-n", "machdep.cpu.brand_string"])).stdout.strip(),
        "memory_bytes": (await proc.run(["sysctl", "-n", "hw.memsize"])).stdout.strip(),
    }
    (output / "comparison.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps(report["summary"], indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
