"""Make the source checkout importable so tests run without installing the package."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _a_speech_model_slot_pool_of_its_own(tmp_path_factory, monkeypatch):
    """Keep every test out of the machine's real speech-model slots (`stt_cli.model_gate`).

    Any test that reaches a decode takes a slot, and the real pool is shared with whatever
    else is running on the machine — a `stt mic` daemon holding its models, an agent's
    batch. A test would then either queue behind them for half an hour or hold a slot
    somebody else was waiting for.
    """
    monkeypatch.setenv("RIG_GPU_SLOT_DIR", str(tmp_path_factory.mktemp("gpu-slots")))
    monkeypatch.delenv("RIG_GPU_SLOTS", raising=False)
    monkeypatch.delenv("RIG_GPU_SLOT_FD", raising=False)
