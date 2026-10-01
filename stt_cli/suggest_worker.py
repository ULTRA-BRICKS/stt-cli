"""suggest_worker — score pieces of dictated text against dictionary topics with laya.

RUN, NEVER IMPORTED BY THE CLI. laya (Convai Innovations' local classifier, PyPI `laya`) pulls
in torch and transformers, so it lives wherever it happens to be — the CLI's own interpreter
when someone installed the `suggest` extra, or a `uv run --with laya` environment otherwise —
and talks to `stt_cli.suggest` the way `diarize_worker.py` talks to `diarize.py`: a request on
standard input, one JSON object on standard output.

Three modes:

- ``--probe``: is laya importable here AND are both checkpoints already on disk? Offline: it
  never downloads anything to answer, so asking is cheap and private.
- ``--fetch``: download the two checkpoints (`stt dict suggest --setup`, where the download was
  asked for and can be watched).
- default: read ``{"pieces": [...], "questions": {...}}`` and answer every piece's per-option
  probabilities, ``{"answers": [{question: {option: probability}}]}``. Offline, on the CPU.

The transcript text arrives on standard input, never in argv, because a command line is
readable by every user on the machine for as long as the process runs.

Keep this file free of any dependency but laya, huggingface_hub and the standard library. It
is executed by a Python we do not control.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from typing import Any

# The hub repository that bundles laya's checkpoints; each one is a subfolder of it.
REPO = "convaiinnovations/laya"
# The two `laya.Router` routes between: English text to the English checkpoint, everything
# else (Russian included) to the multilingual one. The repo's third, `typed-decisions`, is
# never routed to without asking, so it is neither fetched nor loaded.
CHECKPOINTS: dict[str, str | None] = {"english": None, "multilingual": "multilingual"}
# The files one checkpoint needs — laya's own `Agent` asks the hub for exactly these; the
# repository also holds evaluation plots and code nobody here runs.
_FILES = ("rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")
# Forward passes are packed this many states at a time: small enough that a month of
# transcripts never needs more memory than one batch, on a CPU where batching buys little.
BATCH = 8
# laya's shipped checkpoints clamp a calibration value on every load and say so. It is a
# property of the published weights, not of anything stt controls.
_TEMPERATURE_WARNING = r"laya: this checkpoint ships invalid temperatures"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="score dictated text against topics with laya")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true", help="is laya ready here, offline?")
    mode.add_argument("--fetch", action="store_true", help="download the checkpoints")
    args = parser.parse_args(argv)
    # Before huggingface_hub is imported: it reads this once, at import. The matching itself
    # must never reach the network, whatever laya or transformers would like to ask; the fetch
    # must, even for a user who exports HF_HUB_OFFLINE=1 for everything else.
    if args.fetch:
        os.environ.pop("HF_HUB_OFFLINE", None)
    else:
        os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        if args.probe:
            return _say({"ready": _ready()})
        if args.fetch:
            return _say(
                {"fetched": [_checkpoint(sub, offline=False) for sub in CHECKPOINTS.values()]}
            )
        request = json.loads(sys.stdin.read())
        return _say({"answers": score(request["pieces"], request["questions"])})
    except Exception as failure:  # the caller records this; a traceback helps nobody
        return _say({"error": f"{type(failure).__name__}: {failure}"})


def score(pieces: list[str], questions: dict[str, Any]) -> list[dict[str, dict[str, float]]]:
    """Every piece's probability for every option of every question, in piece order."""
    import laya

    models = {name: (_checkpoint(sub, offline=True), sub) for name, sub in CHECKPOINTS.items()}
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=_TEMPERATURE_WARNING, category=RuntimeWarning)
        # The CPU, not Metal: a model on Metal is wired memory macOS neither swaps nor counts
        # (#106), and this runs in the background while speech models may be loaded. One
        # checkpoint resident at a time, for the same reason. The caller holds a `model_gate`
        # slot for this whole process (`suggest._slot`): CPU memory stacks on decodes too.
        router = laya.Router(models=models, device="cpu", max_loaded=1)
        results = router.predict_batch(
            [{"state": piece, "questions": questions} for piece in pieces], batch_size=BATCH
        )
    return [_probabilities(result, questions) for result in results]


def _probabilities(result: Any, questions: dict[str, Any]) -> dict[str, dict[str, float]]:
    """One laya result as ``{question: {option: probability}}``, refusing any other shape.

    laya answers a `choice` with ``{"choice", "probabilities", "confidence"}``; only the
    probabilities are used, since the caller wants how likely EVERY topic is, not the winner.
    """
    answers = result["answers"]
    return {
        name: {str(option): float(p) for option, p in answers[name]["probabilities"].items()}
        for name in questions
    }


def _ready() -> bool:
    try:
        import laya  # noqa: F401

        for sub in CHECKPOINTS.values():
            _checkpoint(sub, offline=True)
    except Exception:
        return False
    return True


def _checkpoint(sub: str | None, *, offline: bool) -> str:
    """The local directory of the repository snapshot holding checkpoint ``sub``.

    Offline, a snapshot missing the checkpoint's files is an error rather than a download:
    `snapshot_download(local_files_only=True)` returns the cached snapshot without looking
    inside it, so the files are checked here.
    """
    from huggingface_hub import snapshot_download

    prefix = f"{sub}/" if sub else ""
    root = str(
        snapshot_download(
            REPO,
            allow_patterns=[prefix + name for name in _FILES],
            local_files_only=offline,
        )
    )
    for name in ("rl_agent_config.json", "model.safetensors"):
        if not os.path.isfile(os.path.join(root, prefix, name)):
            raise FileNotFoundError(f"{REPO} {sub or 'english'}: {name} is not downloaded")
    return root


def _say(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
