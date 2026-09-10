"""Native boundary tests; no model, network, microphone or Apple hardware needed."""

import asyncio
import copy
import json
import struct
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from stt_cli import backends, proc, registry
from stt_cli._errors import EngineError, MissingDependencyError, ResourceError, UsageError
from stt_cli.backends.base import Availability, DecodeRequest
from stt_cli.backends.voz import VozBackend, _arm64_macho, _object, parse_document


def document():
    return {
        "text": "Привет, мир!",
        "durationSec": 1,
        "loadSec": 0.2,
        "processingSec": 0.1,
        "words": [
            {"text": "Привет,", "start": 0, "end": 0.4},
            {"text": "мир!", "start": 0.5, "end": 0.9},
        ],
    }


def wav(path, frames=16000):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, frames, "NONE", "not compressed"))
        audio.writeframes(b"\1\0" * frames)
    return path


def test_unicode_timing_and_no_fabricated_confidence():
    segments = parse_document(document(), offset=37, duration=1)
    assert [(s.start, s.end, s.text) for s in segments] == [(37, 37.9, "Привет, мир!")]
    assert [w.start for w in segments[0].words] == [37, 37.5]
    assert segments[0].confidence is None
    assert segments[0].no_speech is None
    assert all(w.probability is None for w in segments[0].words)


@pytest.mark.parametrize("value", [True, None, "1", -1, float("nan"), float("inf")])
def test_rejects_invalid_numbers(value):
    data = document()
    data["words"][0]["start"] = value
    with pytest.raises(EngineError):
        parse_document(data)


@pytest.mark.parametrize(
    "change",
    [
        {"text": "incomplete"},
        {"words": []},
        {"words": None},
        {"text": None},
        {"durationSec": 0.1},
        {"durationSec": True},
    ],
)
def test_rejects_corrupt_or_truncated_document(change):
    with pytest.raises(EngineError):
        parse_document({**document(), **change}, duration=1)


@pytest.mark.parametrize("text", ["", "[]", "null", "log\n{}", '{"broken":'])
def test_rejects_non_protocol_stdout(text):
    with pytest.raises(EngineError):
        _object(text)


def test_silence_is_not_an_error():
    assert parse_document({"text": "", "words": [], "durationSec": 1}) == []


def test_rejects_reversed_and_unordered_times():
    for start, end in ((0.7, 0.4), (0, 3)):
        data = copy.deepcopy(document())
        data["words"][1].update(start=start, end=end)
        with pytest.raises(EngineError):
            parse_document(data)
    data = document()
    data["words"][0]["start"] = 0.1
    data["words"][1]["start"] = 0
    with pytest.raises(EngineError):
        parse_document(data)


def test_macho_inspection(tmp_path):
    binary = tmp_path / "desertant"
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + b"\0" * 24)
    assert _arm64_macho(binary)
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x01000007) + b"\0" * 24)
    assert not _arm64_macho(binary)
    binary.write_text("#!/bin/sh\necho fake")
    assert not _arm64_macho(binary)


@pytest.mark.parametrize("system,machine", [("Linux", "aarch64"), ("Darwin", "x86_64")])
def test_rejects_linux_intel_and_rosetta(monkeypatch, system, machine):
    monkeypatch.setattr("stt_cli.backends.voz.platform.system", lambda: system)
    monkeypatch.setattr("stt_cli.backends.voz.platform.machine", lambda: machine)
    assert not VozBackend().availability().ok


def test_voz_is_registered_but_never_an_accidental_auto_fallback(monkeypatch):
    assert isinstance(backends.create("voz"), VozBackend)
    assert registry.require_for_engine("voz", "voz")[1] == "v0.1.0"
    monkeypatch.setattr(
        backends,
        "survey",
        lambda **kw: [
            ("whispercpp", Availability(False, "missing")),
            ("voz", Availability(True, "native")),
        ],
    )
    with pytest.raises(MissingDependencyError):
        backends.resolve("auto")


