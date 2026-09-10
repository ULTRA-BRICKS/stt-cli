"""Regression checks for cancellation and malformed native/input boundaries."""

import asyncio
import contextlib
import os
import struct
import sys
import wave

import pytest

from stt_cli import proc
from stt_cli._errors import ProcessTimeout, UsageError
from stt_cli.backends.base import DecodeRequest
from stt_cli.backends.voz import VozBackend, _arm64_macho, _digital_silence, _wav_duration


@pytest.mark.parametrize(
    "magic,endian,stride",
    [
        (b"\xca\xfe\xba\xbe", ">", 20),
        (b"\xbe\xba\xfe\xca", "<", 20),
        (b"\xca\xfe\xba\xbf", ">", 32),
        (b"\xbf\xba\xfe\xca", "<", 32),
    ],
)
def test_every_universal_macho_header(tmp_path, magic, endian, stride):
    binary = tmp_path / "desertant"
    entry = struct.pack(endian + "I", 0x0100000C) + b"\0" * (stride - 4)
    binary.write_bytes(magic + struct.pack(endian + "I", 1) + entry)
    assert _arm64_macho(binary)


def test_truncated_pcm_is_rejected_before_model_work(tmp_path):
    path = tmp_path / "truncated.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000)
    data = path.read_bytes()
    path.write_bytes(data[:-100])
    with pytest.raises(UsageError):
        _wav_duration(path)


@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_and_timeout_reap_the_actual_child(monkeypatch, cancel):
    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    children = []

    async def capture(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(
        proc.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ProcessTimeout):
            await task
        assert children[0].returncode is not None, "a cancelled benchmark left its engine running"
    finally:
        # A failing/red regression test must not itself leave a sleeping child behind.
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()


async def test_digital_silence_bypasses_upstream_empty_transcript_failure(monkeypatch, tmp_path):
    path = tmp_path / "silent.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000)

    async def forbidden(*args, **kwargs):
        pytest.fail("exact digital silence needs no model or network")

    monkeypatch.setattr(proc, "run", forbidden)
    engine = VozBackend()
    assert await engine.decode(DecodeRequest(path, "voz", "en")) == []
    assert engine.last_metrics["digital_silence_fast_path"] == 1
    assert engine.last_metrics["duration_sec"] == 1
    # Even the quietest representable sample is NOT treated as digital silence.
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\1\0" + b"\0\0" * 15999)
    assert not _digital_silence(path)


@pytest.mark.skipif(os.name != "posix", reason="private process sessions are POSIX-specific")
async def test_cancellation_closes_pipes_inherited_by_grandchildren(tmp_path):
    ready = tmp_path / "ready"
    program = (
        "import subprocess,sys,time,pathlib; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)']); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    )
    task = asyncio.create_task(proc.run([sys.executable, "-c", program]))
    try:
        for _ in range(1000):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.exists()
        began = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.get_running_loop().time() - began < 1
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
