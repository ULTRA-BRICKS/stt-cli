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
| `stt mic --check` | Opens the microphone and reports levels against the gate, after the same banner; like any other `stt` command it can be the first run that turns the daemon on (section 4) | Shipped (#85, [#105](https://git.hyperide.ai/ultrabricks/stt-cli/issues/105)) |
| `stt mic --list-devices` | Lists microphones; no banner, no microphone | Shipped |
| `stt mic run` | The daemon in the foreground of this shell | Shipped ([#21](https://git.hyperide.ai/ultrabricks/stt-cli/issues/21)) |
| `stt mic start` / `stop` / `status` | Background daemon lifecycle | Shipped (#21, [#54](https://git.hyperide.ai/ultrabricks/stt-cli/issues/54)) |
| `stt mic enable` / `disable` | Install/remove launchd autostart and start/stop now; records an explicit decision (section 4) | Shipped (#21, #87) |

The six lifecycle words are dispatched only when they are the first argument
(`_LIFECYCLE`, `stt_cli/commands/mic.py:64`).

**Help contents.** `stt mic --help` must name the lifecycle subcommands, the permissions
needed and where to look when nothing appears (shipped). `stt mic run --help` must state every
hotkey default (shipped: `--hotkey-press` default `cmd+shift+space`, `--hotkey-hold` unset,
`--hotkey-stop` default `same`). Both help screens list the **allowed modifiers**
(`hotkey.MODIFIER_RULE`, shipped by
[#101](https://git.hyperide.ai/ultrabricks/stt-cli/issues/101)).

- Shipped (#99): `stt mic --help` and `stt mic run --help` promise the notification's Stop
  button drawn by stt's own helper app, and `stt mic helper` builds it (section 6.2).

## 2. Hotkey bindings

### 2.1 Settings

| Key | Flag | Default | Unset means | Disable with |
| --- | --- | --- | --- | --- |
| `hotkey_press` | `--hotkey-press` | `cmd+shift+space` (`DEFAULT_PRESS_HOTKEY`, `stt_cli/live/hotkey.py:88`) | use the default | `none`, `off` or `""` (`_DISABLE_WORDS`, `hotkey.py:96`) |
| `hotkey_hold` | `--hotkey-hold` | none | disabled | same words |
| `hotkey_stop` | `--hotkey-stop` | `same` (`STOP_SAME`, `hotkey.py:100`) | re-press of the chord that started a press burst | `same`, or the disable words (both mean "no extra chord") |

`hotkey_press`, `hotkey_hold` and `hotkey_stop` are independent settings, all live in one
daemon (`stt_cli/config.py:145-151`). A press binding arms on a press and disarms on the next
press of the same chord; a hold binding arms on press and disarms on release. `hotkey_stop` is
Alex's "configurable stop hotkey defaulting to the same as the trigger" (shipped, #101): set
to a chord it also ends any press burst, is checked with the press rule, and never applies to
a hold burst, which ends on release. While idle it starts nothing.

**Cross-binding rule (shipped):** no two of the three bindings may share a physical key, even
with different modifiers, because a key release reports only the key code
(`same_keycode`, `hotkey.py:507`; `refuse_shared_key`, `:541`). Enforced at `stt config set`
time (`hotkey.validate`, `hotkey.py:584`), at flag time for `enable`/`start`/`run`, and again
at daemon start for a hand-edited config.

### 2.2 Validators

**Shipped ([#101](https://git.hyperide.ai/ultrabricks/stt-cli/issues/101)):**
`hotkey.parse(spec, binding=...)` (`hotkey.py:315`) checks a spec in layers, and every
refusal starts with the binding's name, says which rule it broke, and ends with the allowed
modifiers (section 2.3):

| Check | Applies to | Rule | Why |
| --- | --- | --- | --- |
| Shared floor (`_check_floor`, `hotkey.py:366`) | every chord with a key | at least one of `cmd`/`ctrl`; a bare key is refused | The tap is listen-only: it never swallows the real event, so a chord without `cmd`/`ctrl` is also ordinary typing or navigation (`shift+d` types "D", `opt+e` starts "é", `fn+left` is Home) |
| Press rule (`_check_press`, `:399`) | `hotkey_press`, `hotkey_stop` | a key is required (no lone modifier); not one of the editing chords people press while dictating (`cmd+z`, `cmd+shift+z`, `cmd+x`/`c`/`v`/`a`/`s`, the `cmd`+arrow jumps, `cmd+delete`); the tap never lets an autorepeat key-down toggle | Each press opens or closes the microphone, so an accidental one (a paste, a tap of fn) would silently open it |
| Hold rule (`_check_hold`, `:420`) | `hotkey_hold` | a key chord needs only the floor; a lone modifier is allowed only when it is `fn`, and only on macOS | The release is the tap's job (`tap.Holds`, `stt_cli/live/tap.py:115`): it ends on the key's key-up whatever the modifiers say, also the moment one of the chord's modifiers is let go, and an autorepeat never re-arms |

Decided on #101: beyond fn alone, press and hold need no different rule above the floor for a
key chord — except that only press refuses the editing chords (held for push-to-talk, `cmd+v`
repeats a paste the user sees and lets go of; as a toggle it opens the microphone silently).
`hotkey.py`'s module docstring states this, and `tests/test_hotkey.py` has a case per rule,
including a spec accepted for one binding and refused for the other (`fn`, `cmd+v`).

The bare-`fn` refusal for a press binding now suggests `cmd+shift+space` (itself accepted)
and points at `hotkey_hold` for fn alone; it used to suggest `cmd+shift+fn`, which was refused.

### 2.3 Allowed modifiers

| Platform | Strong (one required) | Also allowed with a strong one | Not available |
| --- | --- | --- | --- |
| macOS | `cmd`, `ctrl` | `shift`, `opt` (`option`, `alt`), `fn` | — |
| Windows (future, [#76](https://git.hyperide.ai/ultrabricks/stt-cli/issues/76)) | `ctrl`, `win` (to decide) | `shift`, `alt` | `fn` |
| Linux (future, [#64](https://git.hyperide.ai/ultrabricks/stt-cli/issues/64)) | `ctrl`, `super` (to decide) | `shift`, `alt` | `fn`, unless the device reports `KEY_FN` |

Keys: letters, digits, the punctuation row, `space`, `tab`, `return`, `delete`, arrows,
`f1`–`f12`, named by US-ANSI physical position. `escape` is deliberately not a hotkey key.

This list is `hotkey.MODIFIER_RULE` (`hotkey.py:105`), printed in `stt mic --help`, `stt mic
run --help`, the check banner, the config screen's hint for the hotkey fields, and every
refusal message (#101).

### 2.4 `fn` policy

`fn` is a modifier bit (`_FN = 1 << 23`, `hotkey.py:131`) accepted alongside `cmd`/`ctrl` in
any chord (e.g. `ctrl+fn+f1`), and alone for `hotkey_hold`.

**Shipped — fn-alone hold push-to-talk** ([#101](https://git.hyperide.ai/ultrabricks/stt-cli/issues/101),
decided 2026-09-29, reversible): hold the Globe/fn key alone, speak, release — the style of
macOS Dictation and several open-source Mac dictation tools.

- Scope: `fn` alone is allowed for `hotkey_hold` only, and on macOS only. `hotkey_press` (and
  `hotkey_stop`) still require `cmd` or `ctrl`: fn is tapped on its own far too often in
  ordinary use for a single tap to be a safe trigger.
- Detection: the tap asks for `kCGEventFlagsChanged` whenever a hotkey is configured.
  `tap.Holds` reports fn alone as down when fn is the only modifier held, and as released when
  fn comes up — or at once, as a cancel, when any key-down (other than fn's own key codes 63
  and 179) or any other modifier joins it: that was `fn+arrow`/`fn+delete` navigation or the
  start of a chord, and nothing more is reported until fn itself comes up.
- Arming: the daemon opens the microphone only once fn has been held alone for
  `LONE_MODIFIER_ARM_SECONDS` (0.3 s, `hotkey.py:115`; `dictation._held_alone`), so a quick
  `fn+arrow` never flashes the microphone. The value is not yet tuned on real hands — that is
  the live acceptance still open on #101.
- Other bindings while fn is held: a chord matches only its exact modifier set, so with fn
  down `cmd+shift+space` fires nothing (it only cancels the fn hold); a chord that includes
  fn, such as `ctrl+fn+f1`, still fires. `tap.Holds` sends fn's cancel before any such
  press, and `_held_alone` hands back a press it reads inside the window instead of
  dropping it, so `_idle_loop` still acts on it.
- The Globe key setting: the tap cannot suppress macOS's own Globe action (switch input
  source, emoji picker, or double-press Dictation), so fn-alone hold works cleanly only when
  System Settings → Keyboard → "Press 🌐 key to" is **Do Nothing**, read with
  `defaults read com.apple.HIToolbox AppleFnUsageType` (`0` = Do Nothing; `1` input source,
  `2` emoji, `3` dictation; absent = system default, not 0; `hotkey.read_fn_usage`). When
  fn-alone hold is configured and the value is not `0`, stt says so in plain words, naming
  that exact place (`GLOBE_KEY_ADVICE`) — when the binding is set (`stt config set`, the
  config screen, `stt mic enable`/`start --hotkey-hold fn`), in the check banner (section 3),
  and when the daemon comes up. It does not refuse the binding and does not change the
  preference itself (that needs a logout or a private API).

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

So `fn` is a macOS-only entry in the allowed list (section 2.3): the Windows and Linux
adapters ([#60](https://git.hyperide.ai/ultrabricks/stt-cli/issues/60),
[#64](https://git.hyperide.ai/ultrabricks/stt-cli/issues/64),
[#76](https://git.hyperide.ai/ultrabricks/stt-cli/issues/76)) must omit it unless a device
proves it reports `KEY_FN`, and off macOS `parse` refuses `fn` alone today.

## 3. The check banner (run not as the daemon)

Printed to standard error, once, right before an ad-hoc dictation or `--check` opens the
microphone (`_adhoc_banner`, `stt_cli/commands/mic.py:134`). Not printed for
`--list-devices` or bare `stt mic`; suppressed by `--quiet`. It must never crash the
dictation behind it: every probe failure degrades to a plain line.

| Line | Source | Shipped text (examples) |
| --- | --- | --- |
| Daemon state | `_daemon_status_line`, `mic.py:161` | `daemon:  running (pid N) [autostart on]` · `daemon:  not running` · `daemon:  enabled, but not currently running (last exit code 8) — see stt mic status` · `daemon:  status unavailable — …` (inner command quotes omitted here) |
| One line per binding | `_hotkey_lines`, `mic.py:211` | `hotkey:  cmd+shift+space (toggle) — the built-in default, not yet configured`, then `change it with stt config set hotkey_press ..., or stt config for the interactive screen` · `hotkey:  hold disabled — stt config set hotkey_hold ... to arm it` · `hotkey:  stop — press the toggle chord again (hotkey_stop is same)` |
| Allowed modifiers | `_hotkey_lines` (#101) | `hotkey:  allowed on macOS: cmd or ctrl (at least one), plus any of shift, opt, fn, and one key (…); fn alone only for hotkey_hold` |
| Globe key setting | `_globe_key_line`, `mic.py:248` (#101) | only when fn-alone hold is configured and `AppleFnUsageType` is not `0`: `hotkey:  fn alone is your push-to-talk key: set System Settings > Keyboard > "Press 🌐 key to" to "Do Nothing" — …` |
| How to stop this session | printed by the session's first status note today (`listening on <device> — Ctrl-C to finish …`) | must match section 6 |

Tests: `tests/test_hotkey.py::test_check_flag_also_prints_the_ad_hoc_banner`
(the banner is printed on `--check`, stubbing the two line builders),
`test_ad_hoc_dictation_prints_the_check_banner_before_opening_the_microphone`,
`test_quiet_suppresses_the_ad_hoc_check_banner`,
`test_list_devices_never_shows_the_ad_hoc_banner`, and the line-content tests
`test_hotkey_lines_shows_the_configured_chord_and_how_to_change_it`,
`test_hotkey_lines_names_the_built_in_default_when_unconfigured`,
`test_hotkey_lines_degrades_on_a_hand_edited_unusable_chord`.

## 4. The daemon is on by default

`mic_daemon_autostart` (`stt_cli/config.py:171`) is a decision record, not a switch:

| Value | Meaning | Who writes it |
| --- | --- | --- |
| `None` | undecided — eligible for automatic enable | default; `stt config unset mic_daemon_autostart` |
| `"retry"` (`MIC_AUTOSTART_RETRY`, `config.py:55`) | autostart was installed but the daemon was not verified alive; a later `stt` command tries again after the backoff below | `_maybe_autostart_daemon` |
| `True` | on, and the daemon was verified alive (or enabled explicitly) | `_maybe_autostart_daemon`, `stt mic enable` |
| `False` | the user said no | `stt mic disable` |

**Trigger (shipped, [#105](https://git.hyperide.ai/ultrabricks/stt-cli/issues/105),
implementing the decision on [#98](https://git.hyperide.ai/ultrabricks/stt-cli/issues/98)):**
the daemon enables itself at the **first `stt` run on macOS**, unless the user explicitly
disabled it. `cli.main` calls `mic.autostart_after(argv)` after every dispatched command
except `--help`/`help`/`-h`, `--version`/`version`/`-V`, and a command interrupted with
Ctrl-C; `autostart_after` itself skips bare `stt mic`, the six lifecycle words (a first
`stt mic disable` or `enable` has already decided by the time it looks) and the two hidden
entries (`__serve`, the daemon; `__autostart`, the worker below). A command that ends inside
argument parsing — its own `--help`, such as `stt mic --help`, or a usage error — leaves
through argparse's `SystemExit`, which `guard` does not catch, so it never reaches the hook
either. Any other command that ran counts, whatever its exit code — a mistyped command name
(`stt doctorr`, exit 4) included: the person has started using stt either way. The hook runs
after the command, so the command's own output and exit code are settled first. It no
longer waits for a successful ad-hoc dictation (#87): that dictation was itself blocked on
the permissions the daemon can only ask for once it is running.

- **Decided (`True` or `False`):** one read of `config.json`. No lock, no `launchctl`, no
  worker.
- **Undecided (`None`, or `"retry"`) and due:** stamp the attempt in
  `<STT_HOME>/mic-autostart-attempts.json`, start `stt mic __autostart` detached (its own
  session, standard streams to `/dev/null`), and print one line to standard error naming the
  interpreter to turn on under Privacy & Security > Accessibility — unless the command was
  given `-q`/`--quiet` (`stt mic` and a transcription have it; `_asked_quiet`), which drops
  the line but still starts the worker. The command does not wait for the worker.
- **Backoff:** the first attempt is always due. After an attempt that did not end with a
  verified-alive daemon, the next is due 10 minutes after it was requested, doubling after
  each further attempt, capped at one day (`_AutostartAttempts.due`). A verified daemon resets
  the count.
- **The worker** (`_autostart_worker`) takes the decision lock, skips if another worker has
  already finished since the latest request (two commands started together may each start
  one), runs `_maybe_autostart_daemon` with its standard error appended to the daemon's log,
  and records the attempt as finished. `_maybe_autostart_daemon` re-reads the record under
  the lock, so a `stt mic disable` that lands between the spawn and the worker still wins.
  It enables launchd autostart and then polls the same launchd probe `stt mic status` uses,
  up to `_AUTOSTART_PROBE_ATTEMPTS = 6` times, 0.5 s apart (on macOS only — off macOS it
  probes once). Verified alive → `True`; not verified → `"retry"`, with the cause in the
  log. The whole check-enable-probe-persist sequence holds one lock shared with
  `enable`/`disable`.
- **Without `agenttools_service`** nothing is stamped, started or printed.

Tests: the trigger, the backoff and the worker in `tests/test_autostart.py`; the
enable-probe-persist sequence in `tests/test_hotkey.py`. The autouse fixture in
`tests/conftest.py` refuses any `launchctl` process and any autostart worker a test starts,
and fails the test at teardown even when the code under test swallowed the refusal.

**Liveness is not permission.** The launchd-spawned daemon is its own macOS privacy
principal: the interpreter in `ProgramArguments` of
`~/Library/LaunchAgents/com.agenttools.stt.mic.plist`, separate from any terminal. A daemon
can be installed, alive, and still unable to type or hear.

- Gap: the daemon exits on missing Accessibility and launchd restarts it in a loop instead of
  asking for its own grant and waiting —
  [#98](https://git.hyperide.ai/ultrabricks/stt-cli/issues/98).
- Gap: nobody has seen the launchd-spawned daemon type after a hotkey press —
  [#97](https://git.hyperide.ai/ultrabricks/stt-cli/issues/97) (blocked by #98).
- Gap: nobody has watched the first-run trigger turn on a real launchd daemon on a fresh
  Mac; the live check used a fake `launchctl` and a throwaway `HOME` (#105).

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

| Session | Notification text (with a Stop button) | Without one |
| --- | --- | --- |
| Daemon, press | `listening on <device> — press <chord> again, or click Stop, to finish` | `… press <chord> again to finish` |
| Daemon, hold | `listening on <device> — release <chord>, or click Stop, to finish` | `… release <chord> to finish` |
| Ad-hoc | `listening on <device> — Ctrl-C in the terminal, or click Stop, to finish` | `… Ctrl-C in the terminal to finish` |

The wording comes from one place, `status.stop_hint`, and the Stop button is named only when
one can be drawn. Clicking Stop ends only the current burst; the daemon keeps running, and
the daemon's log gets the line `Stop clicked on the notification — finishing this burst`. A
burst that ends any other way withdraws its notification, so no stale Stop button is left
behind. The notification is the burst's only one at the start (the open chime still plays);
`stopped — N word(s) dictated` follows through `osascript` when it ends.

**Shipped (#99): `stt.app`, stt's own helper application.** Source
`stt_cli/helper_app/main.swift`, built and installed by `stt_cli/live/helper.py`.

- A minimal bundle, identifier `ai.hyperide.stt`, name "stt", no Dock icon (`LSUIElement`),
  asking for the "Alerts" style (`NSUserNotificationAlertStyle`, so the Stop button stays on
  screen for the whole burst — macOS 26 was seen to start it on "Banners" regardless, which
  slide away after a few seconds; `stt doctor` prints the style and where to switch it),
  `NSMicrophoneUsageDescription` naming stt. It uses
  `UNUserNotificationCenter` with one category, `STT_BURST`, carrying one action, `STOP`
  ("Stop"), and `customDismissAction` so a dismissal is reported.
- Built on the user's Mac from the Swift source shipped in the package (`swiftc` from the
  Command Line Tools, which Homebrew already requires; no download, no Python dependency),
  and installed as `~/Applications/stt.app`. `stt mic helper [--rebuild]` is the documented
  build step; `stt mic enable`/`start`/`run`, the first-run autostart worker and
  `stt doctor --fix` run it when the installed copy is missing or built from older source
  (the source digest is stamped into its `Info.plist` as `STTSourceDigest`). CI runs on
  Linux and does not build it. The first notification raises macOS's one-time "stt would
  like to send notifications" prompt.
- Protocol (`status.stop_notification`): stt starts
  `stt.app/Contents/MacOS/stt notify --burst <pid-n> --title stt --body <text>`; it prints
  exactly one line and exits — `STOP` (the button), `DISMISSED` (the notification was
  closed), `CLOSED` (its body was clicked, or it could not be posted) or `DENIED`
  (notifications are off for stt: stt then says where to turn them on and posts the same
  text through `osascript`). Only `STOP` stops the burst. When the burst ends another way,
  `_race_burst_signals` cancels the wait and stt sends SIGTERM; the helper removes its
  notification before exiting (killed after two seconds if it has not). It also withdraws
  when its parent process disappears, and removes any stale `stt-burst-*` notification
  before posting a new one.
- A copy built from older source is not asked to notify (it may speak an older protocol);
  `stt doctor` reports it as out of date.
- Fallbacks, in order: terminal-notifier 3.x with `-action Stop` (singular — 3.x ignores
  `-actions`, which is what the code passed before #99 and why no button was ever drawn),
  withdrawn with `-remove stt-mic-stop`; then `osascript display notification` with the
  same how-to-stop text and no button. `stt doctor`, the daemon's ready line and the ad-hoc
  session's first line say when there is no Stop button and that `stt mic helper` adds one.
- Tests: `tests/test_helper_app.py` runs stand-in scripts at the helper's and
  terminal-notifier's paths, so each test starts a real process with the real argv; the
  terminal-notifier stand-in parses options the way 3.x does.
- The same helper is the natural home for an always-visible microphone indicator
  ([#14](https://git.hyperide.ai/ultrabricks/stt-cli/issues/14)) and for the dictionary
  suggestion's Install action ([#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88)).
- Deferred: Windows (a toast notification with a button, through the WinRT
  `ToastNotification` API) and Linux (a freedesktop notification action, through
  `notify-send --action` or D-Bus) adapters, with the platform tickets
  [#60](https://git.hyperide.ai/ultrabricks/stt-cli/issues/60), #64 and #76.

**The helper is also the daemon's privacy principal (#99, from #98).** Once `stt.app` is
installed, `mic_service._serve_argv` puts `stt.app/Contents/MacOS/stt run --` in front of
the daemon's command, so the LaunchAgent starts the helper and the helper starts stt's
Python as its child (a child, not an `exec`: macOS holds the process launchd started
responsible for its children). The helper forwards SIGTERM, SIGINT and SIGHUP, exits with
the child's status, and tells it `STT_HELPER_APP=<bundle>`; `permissions.principal_name`
then names "stt" and the ask-once ledger is keyed on the bundle. A daemon enabled before
the helper existed keeps running as Python until `stt mic disable && stt mic enable`
(`stt mic helper` says so).

**Signing, decided on #99 (reversible).** macOS remembers a grant against the signature's
designated requirement. This Mac has no Developer ID (`security find-identity -v -p
codesigning` finds 0 valid identities), so:

| Signed with | Designated requirement | Grant survives a Python upgrade | Survives a rebuild of the helper |
| --- | --- | --- | --- |
| A Developer ID, when the keychain has one | team identifier | yes | yes |
| A certificate stt makes on this Mac (default here) | `identifier "ai.hyperide.stt" and certificate leaf = H"…"` | yes | yes |
| Ad hoc, if making the certificate fails | the binary's own hash | yes | no |

The local certificate is self-signed (`CN=stt local code signing`, code-signing usage only),
made with `openssl` into a keychain of its own at `STT_HOME/signing/`, with a random
password beside it (owner-only). `codesign` accepts it untrusted; it only has to find it,
so the keychain is put on the user's search list for the one signature and taken off
again. Nothing asks for a password or shows a dialog. The trade-off: anything running as
this user can read that keychain and sign a binary macOS would treat as stt, inheriting its
Accessibility and Microphone. That is no wider than before — the helper runs stt's Python,
which any such process can already edit, and before the helper the principal was Python
itself. Deleting `STT_HOME/signing` makes a new certificate at the next build, and the
grants have to be given once more.

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
| Different validators per binding, modifier list, fn-alone hold, `hotkey_stop` | yes | no — [#124](https://git.hyperide.ai/ultrabricks/stt-cli/issues/124); typing while a hold's modifiers are held is [#125](https://git.hyperide.ai/ultrabricks/stt-cli/issues/125) | #101 |
| Esc-Esc removed | yes | unit tests only | #79 |
| Always-available Stop button + how-to-stop text | yes | see #99: a real `stt.app` notification on Alex's Mac, and the burst race it ends | #99 |
| Daemon on by default, liveness-verified | yes | no — daemon crash-looping on Alex's Mac | #87, #98, #97 |
| Daemon on at the first `stt` run | yes | a detached worker spawned and recorded its outcome, against a fake `launchctl` and a throwaway `HOME` | #105 |
| Daemon has its own Accessibility + Microphone | yes — `stt.app` is the launchd principal once installed and re-enabled (6.2) | no — needs the daemon moved into `stt.app` and the two grants clicked | #98, #97, #99 |
| No wrapped-row corruption in a ~50-column terminal | yes | pty captures at 50 columns, not yet on Alex's phone | #100 |
| Inviting idle line | yes | unit tests only | #91 |

## 10. Open decisions

- Signed helper application — decided on #99 (6.2): named `stt.app`, bundle identifier
  `ai.hyperide.stt`, built on the user's Mac from source; Developer ID when there is one,
  otherwise a certificate stt makes on the Mac, otherwise ad hoc. Reversible: a Developer ID
  build distributed with stt would replace the local build if stt ever ships binaries.

Decided on 2026-09-29 and recorded above (each reversible): fn alone for `hotkey_hold` only,
with the Globe-key advice, and no other press/hold difference above the floor beyond the
press-only editing-chord refusal (2.2, 2.4, #101); the daemon enables itself at the first
`stt` run on macOS (section 4, #98); narrow-terminal corruption is a stt bug with no
line-oriented mode needed (7.2, #100).
