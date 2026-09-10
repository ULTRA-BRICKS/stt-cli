# stt

Speech-to-text on macOS for **any** audio or video file — without the ffmpeg incantation,
without the hallucinated subtitle credits, and without paying for the same transcription
twice.

```bash
stt meeting.m4a                                  # text on stdout
stt talk.mp4 -f srt -o subs/                     # subtitles, straight from a video
stt call.ogg -t absolute --tz Europe/Belgrade    # wall-clock timestamps
stt meeting.m4a --fix --summary -f md            # corrected transcript + structured summary
stt meeting.m4a --diarize -f speakers            # split into speaker turns
stt meeting.m4a -b voz -m voz -l ru              # native Voz, explicitly selected
```

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/alex-mextner/stt-cli/main/install.sh | bash
stt setup       # detects what you already have, explains the options, picks an engine
```

The installer adds `ffmpeg` and a speech engine if they are missing, and registers `stt`
as an agent skill so coding agents find it on their own. `stt doctor` reports every
dependency and how to install anything absent.

## What it actually fixes

**The command line.** `ffmpeg -i "$N.ogg" -ar 16000 -ac 1 -c:a pcm_s16le "$N.wav" &&
whisper-cli -f "$N.wav"` breaks the moment a filename has a space or a comma in it, only
accepts a couple of containers, and leaves you holding a WAV you did not want. `stt <file>`
takes any container, any codec, audio or video, and any filename.

**Hallucinations over silence.** Whisper-family models are trained to always produce text.
Hand one thirty seconds of room tone and it does not stay quiet — it emits whatever followed
silence in its training data, which was largely subtitle files. That is where
`Субтитры сделал DimaTorzok`, `Продолжение следует…`, `Thanks for watching!` and
`Subtitles by the Amara.org community` come from in a recording of your own meeting.

`stt` runs voice-activity detection first and splices out the silence, so the model is never
shown the audio it would invent over. Whatever still gets through meets a second filter with
two tiers: unmistakable artefacts go unconditionally, and ordinary phrases that happen to be
the model's favourite filler are only dropped when the evidence agrees — low confidence, or
sitting over a stretch the detector called silent. Nothing is deleted silently; every removal
is reported, and `--no-clean` flags without dropping.

**Repetition loops.** A decoder that falls into a cycle repeats a phrase until the chunk
ends. Detected structurally — a repeated word group, in any language — rather than from a
word list, and collapsed.

**Doing the work twice.** Every run is stored in a local archive keyed by the audio's content
and the settings that affect the words. Asking for a different format, or timestamps you did
not want the first time, re-renders from the archive in milliseconds. `stt archive ls`.

## Output

| Format | What it is |
| --- | --- |
| `txt` | plain text, optionally with timestamps, speakers, confidence |
| `md` | a document: source metadata, summary if present, transcript |
| `json` | everything — segments, per-segment confidence, alternatives, flags |
| `srt` `vtt` | subtitles |
| `csv` `tsv` | one row per segment, for a spreadsheet (read it by column NAME — columns get added) |
| `speakers` | dialogue, one block per speaker turn |
| `summary` | the structured summary on its own |

`-f txt,srt` for several, `-f all` for the usual set.

### Timestamps

`--timestamps none` (default), `relative` (`1:02:05` from the start), or `absolute`
(`2026-03-31 14:35:02`). Absolute time is anchored to when the recording was made, worked out
from the container's own `creation_time` tag, a date in the filename, or the file's creation
time on disk — in that order, and the output says which one it used. Override it with
`--recorded-at`, and pick a zone with `--tz Europe/Belgrade`.

A date read out of a filename is treated as wall-clock time, not as an instant: a file named
`talk-2026-03-22 19.51.58.ogg` says 19:51 no matter which timezone your laptop is in today.
A `creation_time` tag *is* a real instant, so that one is converted into the display zone.

## Variants and confidence

Whisper segments carry the decoder's confidence. Engines that do not expose confidence,
including Voz, leave it unknown rather than inventing a score. Where confidence is low,
`stt` can go back and decode just that moment again:

```bash
stt rec.m4a --variants 2 --show-variants          # extra decodings of the shaky parts
stt rec.m4a --variant-model large-v3              # cross-check against a different model
```

A second decoding at a higher temperature shows what else the same model considered. A second
*model* is stronger evidence: two sizes of one model tend to make the same mistake, so their
agreement proves little, while agreement across architectures is real corroboration. Only
low-confidence segments are re-decoded, worst first, up to a cap — clean audio costs nothing
extra.

`--show-variants` prints the alternatives with their scores:

```
[0:14:22] (0.41) и тогда мы решили сделать бридж
    ├ alt (0.38, whispercpp:large-v3-turbo@t0.4): и тогда мы решили сделать бренч
    ├ alt (0.52, whispercpp:large-v3): и тогда мы решили сделать бридж-бот
