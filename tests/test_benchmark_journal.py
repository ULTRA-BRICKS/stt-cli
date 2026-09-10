"""A killed/failed benchmark must leave durable, attributable partial evidence."""

import asyncio
import json
import os
import stat
import wave
from pathlib import Path

import pytest

from stt_cli import backends, benchmark
from stt_cli._errors import UsageError
from stt_cli.benchmark_journal import Journal
from stt_cli.commands import bench
from stt_cli.models import Segment


def corpus(tmp_path):
    path = tmp_path / "speech.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\1\0" * 16000)
    return [{"id": "sample", "audio": str(path), "reference": "hello", "language": "en"}]


def read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_completed_trials_survive_cancellation_of_later_trial(monkeypatch, tmp_path):
    started = asyncio.Event()

    class Backend:
        calls = 0

        async def ensure_model(self, model):
            pass

        async def decode(self, request):
            self.calls += 1
            if self.calls == 3:
                started.set()
                await asyncio.sleep(60)
            return [Segment(0, 1, "hello")]

    monkeypatch.setattr(backends, "create", lambda name: Backend())
    path = tmp_path / "progress.jsonl"

    async def run():
        with Journal(path) as journal:
            await benchmark.compare(corpus(tmp_path), ["voz:voz"], repeats=3, on_event=journal.emit)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        # Evidence is readable BEFORE the process finishes, including the in-flight trial.
        events = read_events(path)
        assert events[-1]["event"] == "trial_started"
        assert events[-1]["engine"] == "voz:voz"
        assert events[-1]["case"] == "sample"
        assert events[-1]["repeat"] == 2
        assert len([e for e in events if e["event"] == "trial_finished"]) == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    events = read_events(path)
    assert events[-1]["event"] == "aborted"
    assert events[-1]["error_type"] == "CancelledError"
    assert not any(e["event"] == "completed" for e in events)
    assert events[0]["cases"][0]["sha256"]
    assert [e["sequence"] for e in events] == list(range(len(events)))


async def test_completed_summary_is_logged_and_failed_preparation_is_visible(monkeypatch, tmp_path):
    from stt_cli._errors import EngineError

    class Backend:
        async def ensure_model(self, model):
            raise EngineError(what="unavailable")

    monkeypatch.setattr(backends, "create", lambda name: Backend())
    path = tmp_path / "progress.jsonl"
    with Journal(path) as journal:
        report = await benchmark.compare(corpus(tmp_path), ["voz:voz"], on_event=journal.emit)
    events = read_events(path)
    assert [e["event"] for e in events] == [
        "started",
        "preparation_started",
        "preparation_finished",
        "completed",
    ]
    assert events[-1]["complete"] is False
    assert events[-1]["summary"] == report["summary"]
    assert events[2]["result"]["ok"] is False


def test_journal_exclusive_private_and_durable(tmp_path, monkeypatch):
    path = tmp_path / "progress.jsonl"
    syncs = []
    real_fsync = os.fsync

    def fsync(fd):
        syncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with Journal(path) as journal:
        journal.emit({"event": "trial_finished", "hypothesis": "Привет!"})
        assert read_events(path)[0]["hypothesis"] == "Привет!"
    assert syncs
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = path.read_bytes()
    with pytest.raises(UsageError), Journal(path):
        pass
    assert path.read_bytes() == original


def test_dangling_symlink_is_never_followed(tmp_path):
    path = tmp_path / "progress.jsonl"
    path.symlink_to(tmp_path / "victim")
    with pytest.raises(UsageError), Journal(path):
        pass
    assert not (tmp_path / "victim").exists()


def test_invalid_global_budget_fails_before_comparison(monkeypatch, tmp_path):
    async def forbidden(*args, **kwargs):
        pytest.fail("no comparison for an invalid budget")

    monkeypatch.setattr(benchmark, "compare", forbidden)
    for value in ["0", "-1", "nan", "inf"]:
        with pytest.raises(UsageError):
            bench.run(
                [
                    "absent.json",
                    "--engine",
                    "voz:voz",
                    "-o",
                    str(tmp_path / "out.json"),
                    "--max-seconds",
                    value,
                ]
            )


def test_cli_budget_keeps_journal_and_never_publishes_success(monkeypatch, tmp_path):
    from stt_cli._errors import ProcessTimeout

    cases = corpus(tmp_path)
    manifest = tmp_path / "corpus.json"
    manifest.write_text(json.dumps({"cases": cases}))

    async def hang(*args, on_event, **kwargs):
        on_event({"event": "trial_started", "engine": "voz:voz", "case": "sample", "repeat": 1})
        await asyncio.sleep(60)

    monkeypatch.setattr(benchmark, "compare", hang)
    output = tmp_path / "result.json"
    with pytest.raises(ProcessTimeout):
        bench.run(
            [str(manifest), "--engine", "voz:voz", "-o", str(output), "--max-seconds", "0.02"]
        )
    assert not output.exists()
    events = read_events(Path(str(output) + ".events.jsonl"))
    assert events[0]["event"] == "trial_started"
    assert events[-1]["event"] == "aborted"
