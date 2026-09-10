import json
import wave

import pytest

from stt_cli import backends, benchmark
from stt_cli._errors import EngineError, UsageError
from stt_cli.commands.bench import _write_report
from stt_cli.models import Segment


def corpus(tmp_path):
    audio = tmp_path / "speech.wav"
    with wave.open(str(audio), "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(b"\0\0" * 16000)
    manifest = tmp_path / "corpus.json"
    manifest.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "one", "audio": "speech.wav", "reference": "Hello", "language": "en"}
                ]
            }
        )
    )
    return benchmark.load_manifest(manifest)


async def test_failed_first_pass_does_not_disappear(monkeypatch, tmp_path):
    class Backend:
        calls = 0

        async def ensure_model(self, model):
            pass

        async def decode(self, request):
            self.calls += 1
            if self.calls == 1:
                raise EngineError(what="first pass failed")
            return [Segment(start=0, end=1, text="Hello")]

    backend = Backend()
    monkeypatch.setattr(backends, "create", lambda name: backend)
    report = await benchmark.compare(corpus(tmp_path), ["voz:voz"], repeats=2)
    assert not report["complete"]
    assert report["summary"]["voz:voz"]["first_pass_failures"] == 1
    assert report["summary"]["voz:voz"]["wer"] == 0
    assert len(report["rows"]) == 3
    assert report["cases"][0]["sha256"]


async def test_unavailable_engine_has_no_fake_quality_score(monkeypatch, tmp_path):
    class Backend:
        async def ensure_model(self, model):
            raise EngineError(what="model cannot load")

    monkeypatch.setattr(backends, "create", lambda name: Backend())
    report = await benchmark.compare(corpus(tmp_path), ["voz:voz"], repeats=2)
    assert not report["complete"]
    summary = report["summary"]["voz:voz"]
    assert summary["available"] is False
    assert summary["wer"] is None
    assert summary["speedup"] is None
    assert not report["rows"]


def test_report_publishing_cannot_clobber_existing_results(tmp_path):
    path = tmp_path / "result.json"
    _write_report(path, '{"original": true}')
    with pytest.raises(UsageError):
        _write_report(path, '{"original": false}')
    assert json.loads(path.read_text())["original"] is True
    assert not list(tmp_path.glob(".stt-bench-*"))