```

Voz does not implement temperature sampling or Whisper context. Its `--fix` recipe uses a
separate Whisper model instead; see [Voz settings and limitations](docs/voz.md).

## Correction and summary

```bash
stt rec.m4a --fix                  # an LLM cleans up the transcript
stt rec.m4a --summary -f md        # headline, topics, decisions, actions, open questions
stt rec.m4a --fix --text both      # print the corrected text and the original side by side
```

These borrow an agent CLI you already have logged in — `codex`, `claude` or `opencode` — so
there is no second API key and no second bill. Pick one with `--fix-with`.

The correction pass is given each segment's confidence and every alternative reading, not
just the text. That is the difference between a model editing uniformly and guessing, and a
model leaving the confident parts alone while choosing between real candidates where the
speech model was unsure. Enabling `--fix` therefore gathers variants automatically, whether
or not you asked to see them. The speech model's original wording is always kept as a
variant, so `--text raw` gets it back without re-running anything.

## Terminology

The speech model knows the language; it does not know your product, your colleagues or your
codebase. One five-minute recording produced ConLoca, Conloca, ConLog and ConLoka for a
single project name, and "Vigma" for Figma. A dictionary fixes that:

```bash
stt dict add ConLoca --aka ConLog --aka Coloca --note "the open-source project"
stt dict add Figma --aka Vigma
stt dict import glossary.txt      # "Term = alias, alias  # note", one per line
stt dict check "we keep making Colocka good"   # what would be flagged, and how strongly
```

It is used in three places, because each one catches what the others cannot:

1. **In the speech model's prompt**, before decoding. This is the only one that can fix a
   word the model got wrong acoustically. Measured on a real recording: with the glossary
   carried, "Vigma" came back as "Figma" and three of four project-name mentions came back
   spelled correctly.
2. **As an exact correction** for the spellings you recorded under `--aka`, and as a *flag*
   for words that merely sound like a term. The distinction is deliberate: a spelling you
   wrote down is a fact, a phonetic near-match is a suspicion, and suspicions do not
   silently rewrite your transcript.
3. **In the LLM correction prompt**, with the flagged candidates attached, so the pass that
   can read the sentence decides which suspicions the sentence supports.

Voz cannot use the first mechanism; exact post-corrections and LLM context still apply.

The phonetic matcher is home-grown on purpose: Soundex and Metaphone are built for English
orthography and do not accept Cyrillic at all, and these recordings switch language
mid-sentence. `--dict-similarity` moves the threshold; `stt dict check` shows the scores.

## Speakers

```bash
stt diarize install              # ~2.5 GB, only when you want it
stt login diarization            # opens the browser, gets the token, accepts the terms
stt rec.m4a --diarize -f speakers
```

Speaker diarization uses `pyannote.audio`, which means PyTorch and two gated Hugging Face
models. It is not installed by default and never downloads anything without telling you the
size first.

`stt login diarization` is the whole credential dance in one command. It opens the token
page, watches the clipboard so that clicking **Copy** in the browser is all you have to do,
verifies the token against the Hugging Face API, stores it in the file `huggingface_hub`
itself reads (`~/.cache/huggingface/token`, mode 600 — so `HF_TOKEN` never has to live in
your shell profile), then checks both gated models and reopens the page of any whose terms
you have not accepted yet. `stt login --status` reports without changing anything, and
`stt logout` removes the stored token.

```
stt login [capability] [--provider NAME] [--status] [--no-browser] [--force]
```

Piping works too, for a machine with no browser: `echo hf_... | stt login diarization`.

## Context, loops and the second opinion

Whisper feeds its own previous output back into the next 30-second window. That keeps
casing, punctuation and proper nouns consistent across window boundaries — and it is
exactly what lets a repetition loop feed itself, because a garbage phrase in the prompt
makes more of the same garbage the likeliest continuation. stt decodes with that turned off
by default (`--context off`), which is why the loops stopped.

The quality it costs is real, so you can buy it back without the risk:

```bash
stt rec.m4a --context-compare always --show-variants   # decode twice, keep both readings
stt rec.m4a --fix                                      # implies --context-compare auto
```

The comparison decodes a second time with context on and attaches every disagreement to the
primary segment as a variant. The context-free pass stays primary, so a loop can never win
by default; the LLM correction pass sees both readings with their confidences and chooses
per segment. `auto` — which `--fix` turns on by itself — re-decodes only the chunks that
look damaged (one opening in lower case, or full of shaky segments) instead of the whole
recording.

One caveat, and it belongs exactly where the feature is most used. On whisper.cpp a pinned
glossary only works if the decoder is given a context budget — with `-mc 0` the initial
prompt has no effect at all — so a run with a dictionary buys itself 64 tokens. The primary
pass therefore does carry a little of its own output on that engine, and the loop safety
above is bounded rather than absolute for `stt rec.m4a --fix` with terms in the dictionary.
It is bounded because the glossary is pinned as a static prefix and only what is left of the
64 can hold carried-back text; `--no-dict-bias` keeps `--context off` literal if you would
rather have the guarantee than the terminology. mlx-whisper pins the prompt for free and is
unaffected. A run with no comparison pass is stored as `short` rather than `off`, so the
archive says what was actually decoded; with the comparison on (which is what `--fix` turns
on) it stays `off`, because the two modes then differ in what the second pass decodes
against and collapsing them would serve one for the other.

## Dictating live

```bash
stt mic                          # speak; the words appear where you are already typing
stt mic --check                  # is it hearing you? levels, the bar, and the verdict
stt mic --list-devices           # which microphone is which
stt mic --no-draft               # nothing is typed until it is final
stt mic --model medium           # settle faster, at some cost in accuracy
stt mic --draft-model small      # a better first guess, if the machine can afford it
```

`stt mic` opens the microphone and types what you say into whatever window has focus. Two
models run at once, and what you actually see is this: the first words appear about a second
and a half after you start talking and keep up with you from there, and one to three seconds
after you stop they are replaced by a better version of the same sentence — the one with the
proper nouns, the capitals and the punctuation right.

Underneath, the small model decodes the sentence so far in about forty milliseconds and is
asked again every six-tenths of a second; the wait is the pause the sentence has to end with
before `large-v3-turbo` is given the whole of it. Your terminology dictionary reaches both,
so a project name is spelled the way you wrote it down rather than four different ways.

Press any key yourself and stt stops correcting the sentence in flight and leaves it exactly
where it is — it will never delete a character it did not type. Two presses of Escape end the
session, as does Ctrl-C in the terminal, and the whole transcript is printed when it does. A
session nobody has spoken into for half an hour ends on its own, because a microphone left
open is a room being recorded; `--idle-minutes` changes that, and `--idle-minutes 0` turns it
off. A second `stt mic` refuses to start while one is running: two of them would type into
the same window and each would delete what it believed it wrote.

It needs two permissions, both granted to the terminal application rather than to stt,
because that is the level macOS grants them at: **Accessibility**, to type at all, and
**Microphone**, the first time it records.

`stt mic --check` is what to run when dictation appears to do nothing, which is a real way
for it to fail: if every frame the microphone delivers is below the bar the detector sets,
there is no text, no error and no exit code. It opens the microphone, draws what it hears
against that bar, and says which it was — a permission never granted, the wrong device open,
a voice too quiet, or a room being mistaken for one. It types nothing anywhere. If it reports
that stray noises are loud enough to count, `stt config set mic_threshold <n>` raises the bar;
the check prints the numbers to choose from.

It also needs the `whisper-server` binary, which comes from the same whisper.cpp build as
`whisper-cli` — and it uses whisper.cpp whatever engine you have configured for files,
because MLX has no way to keep a model loaded between questions and loading one per sentence
costs more than the entire latency budget. The Voz integration is for file transcription;
it does not change live dictation.

**How it tells you what it is doing.** The terminal you started it from shows a live status
line — what is being heard, and whether it has settled. A short system sound and a
notification mark the microphone opening and closing, for when that terminal is behind
another window, which it usually is. `-q` turns all three off.

There is no menu bar icon: that needs an `NSApplication` event loop running inside the
process, which is a lot of machinery for a small thing. The notifications come from
`osascript`, which every Mac already has, so the banner is attributed to Script Editor rather
than to stt — the honest cost of not being an application.

There is also no underline under the provisional text, the way Apple's own dictation marks
what it has not decided yet. Only an input method can do that — marked text is drawn by the
receiving application's text system, and an input method is a separate installed app bundle
rather than a command. So provisional text here looks like ordinary typed text and is
corrected underneath you. If you would rather never see a wrong word at all, `--no-draft`
types nothing until it is final.

## Archive

```bash
stt archive ls                        # everything transcribed, newest first
stt archive show <run-id> -f vtt      # re-render an old run, no GPU involved
stt archive path <run-id> --open      # reveal the run's folder in Finder
stt archive usage                     # what it all costs on disk
stt archive gc --older-than 180       # dry run; add --yes to actually delete
```

Audio is kept as mono Opus at 24 kbit/s — about eleven megabytes an hour — so keeping every
recording is affordable. Everything lives under `~/Library/Application Support/stt-cli`, or
wherever you point `STT_HOME`.

Because the archive holds its own copy, a recording stays usable after you move or delete the
original: `stt old-recording.m4a -f srt` finds the archived audio by path and carries on.

Adding a summary or speaker labels to something already transcribed costs only that pass —
those are additions rather than changes, so they are not part of the cache key:

```bash
stt meeting.m4a                # transcribe (minutes)
stt meeting.m4a --summary      # cache hit; runs only the summary (seconds)
```

## Defaults

If every run of yours is `-l ru -t absolute --tz Europe/Belgrade`, those are not options,
they are your defaults:

```bash
stt config set language ru
stt config set timestamps absolute
stt config set timezone Europe/Belgrade
stt config list
```

A flag still wins over a stored value for a single run — in both directions. Every switch has
a negative twin, so a stored default can always be turned off for one run: `--no-fix`,
`--no-summary`, `--no-diarize`, `--no-clean`, `--no-cache`, `--no-keep-media`,
`--no-show-variants`.

## Models and engines

```bash
stt models ls          # the pool, with sizes, languages and rough quality/speed
stt models pull large-v3
```

Three engines, one interface. **whisper.cpp** runs natively on Metal and brings the Silero
voice-activity detector plus per-token confidence. **mlx-whisper** installs with `uv` in
about a minute with no compiler and downloads its own models. **Voz** uses the native
Desert Ant Core ML release without a Python ML stack. `stt setup` explains the options and
remembers the choice; `--backend` overrides it per run. Voz is explicitly selected and never
silently replaces Whisper in `auto` mode.

### Voz on Apple Silicon

```bash
brew install desert-ant-labs/tap/desertant
stt recording.m4a --backend voz --model voz --language ru
# Optional: make Voz the default file engine after its model is ready.
stt setup --backend voz
```

This adapter targets Desert Ant CLI **0.1.1**, Voz weights **v0.1.0**, native arm64 Python
and macOS **14+**. The language declaration is required; it does not force the decoder's
language. Word timestamps are preserved, while confidence remains unknown. Existing SDK
weights are reused, and free space is checked on the SDK's actual cache volume.

See [Voz setup, supported settings, licensing and published benchmarks](docs/voz.md) and
[our measured pilot and its hardware limitations](docs/voz-benchmark-2026-09-10.md).
Native execution is tested, but physical-Mac Neural Engine performance is not inferred
from a virtual Mac's CPU fallback timings.

### Compare engines on the same labelled corpus

```bash
stt bench corpus.json --engine voz:voz --engine whispercpp:large-v3-turbo \
  --engine mlx:large-v3-turbo --repeats 5 --seed 37 -o comparison.json
```

The [manifest and methodology](docs/voz.md#run-the-same-comparison-on-your-recordings)
cover weighted WER/CER, full wall time, first passes, failures, silence and repeatability.
No transcript cache or LLM correction is used in the comparison. Reports contain the
reference text and recognized speech: keep private recordings and their reports private.

Adding a model is one entry in `stt_cli/registry.py`, and adding an engine is one class — so
if something better than Whisper shows up, it is an addition rather than a rewrite. Downloads
check free disk space and physical memory first, and refuse rather than fail at 94 percent.

## Exit codes

`0` ok · `2` bad usage · `4` unknown model/format/command · `5` missing file · `7` network ·
`8` permission or missing token · `9` not enough disk or memory · `10` engine failure ·
`127` missing dependency.

## Requirements

macOS (Apple Silicon for MLX and Voz), Python 3.11+, `ffmpeg`, and one speech engine.
Voz specifically requires native arm64 execution and macOS 14+.
Nothing else: the package itself has zero third-party runtime dependencies.

The stt-cli code is MIT. Voz weights and its SDK retain their separate upstream license;
see [licensing notes](docs/voz.md#upstream-contracts-and-licensing).
