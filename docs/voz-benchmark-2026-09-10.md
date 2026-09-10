# Voz integration: measured pilot, 2026-09-10

This page separates actual inference from unit tests and vendor claims. It does not
claim that a virtual Mac measures physical-Mac Neural Engine performance.

## Corpus and scoring

Eight short recordings from eight speakers in [LibriSpeech test-clean](https://www.openslr.org/12),
selected reproducibly by `tools/voz_experiment.py` with seed 37, plus five seconds of exact
digital silence. The speech recordings total **43.71 seconds** and **119 reference tokens**
under the shared normalizer. The source archive's published MD5 was verified; each decoded
16 kHz mono PCM16 WAV has its own SHA-256 recorded in the report.

LibriSpeech is CC BY 4.0. Attribution: Vassil Panayotov, Daniel Povey, Guoguo Chen,
Sanjeev Khudanpur and the LibriVox readers. Speaker/utterance IDs, original references,
license and hypotheses are preserved in the experiment artifacts. No private recordings
were used or uploaded.

Normalization is NFKC, casefold, punctuation/symbols to spaces, then whitespace collapse,
applied equally to reference and hypothesis. Numbers are not expanded and joined words
are not split. The scoring rules were not changed after observing results. WER and CER
are corpus-weighted; a repeated recording contributes to accuracy only once.

All times below are real measurements. The raw-backend harness bypasses the transcript
archive, VAD, dictionary, cleaning and LLM correction. Wall time includes starting the
backend process and loading its model, unlike a processing-only vendor speed figure.
The first pass is separate; it is not guaranteed factory-cold because OS caches may exist.

## First pilot: actual Voz inference, before the silence workaround

[Run 34484306776](https://github.com/alex-mextner/stt-cli/actions/runs/34484306776),
implementation commit `43b8aca3472c46bacf7f54d76676ef3961527a0f`.
The `voz-public-corpus-pilot` artifact contains `comparison.json`, recordings, hashes,
references, hypotheses, host information and raw logs. Actions artifacts expire after
14 days; the experiment script and original corpus remain available for reproduction.

Host: **macOS 15.7.9, Apple M1 (Virtual), 7 GiB RAM**, native arm64 Python 3.13.15.
Core ML reported **two compute devices and no Neural Engine**. The SHA-256-verified
Desert Ant CLI 0.1.1 / core 3.1.0 / Voz weights v0.1.0 nevertheless executed real inference.
The exact assignment of Core ML operations to the remaining devices was not profiled.

| Measurement | Result |
| --- | ---: |
| Measured speech trials | 24 (8 recordings x 3 repetitions) |
| Successful speech trials | 24/24 |
| Word error rate | 8/119 = **6.72%** |
| Character error rate | **4.14%** |
| Median wall time per speech clip | **7.504 s** |
| p95 wall time per speech clip | **8.841 s** |
| Speech RTF, wall/audio | **1.392** |
| Speech speedup, audio/wall | **0.718x** |
| Speech cases with differing repeated text | **0/8** |
| Exact-silence native failures | **4/4**, including the first pass |

Median native-reported load time was 3.064 s and processing time 1.427 s. These
components are not the entire command wall time. The observed slow VM timings neither
confirm nor refute the vendor's ANE throughput claims on physical M3 Ultra hardware.

The MLX large-v3-turbo probe failed before a comparable result was obtained. It has no
WER or speed score. The report is explicitly `complete: false`: it is **not** a successful
Voz-versus-MLX benchmark, and its zero silence-insertion count does not mean silence
succeeded. The silence cases failed rather than producing a verified empty transcript.

### What the word errors actually were

Four speech recordings matched after normalization. Of the eight word edits in the
others, seven reflect textual forms: `CAP'N` versus `Captain`, `LOVE MAKING` versus
`lovemaking`, and `EIGHTEEN FIFTY FIVE` versus `1855`. One result ended `lawy` where the
reference has `lawyer`. That last discrepancy remains an observed recognition error;
this integration does not claim to have fixed the model's word prediction.

These are eight short English read-speech clips, not enough to estimate general
accuracy, performance on Russian, meetings, noise, accents or long recordings.

## Reliability changes prompted by testing

The native CLI returned a nonzero exit code for the all-zero WAV. The adapter now
recognizes **exact zero PCM** and returns an empty transcript without launching the
model. It uses no loudness threshold: even one nonzero least-significant-bit sample
continues to the decoder. The report marks this as `digital_silence_fast_path`, so it
cannot be mistaken for fast model inference. Nonzero room noise remains an actual ASR
case, not something this shortcut claims to handle.

Other red-to-green regression tests exposed and fixed a truncated WAV accepted as valid,
a universal Mach-O header typo, cancellation leaving a decoder running, and inherited
pipes/worker grandchildren surviving a cancelled wrapper. `proc.run` now uses a private
POSIX process session and reaps it on timeout or cancellation; live-dictation shutdown
uses its existing separate path.

Setup now picks the Voz model when Voz is explicitly selected, saves the defaults only
after preparation succeeds, and does not select Voz as an automatic fallback. Model
pulling delegates free-space checks to the backend that knows the actual cache volume.

## Validation scope

The local full suite passed **549 tests, with 1 skipped**. There are **52 new parametrized
cases** across native-boundary, scoring, execution and setup tests. The scorer was also
checked against an independent dynamic-programming implementation on 10,000 randomized
examples. These are integration/scoring checks, not 10,000 speech recordings.

[CI on e86b9b5](https://github.com/alex-mextner/stt-cli/actions/runs/34487173339) passed
lint, formatting, strict type checking, regression tests and the zero-engine CLI smoke
check on Python 3.11 and 3.13. The macOS native workflow checks the installed arm64
release and runs the native-boundary suite. CodeQL and secret scanning are separate
checks and do not establish the absence of every security defect.

Physical-Mac ANE occupancy, energy, peak resident memory, thermal behavior, long-running
leaks and representative Russian/meeting accuracy are **not established** by this pilot.
There is still one native process per long decode chunk, not a persistent loaded SDK
session. Claiming optimal latency for short dictation would therefore be premature.

## Reproduction

See [the backend instructions and manifest format](voz.md), or run the manual
**Voz public-corpus pilot** GitHub Actions workflow. The heavyweight workflow is manual
so ordinary PR updates do not repeatedly download models and the full corpus. Standard
unit, type and native-contract checks remain automatic.

Use identical inputs and score only common successfully completed speech cases when
comparing latency. Keep silent-input failures separate, do not score unavailable engines
as perfect recognition, and do not combine virtual CPU timings with physical ANE timings
in a winner table. The [vendor comparisons](voz.md#published-comparisons-vendor-evidence-not-our-measurements)
use a different protocol and are not our results.
