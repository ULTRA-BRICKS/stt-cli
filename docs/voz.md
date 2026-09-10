# Voz: native Apple Silicon backend and reproducible comparisons

Research date: 2026-09-10. Integration target: Desert Ant CLI **0.1.1**, Voz weights
**v0.1.0**. This is an opt-in backend, not a change to the existing Whisper defaults.

## Install and transcribe

```sh
brew install desert-ant-labs/tap/desertant
stt doctor
stt recording.m4a --backend voz --model voz --language ru
```

The adapter requires a native arm64 Python, macOS 14+, and the arm64 `desertant`
release executable. An Intel interpreter running under Rosetta is rejected. Set
`STT_VOZ_BIN=/absolute/path/to/desertant` when the release is outside PATH. The
binary's architecture is inspected without needing Xcode. No Python ML dependencies,
PyTorch installation, model conversion or copied model weights are added to stt-cli.

The first transcription prepares the model through the official CLI. Before a
missing model is pulled, the adapter checks the **SDK's actual cache volume**, not
STT_HOME: three model sizes are reserved for weights/staging/specialization, plus
stt-cli's existing 2 GiB free-space margin. Existing model caches are reused. The
checked CLI/weights versions are intentionally pinned: a future output contract or
model must be reviewed and tested before the adapter accepts it.

`-l ru` is a declaration about the input, **not a decoder language-forcing option**.
Voz neither detects nor forces language. The adapter refuses an absent/unsupported
language declaration rather than claim Whisper-equivalent autodetection. It cannot
verify that the actual recording matches the declaration. Use Whisper for unknown
languages. Supported codes: bg, hr, cs, da, nl, en, et, fi, fr, de, el, hu, it, lv,
lt, mt, pl, pt, ro, ru, sk, sl, es, sv, uk.

The normal file pipeline still normalizes audio, gates speech, maps word timestamps
back to the original recording, applies dictionary post-corrections, renders output
and archives transcripts. Confidence and no-speech probability remain **unknown**;
the native CLI does not supply them. In particular, absence of confidence is not
proof of accuracy. This does not change the separate `stt mic` live-dictation engine.

### Settings that are not interchangeable with Whisper

Voz has no initial-prompt biasing, temperature sampling or carried Whisper context.
A glossary is reported as unsupported for decoder biasing; dictionary post-correction
can still operate. Explicit nonzero temperature/context requests fail instead of
paying for identical deterministic re-decodes. `--threads` is not a Neural Engine
parallelism control and produces a warning.

The existing `--fix` defaults imply Whisper-style context/temperature comparison.
For Voz, explicitly use a genuinely different cross-check model instead:

```sh
stt recording.m4a -b voz -m voz -l ru --fix \
  --context off --context-compare off --variants 0 \
  --variant-model whispercpp:large-v3
```

The cross-check engine/model must be installed. Cross-checking is bounded by the
existing variants pipeline; it does not constitute a full-corpus benchmark.

## Why this takes the native Mac path

The official Swift release uses Core ML rather than a Python/PyTorch fallback. The
adapter feeds the existing long VAD-spliced chunks to the official runtime, preserving
its internal 15-second windows and batched decoding. It does **not** launch a model
for every word or every VAD span. Compiled `.mlmodelc` weights are left untouched.

A new native process is still created per decode chunk. This integration is not a
persistent SDK daemon: process/model-load overhead remains, particularly noticeable
for short clips. The benchmark includes it rather than presenting processing-only
numbers as command latency. Native eligibility does not prove ANE utilization or
optimal energy consumption; use Instruments on the actual physical Mac to verify
that, and compare against an SDK instance kept resident before claiming optimality.

The macOS CI job probes Core ML's available compute devices and validates the pinned
binary with its published SHA-256. A hosted Mac with no visible Neural Engine records
**NOT MEASURED**, not a successful hardware performance test. Its optional `say`
recording is a synthetic smoke test, never a representative accuracy benchmark.

## Published comparisons: vendor evidence, not our measurements

