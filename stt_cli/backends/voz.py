"""Voz through the native, release-built Desert Ant CLI (Core ML / Neural Engine).

No model conversion, Python ML stack or per-word subprocesses. The pipeline supplies
long VAD-spliced chunks; the native runtime keeps its own 15-second lane batching.
See docs/voz.md for supported settings, licensing and benchmark methodology.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import re
import struct
import wave
from pathlib import Path
from typing import Any

from .. import proc, registry, resources
from .._errors import EngineError, MissingDependencyError, MissingTargetError, UsageError
from ..models import Segment, Word
from .base import Availability, DecodeRequest, VadProvider, warn_once

CLI_VERSION = "0.1.1"
WEIGHTS_REVISION = "v0.1.0"
INSTALL = "brew install desert-ant-labs/tap/desertant"
LANGUAGES = frozenset(
    "bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro ru sk sl es sv uk".split()
)
# Two 80 ms model frames. Small endpoint rounding is clamped, not allowed to escape
# the recording; larger discrepancies are a protocol error, not plausible timing.
TIMING_TOLERANCE = 0.16
_SENTENCE_END = re.compile(r'[.!?…][)\]"»”]*$')


def _arm64_macho(path: Path) -> bool:
    """Inspect thin/fat Mach-O headers without launching lipo or requiring Xcode."""
    try:
        with path.open("rb") as source:
            header = source.read(4096)
        magic = header[:4]
        if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
            endian = "<" if magic == b"\xcf\xfa\xed\xfe" else ">"
            return struct.unpack_from(endian + "I", header, 4)[0] == 0x0100000C
        fats = {
            b"\xca\xfe\xba\xbe": (">", 20),
            b"\xbe\xba\xfe\xca": ("<", 20),
            b"\xca\xfe\xba\xbf": (">", 32),
            b"\xbf\xba\fe\xca": ("<", 32),
        }
        if magic in fats:
            endian, stride = fats[magic]
            count = struct.unpack_from(endian + "I", header, 4)[0]
            if not 0 < count <= 64 or 8 + count * stride > len(header):
                return False
            return any(
                struct.unpack_from(endian + "I", header, 8 + i * stride)[0] == 0x0100000C
                for i in range(count)
            )
    except (OSError, struct.error):
        pass
    return False


def _find_binary() -> Path | None:
    explicit = os.environ.get("STT_VOZ_BIN")
    candidates = (
        [explicit]
        if explicit
        else [
            proc.which("desertant"),
            str(Path.home() / ".local/bin/desertant"),
            "/opt/homebrew/bin/desertant",
        ]
    )
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser().resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return path
    return None


def _protocol_error(reason: str) -> EngineError:
    # Never echo the JSON: it contains the user's speech and could be megabytes long.
    return EngineError(
        what="invalid Voz response",
        why=reason,
        how=f"use Desert Ant CLI {CLI_VERSION}; see docs/voz.md, then retry with --no-cache",
    )


def _object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise _protocol_error("stdout is not a JSON object") from exc
    if not isinstance(value, dict):
        raise _protocol_error("expected an object, not a list or scalar")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _protocol_error(f"{field} must be a number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise _protocol_error(f"{field} is outside the numeric range") from exc
    if not math.isfinite(result) or result < 0:
        raise _protocol_error(f"{field} must be finite and nonnegative")
    return result


def parse_document(
    document: dict[str, Any], *, offset: float = 0.0, duration: float | None = None
) -> list[Segment]:
    """Validate the upstream word contract and preserve unknown confidence as None."""
    offset = _number(offset, "offset")
    reported = _number(document.get("durationSec"), "durationSec")
    if duration is not None:
        duration = _number(duration, "duration")
    if duration is not None and abs(reported - duration) > TIMING_TOLERANCE:
        raise _protocol_error("durationSec does not match the input WAV")
    limit = reported if duration is None else duration
    text, raw_words = document.get("text"), document.get("words")
    if not isinstance(text, str) or not isinstance(raw_words, list):
        raise _protocol_error("text and words must be a string and an array")
    words: list[Word] = []
    previous_start = 0.0
    for raw in raw_words:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            raise _protocol_error("every word needs text, start and end")
        said = raw["text"].strip()
        start, end = _number(raw.get("start"), "word.start"), _number(raw.get("end"), "word.end")
        if not said or end < start or start < previous_start or end > limit + TIMING_TOLERANCE:
            raise _protocol_error("empty word, unordered timestamps, or timestamp outside the audio")
        previous_start = start
        words.append(Word(min(start, limit) + offset, min(end, limit) + offset, said))
    # Reject truncated documents instead of archiving a plausible-looking partial transcript.
    if "".join(text.split()) != "".join("".join(word.text.split()) for word in words):
        raise _protocol_error("transcript text and timed words disagree")
    out: list[Segment] = []
    group: list[Word] = []

    def flush() -> None:
        if group:
            out.append(
                Segment(
                    start=group[0].start,
                    end=max(word.end for word in group),
                    text=" ".join(word.text for word in group),
                    words=list(group),
                )
            )
            group.clear()

    for word in words:
        if group and (word.start - group[-1].end > 0.8 or word.end - group[0].start > 7):
            flush()
        group.append(word)
        if _SENTENCE_END.search(word.text):
            flush()
    flush()
    return out


def _validate_request(request: DecodeRequest) -> None:
    registry.require_for_engine(request.model, "voz")
    language = (request.language or "").lower().replace("_", "-").split("-")[0]
    if language not in LANGUAGES:
        raise UsageError(
            what="Voz needs a known, supported spoken language",
            why="Voz does not detect language; unsupported audio can produce confident nonsense",
            how="supply -l ru, -l en, or another supported code (see docs/voz.md); "
            "use Whisper for unknown or unsupported languages",
        )
    if request.temperature != 0 or request.max_context != 0:
        raise UsageError(
            what="Voz does not implement Whisper temperature or carried context",
            why="repeating a deterministic decode would not be a second opinion",
            how="use --context off --context-compare off --variants 0; for --fix, "
            "also choose a Whisper --variant-model (see docs/voz.md)",
        )
    _number(request.offset, "offset")
    if request.initial_prompt:
        warn_once("Voz cannot bias its decoder with a glossary; dictionary post-corrections still run")
    if request.threads:
        warn_once("Voz manages Neural Engine scheduling natively; --threads is not used")


def _wav_duration(path: Path) -> float:
    if not path.is_file():
        raise MissingTargetError(what=f"no audio file: {path}", how="check the input path")
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise UsageError(
                    what="Voz backend expects 16 kHz mono PCM16 WAV",
                    how="use the stt transcription pipeline to normalize audio/video first",
                )
            if audio.getnframes() and not audio.readframes(1):
                raise wave.Error("missing PCM data")
            return audio.getnframes() / 16000
    except (OSError, EOFError, wave.Error) as exc:
        raise UsageError(
            what="cannot read the input WAV",
            why=str(exc),
            how="check or re-encode the audio with ffmpeg",
        ) from exc


class VozBackend:
    name = "voz"

    def __init__(self) -> None:
        self._ready = False
        self._lock = asyncio.Lock()
        self._binary: Path | None = None
        self.runtime_info: dict[str, Any] = {}
        self.last_metrics: dict[str, float] = {}

    def availability(self) -> Availability:
        # No process, network request, model load, or download during stt doctor/help.
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            return Availability(
                False,
                "Voz requires native arm64 macOS, not Linux/Intel/Rosetta",
                "run stt with an arm64 Python on an Apple Silicon Mac",
            )
        try:
            major = int(platform.mac_ver()[0].split(".")[0])
        except ValueError:
            major = 0
        if major < 14:
            return Availability(False, "the native CLI requires macOS 14 or newer", INSTALL)
        binary = _find_binary()
        if binary is None:
            return Availability(
                False, "desertant is not installed (or STT_VOZ_BIN is invalid)", INSTALL
            )
        if not _arm64_macho(binary):
            return Availability(False, "desertant is not an arm64 Mach-O executable", INSTALL)
        return Availability(
            True, f"native Voz CLI: {binary}; runtime checked when used; ANE utilization is not measured"
        )

    def vad_provider(self) -> VadProvider | None:
        return None

    async def can_pin_prompt(self) -> bool:
        return False

    def honours_context_budget(self) -> bool:
        return False

    def pinning_the_prompt_costs_context(self) -> bool:
        return False

    async def ensure_model(self, model: str) -> None:
        spec, revision = registry.require_for_engine(model, self.name)
        async with self._lock:
            if self._ready:
                return
            status = self.availability()
            if not status.ok:
                raise MissingDependencyError(
                    what="the Voz engine cannot run", why=status.detail, how=status.how_to_install
                )
            self._binary = _find_binary()
            assert self._binary is not None
            schema = _object(
                (await proc.run([str(self._binary), "schema", "--json"], timeout=30, check=True)).stdout
            )
            if schema.get("tool") != "desertant" or schema.get("version") != CLI_VERSION:
                raise _protocol_error(f"this adapter is validated against CLI {CLI_VERSION}")
            info = _object(
                (
                    await proc.run(
                        [str(self._binary), "info", "voz", "--json"], timeout=120, check=True
                    )
                ).stdout
            )
            if info.get("runnableHere") is not True:
                raise MissingDependencyError(what="this Desert Ant build cannot run Voz", how=INSTALL)
            if (
                info.get("weightsRevision") != revision
                or info.get("weightsRepo") != "desert-ant-labs/voz"
            ):
                raise _protocol_error("unexpected Voz weights revision or repository")
            if not isinstance(info.get("downloaded"), bool):
                raise _protocol_error("info.downloaded must be a boolean")
            if not info["downloaded"]:
                cache = _object(
                    (
                        await proc.run(
                            [str(self._binary), "cache", "--json"], timeout=120, check=True
                        )
                    ).stdout
                )
                path = cache.get("path")
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise _protocol_error("cache.path must be absolute")
                # The SDK owns the cache, not STT_HOME. Allow staging + specialization,
                # in addition to the application's normal 2 GiB disk headroom.
                resources.require_space(3 * spec.size_bytes, path=Path(path), what="Voz weights")
                pulled = _object(
                    (
                        await proc.run(
                            [str(self._binary), "pull", "voz", "--json"], timeout=1200, check=True
                        )
                    ).stdout
                )
                if pulled.get("model") != "voz" or pulled.get("downloaded") is not True:
                    raise _protocol_error("model preparation did not confirm success")
            self.runtime_info = {
                "cli_version": CLI_VERSION,
                "core_version": schema.get("coreVersion"),
                "weights_revision": revision,
                "binary": str(self._binary),
            }
            self._ready = True

    async def decode(self, request: DecodeRequest) -> list[Segment]:
        _validate_request(request)
        wav = request.wav.expanduser().resolve()
        duration = _wav_duration(wav)
        if duration == 0:
            self.last_metrics = {"duration_sec": 0.0, "load_sec": 0.0, "processing_sec": 0.0}
            return []
        await self.ensure_model(request.model)
        assert self._binary is not None
        result = await proc.run(
            [str(self._binary), "voz", str(wav), "--json"],
            timeout=min(proc.DEFAULT_TIMEOUT, max(120.0, 2 * duration + 30)),
            check=True,
        )
        document = _object(result.stdout)
        segments = parse_document(document, offset=request.offset, duration=duration)
        self.last_metrics = {
            "duration_sec": _number(document.get("durationSec"), "durationSec"),
            "load_sec": _number(document.get("loadSec"), "loadSec"),
            "processing_sec": _number(document.get("processingSec"), "processingSec"),
        }
        return segments
