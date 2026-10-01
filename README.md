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
```

## Install

Pick one — each ends with `stt` on your PATH:

```bash
# 1. uv — recommended; macOS, Linux and Windows (incl. Cygwin / Git Bash)
uv tool install hyper-stt

# 2. pipx
pipx install hyper-stt

# 3. one-liner — clones to ~/.local/share/stt-cli, links stt into ~/.local/bin, registers the agent skill
curl -fsSL https://git.hyperide.ai/ultrabricks/stt-cli/raw/branch/main/install.sh | bash

# 4. from a clone — to hack on it; `git pull` updates the installed tool
git clone https://git.hyperide.ai/ultrabricks/stt-cli && cd stt-cli && ./install.sh
```

- After **1** or **2**, run `stt install-skill` once so coding agents discover `stt` (3 and 4 do it for you).
- **Unreleased `main`:** `uv tool install git+https://git.hyperide.ai/ultrabricks/stt-cli` (or the same with `pipx install`).
- **Update:** `uv tool upgrade hyper-stt` · `pipx upgrade hyper-stt` · re-run the one-liner · `git pull`.
- **Installed before the rename** (as `stt-cli`, from git)? Remove that first — `uv tool uninstall stt-cli` or `pipx uninstall stt-cli` — or two installs both claim `stt`.
- **Via rig:** clone into `~/xp/stt-cli` and list `stt` under `tools.items` in `~/.config/rig/config.yaml` — `rig apply commit` runs its `install.sh` and keeps the clone fresh.

> **On PyPI as `hyper-stt`** (the command is still `stt`). The canonical repo is
> **git.hyperide.ai/ultrabricks/stt-cli**; `github.com/alex-mextner/stt-cli` is a frozen, archived mirror.

stt is **macOS-only**. Then run `stt setup` — it detects what you already have, explains the options and picks an engine.

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

Every segment carries the decoder's confidence. Where that confidence is low, `stt` can go
back and decode just that moment again:

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

The phonetic matcher is home-grown on purpose: Soundex and Metaphone are built for English
orthography and do not accept Cyrillic at all, and these recordings switch language
mid-sentence. `--dict-similarity` moves the threshold; `stt dict check` shows the scores.

### Curated dictionaries

Instead of typing a whole field's vocabulary in by hand, install a curated dictionary. stt
ships 21 of them (medicine, pharmacology, law, finance, software, AI, and more, several with
a Russian companion):

```bash
stt dict search medic              # id, version, term count, languages, title, tags; * = installed
stt dict search --tag drug-names
stt dict install medical-general   # re-running it later upgrades to the newest version
stt dict installed                 # what is installed, and anything the merge left out
stt dict show medical-general      # its terms, and what your overlay changed (below)
stt dict uninstall medical-general
```

