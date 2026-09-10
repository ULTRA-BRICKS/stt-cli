# Second comparison attempt: incomplete, not a winner table

This supplements [the completed first Voz pilot](voz-benchmark-2026-09-10.md).

[Run 34486846466](https://github.com/alex-mextner/stt-cli/actions/runs/34486846466)
tested commit `e289a9e8ae4cb6744007570db1dd78f9cda97d19` on 2026-09-10. The host
was again macOS 15.7.9, Apple M1 (Virtual), 7 GiB RAM, with no visible Neural Engine.
The same eight LibriSpeech recordings and exact-zero WAV were used. The preserved
artifact is `voz-public-corpus-pilot`, ID `10156920954` (14-day Actions retention).

## What actually completed

Voz and **whisper.cpp 1.9.2 / large-v3-turbo** both completed their first speech probe.
Whisper was explicitly run with `--no-gpu` as a virtual-Mac CPU baseline; these are not
physical-Mac Metal or ANE results. The recognized first utterance was:

- Voz: `But Captain Bill made no such attempt, knowing it would be useless`
- whisper.cpp: `but cap'n bill made no such attempt knowing it would be useless`

A probe transcript demonstrates execution, not comparative throughput or corpus accuracy.
No per-probe latency result was saved, so the gaps between workflow log timestamps must
not be presented as inference times: they include preparation/downloads.

Direct invocation of the native CLI on the zero WAV returned exit code **1**, empty
stdout, and `Error: no speech found in silence.wav`. This confirms the earlier empty-input
behavior. The new adapter's exact-zero fast path is separate from this native diagnostic.

MLX failed before decoding with:

```text
mlx_whisper is not importable: attempted relative import with no known parent package
```

The job reached its configured 20-minute budget and was cancelled during the subsequent
comparison. Diagnostics and public audio were uploaded, but **no final comparison.json
was produced**. The logs do not locate the exact stalled trial or establish its root
cause. There is no complete same-corpus latency/WER comparison against whisper.cpp or
MLX, and no head-to-head winner can be inferred from this attempt.

The harness saves a report on normal completion and handled trial failures, but currently
has no incremental row checkpoint for forcible process/job termination. Consequently this
attempt does not supply recoverable partial measured rows. The draft status is retained;
this unresolved run must not be described as a successful long-running reliability test.

## Import regression found and fixed

The local standalone-worker directory contains `mlx.py`, the stt backend. Python adds
that directory to the import search path when it starts `mlx_worker.py`. An import of
`mlx` from the real `mlx_whisper` package therefore found the backend file instead of the
third-party package and failed on its relative imports.

A real subprocess regression with small fixture packages reproduced the exact error in
both the worker's probe and decode entrypoints. The fix removes only the worker's own
directory from the standalone script's search path, keeping genuine installed/PYTHONPATH
packages. Both tests pass after the fix. This validates the import boundary, **not** a
successful full MLX model run on this virtual Mac; inference has not been remeasured after
the patch.

The final local suite after this correction is **551 passed, 1 skipped**, including
**54 new parametrized cases** in this PR. The earlier report's 549-test figure belongs to
the preceding commit and is historical, not a discrepancy in model measurements.
