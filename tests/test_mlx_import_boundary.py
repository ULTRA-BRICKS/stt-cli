"""A real worker launch must not import backends/mlx.py as the MLX package."""

import json
import sys
from pathlib import Path

import pytest

from stt_cli import proc
from stt_cli.backends.mlx import WORKER


@pytest.mark.parametrize("probe", [True, False])
async def test_worker_does_not_shadow_installed_mlx(tmp_path, probe):
    package = tmp_path / "mlx"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "core.py").write_text("marker = 'actual-package'\n")
    (tmp_path / "mlx_whisper.py").write_text(
        "from mlx import core\n"
        "assert core.marker == 'actual-package'\n"
        "def transcribe(*args, carry_initial_prompt=False, **kwargs):\n"
        "    return {'language': 'en', 'segments': []}\n"
    )
    arguments = ["--probe"] if probe else ["--audio", "unused.wav", "--model", "fixture"]
    result = await proc.run(
        [sys.executable, str(WORKER), *arguments],
        env={"PYTHONPATH": str(tmp_path), "PYTHONSAFEPATH": ""},
        cwd=Path(tmp_path),
        timeout=10,
    )
    assert result.ok, result.stdout + result.stderr
    answer = json.loads(result.stdout)
    expected = {"carry_initial_prompt": True} if probe else {"language": "en", "segments": []}
    assert answer == expected