`--remote` on `search` and `install` asks the marketplace website
(https://stt-dicts-marketplace.vercel.app) instead of the copies bundled with stt. Either
way the file's SHA-256 must match the catalog index before anything is written, and it is
validated exactly as a hand-typed entry would be. The network is touched only by those two
commands with `--remote`, never by a transcription.

The same catalog is browsable at <https://stt-dicts-marketplace.vercel.app>: search by
topic, tag or term, see every term and its recorded mis-hearings, and download the file or
copy its `stt dict install <id>` command.

Installed dictionaries live in `<STT_HOME>/dicts/installed/` and join your own terms in every
transcription: your terms first, then each installed dictionary in alphabetical order by id.
Your own dictionary wins every conflict, and an earlier installed dictionary wins over a
later one; whatever a conflict leaves out is reported as a warning by the transcription and
by `stt dict installed`. `stt dict list` still shows only your own terms. The cache key
covers the terms the transcription ends up using, so installing or uninstalling a dictionary
that adds any term re-runs the transcription instead of serving the one made before; a
dictionary whose every entry a conflict left out changes nothing, and neither does the key.
Live dictation (`stt mic`) does not read the installed dictionaries yet.

#### Overlays: your edits to an installed dictionary

One wrong or missing entry does not mean copying the whole dictionary into your own and
losing its upgrades. Write an overlay next to it instead, in
`~/.config/stt-cli/dicts/overlays/<id>.yaml` (`$XDG_CONFIG_HOME/stt-cli/dicts/overlays/`
when `XDG_CONFIG_HOME` is set to an absolute path), naming entries by the ids `stt dict show <id>` prints:

```yaml
# ~/.config/stt-cli/dicts/overlays/software-engineering-tools.yaml
base_version: 1          # the installed version you wrote it against
entries:
  conloca:
    op: replace          # new values for the fields you give; aka is replaced whole
    aka: ["ConLog", "our-internal-nickname"]
    note: "our fork, not the upstream project"
  hyperide:
    op: disable          # drop the entry
  my-codename:
    op: add              # a new entry, after the dictionary's own
    term: "ProjNova"
    aka: ["Proj Nova"]
```

`stt dict show <id>` then marks every term base, replaced or added and lists the disabled
ones. Every transcription uses the overlay, and editing it changes the cache key exactly
when it changes the words. Upgrades and `stt dict uninstall` never touch it. When the base
moves on, an overlay entry whose id is gone, a `base_version` that no longer matches, or a
name another entry already has is a warning, never a failure. A mistake in the file itself
(an unknown `op`, an add without a `term`) is an error naming the line.

The file is read as a small, fixed subset of YAML with no extra dependency, so it reads the
same everywhere: `key: value` lines indented with spaces, plain or quoted text, integers,
`["flow", "lists"]` and `- item` lists, and `#` comments. Anchors, tags, `|`/`>` blocks,
`{flow: mappings}`, tabs and a key repeated in one mapping are refused with the line number.
Quote `null`, `true`, `false` and `~` if you mean them as text. `stt dict --help` prints an
example and the rules. The details are in `docs/specs/dictionaries.md` section 7.

### Suggested dictionaries

After a month of transcribing, stt can tell you which curated dictionary matches what you
actually talk about — by a notification, at most once a week, never the same dictionary twice,
and never by installing anything itself. It names the dictionary, how many of your recent
transcripts matched it, and the `stt dict install <id>` to run.

```bash
stt dict suggest --setup   # once: fetch the local classifier, about 3 GB (laya and two checkpoints)
stt dict suggest           # on or off, ready or not, the last check and what was suggested
stt config set dict_suggest false   # no more suggestions, and nothing is read
```

The matching runs on your computer, offline: laya, a local classifier, sorts pieces of your
last month of archived transcripts into the catalog's topics, in a background process that
starts after a transcription at most once a day. No transcript text is read until you ran the
setup; a month in without it, one notification says how. Notifications use `osascript` on
macOS, `notify-send` or D-Bus on Linux and a PowerShell toast on Windows; only the macOS one
has been seen working so far.

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
stt mic                          # alias for `stt mic --help`
stt mic --device 0               # speak now, ad-hoc, right in this terminal
stt mic --check                  # is it hearing you? levels, the bar, and the verdict
stt mic --list-devices           # which microphone is which
stt mic --no-draft               # nothing is typed until it is final
stt mic --model medium           # settle faster, at some cost in accuracy
stt mic --draft-model small      # a better first guess, if the machine can afford it
```

A bare `stt mic` — no arguments at all — is a literal alias for `stt mic --help`: it prints
help and opens nothing. Add any dictation flag — `--device`, `--model`, `--no-draft`, ... —
and it dictates ad-hoc, right now: first a short banner (is the background daemon running,
what hotkey fires it, how to change it — so you know the hotkey you could rely on instead
is armed), then the microphone opens and types what you say into whatever window has focus.
`--check` shows the same banner but runs the microphone level/verdict diagnostic instead of
dictating; `--list-devices` never opens the microphone at all, so it skips the banner.
Two models run at once, and what you actually see is this: the first words appear about a
second and a half after you start talking and keep up with you from there, and one to three
seconds after you stop they are replaced by a better version of the same sentence — the one
with the proper nouns, the capitals and the punctuation right.

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
**Microphone**, the first time it records. `stt doctor --fix` triggers macOS's own
Accessibility consent flow if it has not been granted yet — stt cannot complete that flow
for you, so finishing it is still yours to do.

The background daemon (below) is a different process to macOS — the Python interpreter
launchd runs, not your terminal — so it needs its own two grants, and it asks for them
itself: when it starts without Accessibility it raises macOS's prompt, opens System
Settings on Privacy & Security > Accessibility, and waits there, carrying on by itself
within two seconds of the toggle being turned on (ten, once the prompt is over five minutes
old); then it asks for the Microphone the same
way (one Allow click). It asks each interpreter once, however often it restarts. If you
missed the window, `stt mic --fix` (`-y` to skip any question) turns the daemon on, makes
it ask again, and waits with you until it reports ready — your only action is the click.
A daemon enabled from an older stt still exits instead of waiting; `--fix` says so within
seconds, and `stt mic disable && stt mic enable` reinstalls it from this one. A daemon that
keeps failing for any other reason is also named within seconds, with its log. Only the
launchd daemon waits: `stt mic run` in a terminal asks on the terminal's behalf, so it
still refuses at once and says what to turn on.

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
costs more than the entire latency budget.

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

### Two hotkeys instead of a terminal window: press-to-toggle and hold-to-talk

```bash
stt config set hotkey_press "cmd+shift+space"  # press once to start, again to stop
stt config set hotkey_hold "ctrl+opt+d"        # dictate only while held (push-to-talk)
stt config set hotkey_hold fn                  # ...or hold the fn (Globe) key alone
stt config set hotkey_stop "cmd+shift+x"       # optional extra chord that ends a toggle burst
stt mic enable                                 # start now, and start at every login
stt mic status                                 # is it running?
stt mic stop                                   # stop it
```

`stt mic run`/`start`/`status`/`stop`/`enable`/`disable` run the same dictation as a
background daemon instead of a foreground session: both models load once, and from then on
the microphone stays CLOSED — nothing is captured — until one of the two configured
bindings opens it for one burst. `hotkey_press` and `hotkey_hold` are two INDEPENDENT
chords, both settable at once, not a mode switch on one chord: `hotkey_press` is
press-once-to-start, press-again-to-stop, for a sentence you don't want to hold a key
through; `hotkey_hold` is push-to-talk in the literal, radio sense — dictate exactly while
the chord is held, stop the instant you release it — and defaults to unset (disabled) until
you choose one. Either way, because both models are already loaded, the delay between the
hotkey and the microphone being live is under a second — a cold ad-hoc dictation (`stt mic
--device 0`, or any other flag) spends 30-45 seconds loading them first. Every burst, on
both this daemon path and the ad-hoc path above, posts a notification that says how to stop
(the chord to press again or release, or Ctrl-C) and carries a Stop button. stt draws that
button with a small app of its own, `stt.app`, which it builds from the Swift source it
ships and installs in `~/Applications`: `stt mic helper` is the build step, and `stt mic
enable` and `stt doctor --fix` run it too (it needs the Swift compiler: `xcode-select
--install`). The first notification asks once whether stt may send notifications. Once the
app is installed, the background daemon also runs inside it, so macOS lists its
Accessibility and Microphone permissions under "stt" and a Python upgrade no longer takes
them away (a daemon enabled earlier moves over with `stt mic disable && stt mic enable`).
Without the app, terminal-notifier 3.x draws the button if it is installed; the hotkey (or
Ctrl-C) always stops a dictation either way.

Every chord needs cmd or ctrl, plus any of shift, opt and fn, and one key (a letter, digit,
punctuation, space, tab, return, delete, an arrow, f1-f12): stt only listens to the
keyboard and cannot stop a keystroke reaching your document, so anything without cmd or
ctrl would also be ordinary typing. The one exception is `fn` alone, for `hotkey_hold`
only, on macOS only — the Mac push-to-talk convention. It opens the microphone after fn has
been held on its own for a moment, and any other key pressed with it (fn+arrow, fn+delete)
means it was navigation, not dictation. macOS still runs its own Globe-key action on every
press of fn, so set System Settings > Keyboard > "Press 🌐 key to" to "Do Nothing"; stt says
so whenever fn is bound and that setting is anything else. A toggle chord (`hotkey_press`,
`hotkey_stop`) may not be an editing shortcut you press while dictating (cmd+z, cmd+c,
cmd+v, cmd+a, cmd+s, the cmd+arrow jumps, cmd+delete). `hotkey_stop` defaults to `same`:
press the toggle chord again. It never ends a hold burst, which ends when you let go. No two
bindings may share a key.

`enable` also installs OS autostart (a launchd `LaunchAgent`) so the daemon comes back after
a restart; `disable` removes it and must be run explicitly to turn it back off. On macOS the
daemon is on by default: the first `stt` command you run (anything but `--help` — a
command's own `--help` included — `--version`, a usage error, and `stt mic` with no flags or
with one of the six words below) prints one line (none with `-q`/`--quiet`)
naming the Accessibility toggle macOS is about to ask for, and a detached background process
enables the daemon without delaying that command. An explicit `stt mic disable` — first
command or later — is never overridden, and once the decision is made either way a command
only reads the record; it takes no lock and runs no `launchctl`. That automatic enable is
only confirmed once the daemon it just installed is verified genuinely alive, not merely
once the OS accepted the install: a launchd-spawned process runs under its own permission
identity, separate from your terminal's (see `stt mic status` below). If that separate
process has not been granted Accessibility yet, it does not crash: it asks macOS for it and
waits, and `stt mic status` (and the banner before an ad-hoc dictation) says "waiting for
Accessibility — turn on 'python3.14' in System Settings > Privacy & Security >
Accessibility" until you click. If it cannot be verified alive at all, the outcome goes to
the daemon's log and the decision stays retryable rather than either falsely confirmed or
silently abandoned: a later `stt` command tries again, ten minutes after the first failed
try, twice as long after each one after that, and never more than a day apart. These six
lifecycle subcommands come from the same
`agenttools_service` library every other long-running agent-tools server uses (the review-cli
dashboard, spec-web) — not reimplemented here — so it needs a small additional install:

```bash
pip install -e <agent-tools>/lib/agenttools_daemon -e <agent-tools>/lib/agenttools_service
```

Without it, `stt mic run` still works (an ad-hoc foreground daemon, Ctrl-C to stop); only
`start`/`status`/`stop`/`enable`/`disable` need the library, and say so if it's missing. The
daemon is not turned on by a first `stt` command either, and that command prints nothing
about it.

A bare `stt mic` — no arguments at all — is completely unaffected by any of this: it is a
literal alias for `stt mic --help` regardless of whether the library is installed. Any
dictation flag still opens the microphone ad-hoc exactly as it always has, its pre-flight
banner just naming the daemon "not available" instead of showing a real state without the
library.

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
stt config unset language          # back to the built-in default
```

A bare `stt config`, typed at an interactive terminal, opens a browsable screen instead —
every setting grouped by what it affects, with a plain-language description, worked
examples, and the exact `stt config set` line an edit is equivalent to. Arrow keys or a
mouse click move between settings and tabs, `/` searches by name or by what a setting does
(typing "who is speaking" finds `diarize`), Enter edits, Space toggles a boolean, `r` resets
one setting to its default. Piped, redirected, or run from a script, `stt config` prints the
same plain list it always has — nothing about scripting it changes.

A flag still wins over a stored value for a single run — in both directions. Every switch has
a negative twin, so a stored default can always be turned off for one run: `--no-fix`,
`--no-summary`, `--no-diarize`, `--no-clean`, `--no-cache`, `--no-keep-media`,
`--no-show-variants`.

## Models and engines

```bash
stt models ls          # the pool, with sizes, languages and rough quality/speed
stt models pull large-v3
```

Two engines, one interface. **whisper.cpp** runs natively on Metal and brings the Silero
voice-activity detector plus per-token confidence. **mlx-whisper** installs with `uv` in
about a minute with no compiler and downloads its own models. `stt setup` looks at what you
have, explains the trade-off, and remembers your answer; `--backend` overrides it per run.

Adding a model is one entry in `stt_cli/registry.py`, and adding an engine is one class — so
if something better than Whisper shows up, it is an addition rather than a rewrite. Downloads
check free disk space and physical memory first, and refuse rather than fail at 94 percent.

### Many runs at once

Each whisper.cpp or mlx run loads its whole model onto the GPU, where it is wired memory:
macOS cannot swap it out, and does not even report it as memory pressure. Thirteen `stt`
runs started side by side by parallel agents once wired 24.65 of 25.77 GB this way and
hard-reset the Mac. So every model load on the machine — a decode, the Silero VAD, the two
models `stt mic` keeps loaded — takes one of a fixed number of **speech-model slots** first,
and an `stt` that finds them all taken prints one line saying so and waits:

```
stt: all 2 speech-model slot(s) on this machine are in use (pid=81234 stt interview.m4a, pid=81240 stt mic run); waiting up to 30 min before loading large-v3-turbo
```

The number of slots comes from the machine and the model: a sixth of physical memory
divided by the model file's size, between 1 and 8 — two `large-v3-turbo` on a 24 GB Mac,
one `large-v3`. `RIG_GPU_SLOTS=<n>` sets it outright. A run still waiting after 30 minutes
gives up with exit code `9`, naming the processes that hold the slots. The slots are lock
files in `$RIG_GPU_SLOT_DIR` (default `~/.local/state/agent-tools/gpu-slots`), shared with
anything else that takes GPU slots there; a lock belongs to the kernel, so a process that
dies — however it dies — frees its slot, and nothing is ever left to clean up.

`stt mic` waits for its main model's slot like any other run, but takes one for its fast
draft model only if one is free right then. Otherwise it dictates without the draft and
says so, rather than wait for a slot its own main model may be the one holding.

A model process never outlives the `stt` that started it. It runs in a process group of its
own that `stt` takes down on every way out — an error, Ctrl-C, `SIGTERM`, `SIGHUP`, a
cancelled task — and a small watcher beside it kills the group if `stt` disappears without
cleaning up, `kill -9` included. Each decode also has a time limit that fits its audio: two
minutes to load the model plus five seconds per second of audio (the VAD gets a tenth of a
second per second), so a whisper-cli that hangs on a ten-second clip is reaped in under
three minutes instead of three hours. The mlx engine keeps a flat three-hour limit, because
its worker downloads the model on first use.

## Exit codes

`0` ok · `2` bad usage · `4` unknown model/format/command · `5` missing file · `7` network ·
`8` permission or missing token · `9` not enough disk or memory · `10` engine failure ·
`127` missing dependency · `130` interrupted with Ctrl-C.

## Requirements

macOS (Apple Silicon for the MLX engine), Python 3.11+, `ffmpeg`, and one speech engine.
Nothing else: the package itself has zero third-party runtime dependencies.

MIT.
