"""Regression checks for cancellation and malformed native/input boundaries."""

import asyncio
import struct
import sys
import wave

import pytest

from stt_cli import proc
from stt_cli._errors import ProcessTimeout, UsageError
from stt_cli.backends.voz import _arm64_macho, _wav_duration


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