The [Voz model card](https://huggingface.co/desert-ant-labs/voz) reports word error
rate (WER; lower is better):

| Corpus | Voz | Whisper large-v3-turbo |
| --- | ---: | ---: |
| LibriSpeech test-clean | 2.19% | 2.13% |
| LibriSpeech test-other | 3.86% | 3.70% |
| GigaSpeech | 9.70% | 8.47% |
| SPGISpeech | 3.86% | 2.79% |
| Earnings22 | 12.97% | 11.07% |
| AMI | 11.84% | 13.87% |
| Reported average | 7.40% | 7.00% |

Important: the card says Voz uses its own normalizer, while Whisper figures come
from a leaderboard. This is **not** a controlled same-runtime, same-normalization
re-run. It suggests a speed/accuracy trade-off, not universal Voz superiority. The
reported Russian FLEURS result is 6.57% on concatenated long-form evaluation; it is
not a measured WER for real Russian meetings, mixed-language dictation, names or code.

The [vendor model page](https://desertant.com/models/voz/) reports roughly 290–319x
realtime on long recordings on an **M3 Ultra**, and a same-podcast 4.7x speed advantage
over whisper.cpp large-v3-turbo. These are vendor/device/workload-specific claims,
not measurements on the user's Mac. Short utterances have proportionally more fixed
window and process overhead. No independently reproduced controlled Voz comparison
was established during this integration research.

The [official CLI performance notes](https://github.com/Desert-Ant-Labs/desert-ant-cli/blob/v0.1.1/docs/performance.md)
warn that debug builds have much slower DSP/tokenizer/hash work. Use an installed
release, not `swift run` without release optimization. Distinguish download time,
first Core ML specialization, subsequent process loads and actual processing.

## Run the same comparison on your recordings

Prepare a local, human-corrected reference corpus. Include Russian and English,
short dictation and 10–30 minute recordings, clean/noisy speech, proper names/numbers,
code-switching and several silence/room-noise files. Keep each group in a separate
manifest to avoid hiding bad languages behind good ones. Do not use another model's
unreviewed output as the reference. Start with at least 30 clips per important group;
5 repeats measure variability, not statistical proof of population accuracy.

Normalize each recording **once**, before the timed run:

```sh
ffmpeg -i recording.m4a -ar 16000 -ac 1 -c:a pcm_s16le recording.wav
```

Example `corpus.json` (audio paths are relative to this manifest):

```json
{
  "cases": [
    {"id":"ru-01", "audio":"ru-01.wav", "language":"ru",
     "reference":"В пятницу выпустим новую версию приложения."},
    {"id":"en-01", "audio":"en-01.wav", "language":"en",
     "reference":"The deployment starts on Friday."},
    {"id":"silence-01", "audio":"silence.wav", "language":"ru", "reference":""}
  ]
}
```

The examples are a schema illustration, not fabricated benchmark recordings.
Each case may optionally supply `sha256`; a mismatch is refused. Actual input hashes,
references and hypotheses are retained in the report. **The report contains your
speech: keep it private unless you have permission to publish the corpus.**

```sh
stt bench corpus.json \
  --engine voz:voz \
  --engine whispercpp:large-v3-turbo \
  --engine mlx:large-v3-turbo \
  --repeats 5 --timeout 300 --seed 37 -o comparison.json
```

The runner uses raw backends, with no transcript cache, cleaning, VAD, glossary or
LLM correction, to avoid attributing post-processing to the ASR model. Language is
supplied identically, but Voz only uses it as an eligibility check. A separate first
pass is recorded before measured repetitions; it is not guaranteed to be a factory-
cold model because the operating-system cache may already be populated. Engine order
is seeded/shuffled and execution is serial, so models do not fight for the GPU/ANE.

The report includes corpus-weighted WER and character error rate (CER), consistent
Unicode normalization on references and hypotheses, median/p95 wall time, failures,
missing quality cases, nondeterministic transcripts, and false words on silence.
Conventional **RTF = wall seconds / audio seconds**; **speedup = audio / wall**.
Voz's processing/load seconds are stored separately. WER is a fraction, not a percent.
Repeated trials do not multiply a case's contribution to accuracy. Silence does not
have a zero-word denominator: false words are counted separately. Failed/unavailable
engines are never scored as WER zero. Do not compare winner scores from incomplete
reports or different corpora. The command returns 10 when engines are unavailable or
measured decodes fail, but still saves the diagnostic report.

Peak memory, energy, thermal throttling, long-running leak rates and real ANE occupancy
are **not measured** by this harness. Corpus p95 combines clip lengths; compare like-
length manifests, and use the individual rows for per-recording analysis.

## Verification boundaries

Functional tests exercise hostile JSON, unknown confidence, invalid numeric values,
word offsets, language/context rejection, safe argument vectors, architecture checks,
model-cache reuse and disk failures. The scorer has an independent dynamic-programming
reference implementation and randomized tests. Those tests validate integration and
measurement logic, **not** speech-model quality or native performance.

Physical-Mac comparative accuracy/speed/reliability results must come from an actual
`stt bench` report. They are not filled in from vendor numbers or Linux mock timings.

## Upstream contracts and licensing

- [CLI JSON contract](https://github.com/Desert-Ant-Labs/desert-ant-cli/blob/v0.1.1/docs/json.md)
- [Exact Voz adapter in CLI 0.1.1](https://github.com/Desert-Ant-Labs/desert-ant-cli/blob/v0.1.1/Sources/DesertAntCLI/Run/Adapters/VozRunner.swift)
- [Native SDK usage and limits](https://desertant.com/docs/voz/)
- [Pinned release](https://github.com/Desert-Ant-Labs/desert-ant-cli/releases/tag/v0.1.1)
- [Model license](https://license.desertant.com/1.0)

The CLI is MIT; the converted models/SDK use Desert Ant's source-available license.
This PR does not relicense or redistribute those weights. Review the upstream terms
before embedding or redistributing the runtime/model in a product.