@pytest.mark.parametrize(
    "kwargs",
    [{"language": None}, {"language": "ja"}, {"temperature": 0.4}, {"max_context": 64}],
)
async def test_unsupported_requests_fail_before_any_process(monkeypatch, tmp_path, kwargs):
    async def forbidden(*args, **kw):
        pytest.fail("an unsupported request must not start a process")

    monkeypatch.setattr(proc, "run", forbidden)
    request = DecodeRequest(wav=tmp_path / "absent.wav", model="voz", language="ru")
    with pytest.raises(UsageError):
        await VozBackend().decode(replace(request, **kwargs))


async def test_empty_wav_skips_model_download(monkeypatch, tmp_path):
    async def forbidden(*args):
        pytest.fail("empty audio must not download a model")

    monkeypatch.setattr(VozBackend, "ensure_model", forbidden)
    request = DecodeRequest(wav(tmp_path / "empty.wav", 0), "voz", "en")
    assert await VozBackend().decode(request) == []


async def test_decode_uses_absolute_argv_and_retains_word_times(monkeypatch, tmp_path):
    audio = wav(tmp_path / "--речь ; $(echo danger).wav")
    backend = VozBackend()
    backend._ready, backend._binary = True, Path("/native/desertant")

    async def run(argv, **kwargs):
        assert argv == ["/native/desertant", "voz", str(audio.resolve()), "--json"]
        assert kwargs["check"] is True and kwargs["timeout"] > 0
        return proc.Result(0, json.dumps(document()), "", argv)

    monkeypatch.setattr(proc, "run", run)
    result = await backend.decode(DecodeRequest(audio, "voz", "ru"))
    assert result[0].text == "Привет, мир!"
    assert backend.last_metrics["processing_sec"] == 0.1
    assert sorted(p.name for p in tmp_path.iterdir()) == [audio.name]


@pytest.mark.parametrize("downloaded", [False, True])
async def test_preparation_is_serial_once_and_guards_actual_cache(monkeypatch, downloaded):
    monkeypatch.setattr(VozBackend, "availability", lambda self: Availability(True, "native"))
    monkeypatch.setattr("stt_cli.backends.voz._find_binary", lambda: Path("/native/desertant"))
    calls, spaces = [], []
    responses = {
        "schema": {"tool": "desertant", "version": "0.1.1", "coreVersion": "3.1.0"},
        "info": {
            "runnableHere": True,
            "weightsRevision": "v0.1.0",
            "weightsRepo": "desert-ant-labs/voz",
            "downloaded": downloaded,
        },
        "cache": {"path": "/actual-sdk-cache"},
        "pull": {"model": "voz", "downloaded": True},
    }

    async def run(argv, **kwargs):
        calls.append(argv[1])
        await asyncio.sleep(0)
        return proc.Result(0, json.dumps(responses[argv[1]]), "", argv)

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(
        "stt_cli.backends.voz.resources.require_space",
        lambda needed, **kw: spaces.append((needed, kw["path"])),
    )
    backend = VozBackend()
    await asyncio.gather(backend.ensure_model("voz"), backend.ensure_model("voz"))
    assert calls == (["schema", "info"] if downloaded else ["schema", "info", "cache", "pull"])
    assert spaces == (
        [] if downloaded else [(3 * registry.get("voz").size_bytes, Path("/actual-sdk-cache"))]
    )


async def test_resource_failure_does_not_download_or_mark_ready(monkeypatch):
    monkeypatch.setattr(VozBackend, "availability", lambda self: Availability(True, "native"))
    monkeypatch.setattr("stt_cli.backends.voz._find_binary", lambda: Path("/native/desertant"))

    def no_space(*args, **kwargs):
        raise ResourceError(what="disk full")

    monkeypatch.setattr("stt_cli.backends.voz.resources.require_space", no_space)

    async def run(argv, **kwargs):
        assert "pull" not in argv
        response = {
            "tool": "desertant",
            "version": "0.1.1",
            "runnableHere": True,
            "weightsRevision": "v0.1.0",
            "weightsRepo": "desert-ant-labs/voz",
            "downloaded": False,
            "path": "/cache",
        }
        return proc.Result(0, json.dumps(response), "", argv)

    monkeypatch.setattr(proc, "run", run)
    backend = VozBackend()
    with pytest.raises(ResourceError):
        await backend.ensure_model("voz")
    assert not backend._ready
