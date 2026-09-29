# Live dictation UX (`stt mic`) — specification

The contract for how `stt mic` starts, stops, reports itself and is configured. Each section
says what is **shipped** on `main` (verified against
[e10967b](https://git.hyperide.ai/ultrabricks/stt-cli/commit/e10967bd61), 2026-09-29) and
what is a **gap**, with the ticket that owns it. When code and this document disagree, one of
them is a bug: fix the code, or change this document in the same pull request that changes
the behaviour.

The invariants in `AGENTS.md` ("Live dictation") still apply and are not repeated here: stt
deletes only what it typed, one writer, no key code leaves `tap.py`, silence never reaches a
model, one session at a time, nothing holds a resource after it ends.

Origin: Alex's requests of 2026-09-24 to 2026-09-28 (two hotkeys with different checks,
`fn` on Mac, bare `stt mic` as help, a check window when not run as the daemon, the daemon on
by default, stacked status lines, an inviting idle line, a Stop button that is always there).

## 1. Command surface

| Invocation | Behaviour | Status |
| --- | --- | --- |
| `stt mic` (no arguments) | Byte-identical to `stt mic --help`; launches nothing | Shipped ([#85](https://git.hyperide.ai/ultrabricks/stt-cli/issues/85)), `stt_cli/commands/mic.py:102` |
| `stt mic [flags]` | Ad-hoc dictation in this terminal, after the check banner (section 3) | Shipped |
| `stt mic --check` | Opens the microphone and reports levels against the gate, after the same banner; never triggers daemon autostart | Shipped (#85, [#87](https://git.hyperide.ai/ultrabricks/stt-cli/issues/87)) |
| `stt mic --list-devices` | Lists microphones; no banner, no microphone | Shipped |
| `stt mic run` | The daemon in the foreground of this shell | Shipped ([#21](https://git.hyperide.ai/ultrabricks/stt-cli/issues/21)) |
| `stt mic start` / `stop` / `status` | Background daemon lifecycle | Shipped (#21, [#54](https://git.hyperide.ai/ultrabricks/stt-cli/issues/54)) |
| `stt mic enable` / `disable` | Install/remove launchd autostart and start/stop now; records an explicit decision (section 4) | Shipped (#21, #87) |

The six lifecycle words are dispatched only when they are the first argument
(`_LIFECYCLE`, `stt_cli/commands/mic.py:64`).

**Help contents.** `stt mic --help` must name the lifecycle subcommands, the permissions
needed and where to look when nothing appears (shipped). `stt mic run --help` must state both
hotkey defaults (shipped: `--hotkey-press` default `cmd+shift+space`, `--hotkey-hold` unset).

- Gap: neither help screen lists the **allowed modifiers** —
  [#101](https://git.hyperide.ai/ultrabricks/stt-cli/issues/101).
- Gap: `stt mic --help` promises "a notification's Stop button when `terminal-notifier` is
  installed", which does not currently work (section 6) —
  [#99](https://git.hyperide.ai/ultrabricks/stt-cli/issues/99).

## 2. Hotkey bindings

### 2.1 Settings

| Key | Flag | Default | Unset means | Disable with |
| --- | --- | --- | --- | --- |
| `hotkey_press` | `--hotkey-press` | `cmd+shift+space` (`DEFAULT_PRESS_HOTKEY`, `stt_cli/live/hotkey.py:94`) | use the default | `none`, `off` or `""` (`_DISABLE_WORDS`, `hotkey.py:102`) |
| `hotkey_hold` | `--hotkey-hold` | none | disabled | same words |
| `hotkey_stop` (gap) | — | `same` | re-press of the chord that started a press burst | — |

`hotkey_press` and `hotkey_hold` are two independent bindings, both live in one daemon
(`stt_cli/config.py:145-146`). A press binding arms on a press and disarms on the next press
of the same chord; a hold binding arms on press and disarms on release. `hotkey_stop` is
Alex's "configurable stop hotkey defaulting to the same as the trigger" and is not built yet
(#101): when set to a chord it ends any press burst; it never applies to hold bursts, which
end on release.

**Cross-binding rule (shipped):** the two bindings may not share a physical key, even with
different modifiers, because a key release reports only the key code
(`same_keycode`, `hotkey.py:387`). Enforced at `stt config set` time (`hotkey.validate`,
`hotkey.py:408`), at flag time for `enable`/`start`/`run`, and again at daemon start for a
hand-edited config.

### 2.2 Validators

**Shipped today:** one validator for both bindings — `hotkey.parse(spec, kind=...)`
(`hotkey.py:287-331`) with `_STRONG_MODIFIERS_HOLD = _STRONG_MODIFIERS_PRESS`
(`hotkey.py:152-153`) and one error text regardless of `kind`. A spec needs at least one
modifier, at least one of them `cmd` or `ctrl`, and a key from the known US-ANSI key list.

**Target ([#101](https://git.hyperide.ai/ultrabricks/stt-cli/issues/101)):**

| Check | Applies to | Rule | Why |
| --- | --- | --- | --- |
| Shared floor | every key+modifier chord | at least one of `cmd`/`ctrl`; a bare key is refused | The tap is listen-only: it never swallows the real event, so a chord without `cmd`/`ctrl` is also ordinary typing or navigation (`shift+d` types "D", `opt+e` starts "é", `fn+left` is Home) |
| Press rule | `hotkey_press`, `hotkey_stop` | fires once per physical press; autorepeat key-downs ignored; the re-press is the stop, so the chord must not be one the user presses while dictating | A toggle that re-fires on autorepeat opens and closes the microphone in a loop |
| Hold rule | `hotkey_hold` | needs a reliable release: autorepeat never re-arms, releasing the modifier before the key still ends the burst; `shift`-only is refused | Holding `shift+key` with a listen-only tap types the letter into the document for the whole burst |
| fn-alone hold | `hotkey_hold` only, macOS only (decided on #101, not built) | see 2.4 | The Mac push-to-talk convention |

Each check has its own error message naming the binding and the rule, and its own tests. If
the final decision is that press and hold need no different rule beyond the floor, that
decision is written in `hotkey.py`'s docstring and tested — not left implicit.

A known wrong message today: bare `fn` is refused with "e.g. `cmd+shift+fn`", which is itself
refused because `fn` is not a key (#101).

### 2.3 Allowed modifiers

| Platform | Strong (one required) | Also allowed with a strong one | Not available |
| --- | --- | --- | --- |
| macOS | `cmd`, `ctrl` | `shift`, `opt` (`option`, `alt`), `fn` | — |
| Windows (future, [#76](https://git.hyperide.ai/ultrabricks/stt-cli/issues/76)) | `ctrl`, `win` (to decide) | `shift`, `alt` | `fn` |
| Linux (future, [#64](https://git.hyperide.ai/ultrabricks/stt-cli/issues/64)) | `ctrl`, `super` (to decide) | `shift`, `alt` | `fn`, unless the device reports `KEY_FN` |

Keys: letters, digits, the punctuation row, `space`, `tab`, `return`, `delete`, arrows,
`f1`–`f12`, named by US-ANSI physical position. `escape` is deliberately not a hotkey key.

This list is printed in `stt mic --help`, `stt mic run --help`, the config screen's hint for
both fields, and every refusal message (#101).

### 2.4 `fn` policy

`fn` is recognised as a modifier bit (`_FN = 1 << 23`, `hotkey.py:118`, entered as
`_MODIFIERS["fn"]`, `:164`) and is accepted alongside `cmd`/`ctrl` (e.g. `ctrl+fn+f1`). It
can never stand alone today: `parse` requires a trailing key, and the tap matches
key-down/key-up events, not modifier-only `flagsChanged` events.

**Decided — fn-alone hold push-to-talk** ([#101](https://git.hyperide.ai/ultrabricks/stt-cli/issues/101),
2026-09-29, reversible; not built): hold the Globe/fn key alone, speak, release — the style of
macOS Dictation and several open-source Mac dictation tools.

- Scope: `fn` alone is allowed for `hotkey_hold` only, and on macOS only. `hotkey_press` (and
  `hotkey_stop`) still require `cmd` or `ctrl`: fn is tapped on its own far too often in
  ordinary use for a single tap to be a safe trigger.
- Detection: add `kCGEventFlagsChanged` to the tap; a hold counts only if fn goes down and
  back up with **no key-down in between**. Any key-down while fn is held means it was
  `fn+arrow`/`fn+delete` navigation, and the burst is cancelled.
- The Globe key setting: the tap cannot suppress macOS's own Globe action (switch input
  source, emoji picker, or double-press Dictation), so fn-alone hold works cleanly only when
  System Settings → Keyboard → "Press 🌐 key to" is **Do Nothing**, readable as
  `defaults read com.apple.HIToolbox AppleFnUsageType` (`0` = Do Nothing; `1` input source,
  `2` emoji, `3` dictation; absent = system default, not 0). When fn-alone hold is configured
  and the value is not `0`, stt tells the user in plain words to set it to Do Nothing, naming
  that exact place — when the binding is set, and in the check banner (section 3). It does not
  refuse the binding and does not change the preference itself (that needs a logout or a
  private API).

### 2.5 `fn` on Windows and Linux (research, 2026-09-29)

- **Windows:** on most laptop keyboards fn is resolved by the keyboard's own controller or
  the embedded controller and never produces a scan code, so a low-level keyboard hook
  (`SetWindowsHookEx(WH_KEYBOARD_LL)`) cannot see it
  ([Fn key](https://en.wikipedia.org/wiki/Fn_key);
  [AutoHotkey forum](https://www.autohotkey.com/boards/viewtopic.php?t=118394)). Some vendors
  expose it only through their own drivers. Treat `fn` as unavailable.
- **Linux:** keyboard-dependent. Some drivers report `KEY_FN` through evdev (Apple keyboards
  via `hid-apple`; specific `atkbd` quirks such as
  [TongFang barebones](https://lkml.iu.edu/hypermail/linux/kernel/2503.2/01685.html)); most
  do not. Offer `fn` only if the device is seen to report it.

## 3. The check banner (run not as the daemon)

Printed to standard error, once, right before an ad-hoc dictation or `--check` opens the
microphone (`_adhoc_banner`, `stt_cli/commands/mic.py:126`). Not printed for
`--list-devices` or bare `stt mic`; suppressed by `--quiet`. It must never crash the
dictation behind it: every probe failure degrades to a plain line.

| Line | Source | Shipped text (examples) |
| --- | --- | --- |
| Daemon state | `_daemon_status_line`, `mic.py:153` | `daemon:  running (pid N) [autostart on]` · `daemon:  not running` · `daemon:  enabled, but not currently running (last exit code 8) — see stt mic status` · `daemon:  status unavailable — …` (inner command quotes omitted here) |
| One line per binding | `_hotkey_lines`, `mic.py:184` | `hotkey:  cmd+shift+space (toggle) — the built-in default, not yet configured`, then `change it with stt config set hotkey_press ..., or stt config for the interactive screen` · `hotkey:  hold disabled — stt config set hotkey_hold ... to arm it` |
| Allowed modifiers | gap (#101) | e.g. `modifiers: cmd or ctrl, plus any of shift/opt/fn` |
| Globe key setting | gap (#101) | only when fn-alone hold is configured and `AppleFnUsageType` is not `0`: set "Press 🌐 key to" to Do Nothing in System Settings → Keyboard |
| How to stop this session | printed by the session's first status note today (`listening on <device> — Ctrl-C to finish …`) | must match section 6 |

Tests: `tests/test_hotkey.py::test_check_flag_also_prints_the_ad_hoc_banner_but_never_triggers_autostart`
(the banner is printed on `--check`, stubbing the two line builders),
`test_ad_hoc_dictation_prints_the_check_banner_before_opening_the_microphone`,
`test_quiet_suppresses_the_ad_hoc_check_banner`,
`test_list_devices_never_shows_the_ad_hoc_banner`, and the line-content tests
`test_hotkey_lines_shows_the_configured_chord_and_how_to_change_it`,
`test_hotkey_lines_names_the_built_in_default_when_unconfigured`,
`test_hotkey_lines_degrades_on_a_hand_edited_unusable_chord`.

## 4. The daemon is on by default

`mic_daemon_autostart` (`stt_cli/config.py:172`) is a decision record, not a switch:

| Value | Meaning | Who writes it |
| --- | --- | --- |
| `None` | undecided — eligible for automatic enable | default; `stt config unset mic_daemon_autostart` |
| `"retry"` (`MIC_AUTOSTART_RETRY`, `config.py:54`) | autostart was installed but the daemon was not verified alive; try again next time | `_maybe_autostart_daemon` |
| `True` | on, and the daemon was verified alive (or enabled explicitly) | `_maybe_autostart_daemon`, `stt mic enable` |
| `False` | the user said no | `stt mic disable` |

**Trigger shipped today (#87):** after an ad-hoc dictation completes successfully, and only
while the record is `None` or `"retry"`, `_maybe_autostart_daemon` (`mic.py:339`) enables
launchd autostart and then polls the same launchd probe `stt mic status` uses, up to
`_AUTOSTART_PROBE_ATTEMPTS = 6` times, 0.5 s apart (`mic.py:261-294`; on macOS only — off
macOS it probes once). Verified alive → `True` and a note that the hotkey now works from any
application; not verified → `"retry"` and a note naming the cause. An explicit
`enable`/`disable` is never overridden. The whole check-enable-probe-persist sequence holds
one lock shared with `enable`/`disable`. `--check` never triggers it.

**Trigger decided ([#98](https://git.hyperide.ai/ultrabricks/stt-cli/issues/98), 2026-09-29,
reversible; gap until #98 lands):** the daemon enables itself at the **first `stt` run on
macOS**, unless the user explicitly disabled it (`mic_daemon_autostart` is `False`, written
by `stt mic disable`). It no longer waits for a successful ad-hoc dictation: that dictation
was itself blocked on the permissions the daemon can only ask for once it is running, so the
daemon's own one-click permission request (#98) now appears in the session the user
installed stt in. The lock, the liveness probe and the recorded states above stay the same;
only the trigger moves. Because every `stt` command can now be the trigger, the enable-and-probe
sequence (up to about 3 s under the shared lock) must never delay the command that triggered
it — it runs detached, or after that command's own output and exit code are settled — and a
`"retry"` record is retried with a backoff #98 chooses, so a daemon that cannot come alive
(for example one denied Accessibility) does not cost every later command a probe. The record
is read under the lock, as today, so with that backoff concurrent commands do not each probe.

**Liveness is not permission.** The launchd-spawned daemon is its own macOS privacy
principal: the interpreter in `ProgramArguments` of
`~/Library/LaunchAgents/com.agenttools.stt.mic.plist`, separate from any terminal. A daemon
can be installed, alive, and still unable to type or hear.

- Gap: the daemon exits on missing Accessibility and launchd restarts it in a loop instead of
  asking for its own grant and waiting —
  [#98](https://git.hyperide.ai/ultrabricks/stt-cli/issues/98).
- Gap: nobody has seen the launchd-spawned daemon type after a hotkey press —
  [#97](https://git.hyperide.ai/ultrabricks/stt-cli/issues/97) (blocked by #98).
- Gap: the trigger is still the first successful ad-hoc dictation, not the first `stt` run
  decided above — #98.

## 5. Permissions

| Context | Principal macOS evaluates | stt's advice today |
| --- | --- | --- |
| `stt mic` / `stt mic run` in a terminal | the terminal application the command runs under — see [`docs/accessibility.md`](../accessibility.md) | "grant Accessibility to the terminal application running stt" (`stt_cli/live/quartz.py:169`) |
| `stt mic start` / `enable` (launchd) | the launchd-spawned interpreter (first `ProgramArguments` entry of the plist) | "grant Accessibility to the process launchd runs for the daemon (its python interpreter, or bin/stt …)" (`mic.py:81`) |

stt cannot grant itself Accessibility (settled in
[#57](https://git.hyperide.ai/ultrabricks/stt-cli/issues/57), `docs/accessibility.md`).

**Why one Mac answered both "granted" and "NOT granted" (resolved on
[#80](https://git.hyperide.ai/ultrabricks/stt-cli/issues/80), 2026-09-29, from Alex's own
terminal).** A check run from a terminal is attributed to the terminal application, so each
entry point carries its own grant. On Alex's Mac, Ghostty and tmux are granted and WezTerm is
denied; a session reached over ssh or mosh (his phone, through Moshi) is not granted. The
launchd daemon has no terminal: its principal is Homebrew's `python3.14`, which is not
granted, so the daemon exits with code 8 and launchd restarts it in a loop (#98). Nothing
went stale. Still a gap (#80): stt's messages name a class of process, not the concrete
principal it checked.

Microphone: `stt mic --check` reports "delivered pure silence" when macOS feeds an empty
stream, which is what a denied Microphone permission looks like
(`stt_cli/live/meter.py:167`). Any non-zero level (for example "peak 117, room 22") means the
stream is open.

## 6. Stopping

### 6.1 Stop contract

| Session | Always works | Also |
| --- | --- | --- |
| Daemon, press burst | press the same chord again (or `hotkey_stop`, #101) | notification Stop button; idle timeout |
| Daemon, hold burst | release the chord | notification Stop button; idle timeout |
| Ad-hoc (`stt mic` in a terminal) | Ctrl-C | notification Stop button; idle timeout |
| The daemon itself | `stt mic stop` / `disable` | — |

- The idle timeout is 30 minutes of nobody speaking (`IDLE_MINUTES`,
  `stt_cli/live/session.py:55`); `--idle-minutes 0` disables it.
- Any key the user presses during a session interrupts only the correction pass for the
  sentence in flight. It never ends the session.
- **No Escape-Escape.** The double-Escape stop was removed by
  [#79](https://git.hyperide.ai/ultrabricks/stt-cli/issues/79) because Escape is pressed
  constantly for unrelated reasons. It must not come back, and no other single common key may
  end a session.
- **Identical in demo and real use:** the ad-hoc path and the daemon path post the same Stop
  notification.

### 6.2 The notification

**Required (#99):** every burst posts a notification that (a) says how to stop, in words,
and (b) carries a clickable Stop button — on a default install, with no optional tools.

| Session | Notification text |
| --- | --- |
| Daemon, press | `listening — press <chord> again, or click Stop, to finish` |
| Daemon, hold | `listening — release <chord>, or click Stop, to finish` |
| Ad-hoc | `listening — Ctrl-C in the terminal, or click Stop, to finish` |

Clicking Stop ends only the current burst; the daemon keeps running. A burst that ends any
other way withdraws its notification, so no stale Stop button is left behind.

**Shipped today — not meeting the requirement:**

- The only notification every user gets is `osascript display notification "listening on
  <device>"` (`status.opened`, `stt_cli/live/status.py:289`), which has no button and does
  not say how to stop. For the daemon, the "how to stop" text goes only to its log file.
- The Stop button depends on the optional `terminal-notifier`
  (`notifier_available`, `status.py:341`), and is called with `-actions Stop`
  (`status.py:404`). The terminal-notifier Homebrew installs today does not read
  `-actions`: 2.0.0 removed action buttons, and 3.0.0 (2026-08-23; Homebrew stable is 3.1.0
  as of 2026-09-29) brought them back as `-action TITLE`, singular
  ([3.0.0 release notes](https://github.com/julienXX/terminal-notifier/releases/tag/3.0.0)).
  So no button is drawn and the call returns at once. The unit test asserts the wrong flag
  against a mocked subprocess.

**Helper application (#99, to build):**

- A minimal app bundle (working name `stt-notify.app`) with its own bundle identifier, no
  Dock icon, using `UNUserNotificationCenter` with one category carrying one `Stop` action.
- Built in CI as a universal binary; installed into `~/Applications` by `stt doctor --fix` or
  the first `stt mic enable`; the first use raises macOS's one-time Notifications prompt.
- Protocol: stt starts it with the burst id and the text; it prints exactly one line —
  `STOP`, `CLOSED` or `DISMISSED` — and exits. stt terminates it and it withdraws its
  notification when the burst ends another way. This is the same contract
  `stop_notification` has today, so `_race_burst_signals`
  (`stt_cli/live/dictation.py:378`) does not change.
- The same helper is the natural home for an always-visible microphone indicator
  ([#14](https://git.hyperide.ai/ultrabricks/stt-cli/issues/14)) and for the dictionary
  suggestion's Install action ([#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88)).
- Until it exists: pass `-action Stop`, and put the how-to-stop text into the notification
  every user already gets.
- Deferred: Windows (a toast notification with a button) and Linux (a freedesktop
  notification action) adapters, with the platform tickets
  [#60](https://git.hyperide.ai/ultrabricks/stt-cli/issues/60), #64 and #76.

## 7. Status line

### 7.1 Rendering

One line on standard error, redrawn in place (`status.Line`, `stt_cli/live/status.py:95`):
a bare carriage return before each repaint, never a newline mid-session, and silent when
standard error is not a terminal. `--quiet` turns it off. Each frame, and the padding that
erases the previous one, is fitted to one column less than the width of standard error
itself, measured at every repaint (`status._room`); `COLUMNS` and the width of standard
output are consulted only when standard error reports no size. Widths are counted in
terminal cells (`status._columns`): escape sequences and combining or format characters
take none, East Asian wide and fullwidth characters take two, and a control character in
the text (a newline in dictated text) is drawn as a space. A trimmed frame ends in `…`, and
in a colour reset when it carried any escape sequence. Section 7.2 is the rule this meets.

| State | Frame (`status.render`, `status.py:231`) |
| --- | --- |
| Idle, nothing heard yet | `● start speaking _  try saying "…"` — the caret alternates `_` / space every 0.5 s |
| Hearing speech, draft | `● listening  … <draft text>` |
| Sentence settled | `● listening  ✓ <settled text>` |
| Paused | `○ paused` |

### 7.2 Narrow terminals: no wrapped-row corruption

**Decided ([#100](https://git.hyperide.ai/ultrabricks/stt-cli/issues/100), 2026-09-29): a
real stt bug.** This reverses [#89](https://git.hyperide.ai/ultrabricks/stt-cli/issues/89),
which closed Alex's "stacked lines" as a capture artifact after reproducing only in wide
terminals.

- The interface Alex watches is a terminal about 50 columns wide: his phone, connected to the
  Mac through Moshi / ssh. His output from it (verbatim on #100) shows rows partly
  overwritten, duplicated and fused with neighbouring text, across the check banner, the
  meter and `stt mic status`.
- Mechanism, as recorded on #100: a line wider than the terminal wraps onto several physical
  rows, and a bare `\r` returns only to the start of the last row, so a repaint overwrites
  only the tail of the previous line and leaves its earlier rows on screen. The `^R` in the
  first report is taken to be this corruption described loosely; stt never emits byte 0x12.

The rules:

- A line redrawn in place never occupies more than one physical row: it fits the width of
  the terminal it is written to (standard error), measured at that repaint (a resize between
  repaints included), counting the padding that erases the previous frame.
- A line printed once (the check banner, the meter verdict, notes, `stt mic status`) ends
  with a newline and is never repainted with `\r`.
- A wide real terminal (Terminal.app, iTerm2, Ghostty, tmux) keeps today's in-place redraw.
- Proof: a capture from a ~50-column terminal (Moshi/ssh, or a resized local terminal),
  before and after, with no duplicated or partly overwritten rows.

**Shipped (#100):** the width is measured on standard error at every repaint, with no
20-column floor, and the erase padding is capped to it (section 7.1). Before, it was
measured through `shutil.get_terminal_size`, which reads `COLUMNS` and then standard output:
with standard output redirected (`stt mic > notes.txt`) or a stale `COLUMNS`, a 50-column
terminal was painted as if it were 80 columns wide, and a pty capture at 50 columns showed
one new `● listening … <draft>` row per repaint — Alex's first report. The same capture
now shows one row. Tests: `tests/test_live.py::test_a_repainted_line_never_wraps_in_a_narrow_terminal`,
`test_a_resize_between_repaints_is_measured_and_the_erase_fits_too`,
`test_a_terminal_narrower_than_twenty_columns_is_fitted_too`,
`test_escape_sequences_and_combining_marks_take_no_columns`. Out of reach of a bare `\r`: a
terminal that reflows its rows when it narrows (Terminal.app, iTerm2) turns the frame drawn
before the resize into two rows itself, and the first of them stays on screen.

A separate line-oriented mode for readers that are not terminals (a line-by-line capture
shows every frame as a new line, as #89 measured) is not part of #100's fix.

### 7.3 Idle UX

Shipped by [#91](https://git.hyperide.ai/ultrabricks/stt-cli/issues/91): the idle head reads
"start speaking" (`_INVITATION`, `status.py:81`) instead of the passive "listening", with a
blinking caret (`_blink_while_idle`, `stt_cli/live/dictation.py:308`, every 0.5 s) and one
example phrase chosen once per burst (`status.example_hint`, `status.py:258`): a term from
the user's own dictionary when there is one, otherwise a built-in spoken sentence
(`_BUILTIN_EXAMPLES`, `status.py:89`). The frame reverts to the listening form the moment
speech arrives.

- Gap: examples come only from the personal dictionary; once installed dictionaries exist
  they must come from the effective glossary —
  [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83), see
  [`dictionaries.md`](dictionaries.md).

## 8. Platforms

Live dictation is macOS-only today (Quartz event tap, `avfoundation` capture, launchd). Linux
capture and typing are [#64](https://git.hyperide.ai/ultrabricks/stt-cli/issues/64); the
Linux platform layer is [#60](https://git.hyperide.ai/ultrabricks/stt-cli/issues/60);
Windows is [#76](https://git.hyperide.ai/ultrabricks/stt-cli/issues/76). Each adapter needs
its own allowed-modifier list (section 2.3), stop notification (section 6.2) and autostart
mechanism.

## 9. Verification status

| Item | Shipped | Live-verified on a real Mac | Owner |
| --- | --- | --- | --- |
| Bare `stt mic` = help | yes | yes (`cmp` of both outputs) | #85 |
| Check banner on ad-hoc and `--check` | yes | unit tests only | #85 |
| Two independent bindings, collision guard | yes | no | #79, #97 |
| Different validators per binding, modifier list, fn | no | — | #101 |
| Esc-Esc removed | yes | unit tests only | #79 |
| Always-available Stop button + how-to-stop text | no | — | #99 |
| Daemon on by default, liveness-verified | yes (after first successful dictation) | no — daemon crash-looping on Alex's Mac | #87, #98, #97 |
| Daemon on at the first `stt` run | no | — | #98 |
| Daemon has its own Accessibility + Microphone | no | — | #98, #97 |
| No wrapped-row corruption in a ~50-column terminal | yes | pty captures at 50 columns, not yet on Alex's phone | #100 |
| Inviting idle line | yes | unit tests only | #91 |

## 10. Open decisions

- Beyond the fn-alone exception, do press and hold need different rules above the shared
  floor, and which? — #101.
- Signed helper application: Developer ID signing or ad-hoc; name; where it is built — #99.

Decided on 2026-09-29 and recorded above (each reversible): fn alone for `hotkey_hold` only,
with the Globe-key advice (2.4, #101); the daemon enables itself at the first `stt` run on
macOS (section 4, #98); narrow-terminal corruption is a stt bug with no line-oriented mode
needed (7.2, #100).
