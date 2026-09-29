# Can `stt` grant itself Accessibility?

**No.** There is no path — on a stock, SIP-on Mac, for an unsigned/ad-hoc-signed CLI like
`bin/stt` — that lets `stt` finish its own macOS Accessibility grant (System Settings >
Privacy & Security > Accessibility) with zero required human clicks. Not by disabling SIP,
not by writing `TCC.db` directly, not by enrolling the Mac in an MDM of its own. The floor
is **one guided user toggle** — the same floor every comparable commercial tool pays (see
the table below). The only exception is **zero toggles when the user's own organization
delivers a PPPC (Privacy Preferences Policy Control) profile through its own,
already-enrolled MDM** — an IT operation, not something `stt` can do to the Mac it is
running on.

This is the settled, evidence-backed answer — reached independently and unanimously from
security, developer-experience, SRE and cost-engineering perspectives — written down once so
the question stops getting re-investigated from scratch every time it comes up.

## Why not

`stt`'s own `live/quartz.py` already states the API ceiling: a process can **ask**
(`AXIsProcessTrustedWithOptions` with the prompt option) and cannot **grant**. That is
Apple's own sanctioned API, and raising the dialog is as far as any process — signed or
not — can take itself. Every other technique that looks like it might get further was
checked against this tree and rejected with a concrete reason, not just "seems risky":

- **`tccutil`** has no grant verb — reset only.
- **Writing `TCC.db` directly** doesn't reach the permission: Accessibility
  (`kTCCServiceAccessibility`) lives in the *system* database
  (`/Library/Application Support/com.apple.TCC/TCC.db`), which is SIP-protected. A
  per-user database write is out of bounds by construction.
- **A local `profiles install` of a PPPC payload** (`Authorization=Allow` for
  `kTCCServiceAccessibility`) does not work outside MDM. Per Apple's own Configuration
  Profile Reference, a Privacy Preferences Policy Control payload's `Allow` is honored
  **only when the profile was installed by an MDM**; a manually-run
  `profiles install -path ...` either refuses or installs a profile whose `Allow` is
  silently ignored. The failure mode is the worst kind: `profiles list` reports the
  profile as installed, and `AXIsProcessTrusted()` is still `false` — a support ticket
  that looks configured and isn't.
- **Standing up a homemade/self-service MDM** (to make the PPPC payload above actually
  apply) requires User-Approved MDM: an APNs push certificate, a reachable MDM server,
  *and* the user clicking "Allow" on enrollment *and* approving it again in System
  Settings. That moves the click from "toggle Accessibility" to "enroll this Mac in an
  MDM this CLI stood up" — heavier, not lighter, and indistinguishable from the
  infostealer persistence pattern this permission exists to stop.
- **AppleScript / Accessibility-driving System Settings' own toggle** is chicken-and-egg:
  controlling System Settings itself requires Accessibility. It also breaks on every
  major System Settings rewrite (Ventura's split, Sequoia, ...) and is the exact
  "unattended keylogger enablement" shape real infostealers (Atomic, Poseidon,
  MacStealer) use once they already have one foothold.
- **A signed wrapper binary that `exec`s the current Python** does not hold the identity
  either: `execve()` replaces the running process image, so the Mach-O TCC actually keys
  off (cdhash / Team ID) becomes Python's again. The only shapes that work are Karabiner's
  (a signed helper binary that *stays running* and owns the tap itself, with Python
  talking to it over an authenticated local socket) or a frozen, notarized app bundle —
  and both are still exactly one user toggle, just one that survives future rebuilds
  instead of churning on every one.
- **A future macOS bug** that let an unsigned, launchd-launched process self-grant with no
  prompt would be exactly that — an OS bug to report, not a technique to productize.

If a $99-signed, notarized `.app` with a dedicated onboarding wizard still cannot skip the
click, an unsigned `#!/usr/bin/env python3` shim launched by `launchd` will not either.

## What every comparable tool actually does on first run

None of the following tools achieve a zero-click grant. All of them are Developer ID (or
Mac App Store) signed `.app` bundles with a stable Team ID — a stronger identity position
than `stt` currently has — and all of them still pay at least one toggle.

| Tool | Identity | First-run flow | Zero-click? |
| --- | --- | --- | --- |
| **Hammerspoon** | Developer ID `.app` | `hs.accessibilityState(true)` calls `AXIsProcessTrustedWithOptions`; docs say restart the app after the toggle (source: `MJAccessibilityUtils.m`, [hammerspoon.org/go](https://www.hammerspoon.org/go/)) | No — one toggle, then relaunch |
| **BetterTouchTool** | Developer ID / MAS `.app` | In-app permissions checklist opens the Accessibility pane via the `x-apple.systempreferences:` URL scheme and polls `AXIsProcessTrusted` until toggled | No — one guided toggle |
| **Karabiner-Elements** | Signed `.app` + privileged helper + DriverKit system extension | Settings window's "Open System Settings" button; *separate* approvals for Accessibility, Input Monitoring, and the DriverKit extension, with distinct TCC rows for `karabiner_grabber` and `karabiner_observer` (source: `pqrs-org/Karabiner-Elements` `SettingsWindow`) | No — several clicks, more than the others because the grants are many and sticky |
| **CleanShot X / Rewind (Limitless)** | Notarized `.app` | Onboarding wizard with a separate Settings hop per TCC service (Accessibility, plus Screen Recording — which has no comparable prompt API and is stricter) | No — one Settings hop per permission |
| **Rectangle / Magnet / Keyboard Maestro / Alfred** | Signed `.app` (Magnet is Mac App Store) | Same `AXIsProcessTrustedWithOptions` prompt / Settings pane pattern | No |
| **espanso** (closest CLI-adjacent product analog — it also types into other windows) | Tried both a bare CLI and an `.app`; ad-hoc/Homebrew-signed in the CLI form | Toggle once, then `brew upgrade` changes the binary's path/cdhash and silently revokes the grant — its GitHub issues are full of "it stopped typing after an upgrade" reports as a result | No — and the grant does not survive upgrades without a stable signed identity |
| **skhd / yabai** (closest CLI-only analog) | Homebrew-installed Mach-O, often ad-hoc-signed | Grant lands on `/opt/homebrew/bin/skhd` if launched directly, or on the terminal app if launched from a shell | No |

**The only zero-*toggle* exception:** an organization's **own, already-enrolled MDM**
pushing a PPPC profile whose `Allow` names a Team-ID-anchored code requirement. That
requires Developer ID signing on `stt`'s side (an ad-hoc cdhash is not a stable code
requirement an admin can pin) and an MDM the *customer* operates — Karabiner is deployed
this way via Jamf in managed fleets. It is IT-operated infrastructure, not a runtime
capability `stt` invokes on itself, and it is out of scope for anything `stt doctor` or
`stt doctor --fix` does today.

## What `stt` actually does today

Plain `stt doctor` already names the gap and its fix directly, without touching anything:

```
  ✗ live dictation (`stt mic`): whisper-server found, Accessibility NOT granted
      fix: grant Accessibility to the terminal application running stt in System Settings > Privacy & Security > Accessibility, then start a new terminal (see https://git.hyperide.ai/ultrabricks/stt-cli/src/branch/main/docs/accessibility.md for why stt cannot do this step itself)
```

`stt doctor --fix` goes one step further and raises Apple's own consent dialog
(`quartz.ask_for_trust`), then waits a few seconds to report whether it landed — it is
deliberately honest that this is a *request*, not a completion.

That one guided click — attributed to the actual application asking — is the ceiling this
tool, or any comparable tool, can reach without help from the user or their organization's
MDM.

### The background daemon asks for itself

A prompt is attributed to the process that raises it, and `stt doctor --fix` runs in a
terminal — so it asks for the terminal. The daemon `stt mic enable` hands to launchd is a
different principal: the interpreter launchd execs (on a Homebrew Mac,
`/opt/homebrew/Cellar/python@3.14/<version>/Frameworks/Python.framework/Versions/3.14/bin/python3.14`,
listed in System Settings as `python3.14`). Measured on 2026-09-29 from a one-shot launchd
job versus the terminal it was submitted from: `AXIsProcessTrusted()` false in both, and
`AVCaptureDevice authorizationStatusForMediaType:` for audio `0` (not determined) under
launchd against `3` (authorized) in the terminal. A terminal grant says nothing about the
daemon.

So the daemon asks (`live/permissions.py`, ticket
[#98](https://git.hyperide.ai/ultrabricks/stt-cli/issues/98)). Started without
Accessibility, it calls `AXIsProcessTrustedWithOptions` with the prompt option — which is
what adds its interpreter to the Accessibility list, so there is a toggle to click — opens
System Settings on Privacy & Security > Accessibility, and stays alive. It re-reads
Accessibility from a fresh child process — every two seconds for five minutes after each
prompt, every ten after that — because a process never sees
its own answer change: on 2026-09-29 the toggle for the waiting daemon was turned on at
12:10:51 (TCC.db), the daemon's own `AXIsProcessTrusted()` still said no at 12:14, and it
never noticed before the reboot at 12:47. A child runs under the daemon's principal, so its
answer is the daemon's grant; the moment it says yes, the daemon replaces itself with a
fresh run of the same command (`execv`: same pid, so launchd sees no restart), which sees
the grant and carries on. Then the
microphone: `requestAccessForMediaType:` when macOS has never asked (its own dialog has the
Allow button), or the Microphone pane when the answer was already no (macOS never asks
twice). Each principal is asked at most once — a ledger under `STT_HOME` records it before
the prompt is shown — so a restart never shows it again; `stt mic --fix` clears the ledger,
and the waiting daemon asks again within one poll, no restart. Before this, the daemon
exited with code 8 and launchd restarted it every ten seconds: 7273 times on one machine.

## When a grant may not carry over

The espanso row above is not a hazard unique to a different tool: `bin/stt`'s shebang
(`#!/usr/bin/env python3`) resolves to the exact same ad-hoc/Homebrew-signed shape. On a
Homebrew-managed Mac that resolves to something like
`/opt/homebrew/Cellar/python@3.14/<version>/Frameworks/Python.framework/Versions/3.14/bin/python3.14`
— an ad-hoc-signed binary whose TCC identity is tied to that exact content hash, not a
stable Team ID. *If* that binary is the principal macOS evaluates, any future rebuild of it
(a `brew upgrade`/`reinstall` that repours the bottle) would make macOS treat the
post-rebuild `python3.14` as a brand-new, never-before-granted principal at the same file
path. That is a documented risk for this class of binary, not something observed for `stt`
yet.

### What was actually observed, and what is still unresolved

On the maintainer's own Mac (Alex's; 2026-09-24 to 2026-09-28) two
invocation contexts disagreed about whether `stt` was trusted:

- **Alex's own terminal** repeatedly reported Accessibility as **granted** — including
  before anyone had suggested a permission was needed.
- **An automation agent's shell on the same machine** (a tmux/agent-harness process tree,
  not a terminal application) reported **NOT granted**. The system `TCC.db`
  (`/Library/Application Support/com.apple.TCC/TCC.db`) held one Accessibility row for the
  resolved `python3.14` path, with `auth_value=0` (denied) and `auth_reason=4` ("System
  Set", i.e. written by `tccd` itself rather than by a click in System Settings).

That discrepancy is **observed**. Its cause is **unresolved**. The agent-shell result lines
up with that denied `python3.14` row; what macOS evaluated in Alex's terminal — the
terminal application as the "responsible" process, or the `python3.14` binary itself (the
skhd row in the table above shows both shapes happen for Homebrew CLIs) — is the open
question, and so is what wrote the denied row. Nothing observed so far shows that a grant
Alex made went stale: the Homebrew rebuild explanation was checked and ruled out (a single
`python@3.14` build in the Cellar, installed seven months earlier, with no
`brew upgrade`/`reinstall` of it in shell history), and no other trigger has been
confirmed.

### What `stt`'s own messages can and cannot tell you

`stt doctor` and `stt mic --check` print the same "NOT granted" line whether a grant was
never made or was made and later stopped applying, and name only "the terminal application
running stt". The background daemon is the exception: it waits instead of exiting, and
`stt mic status` names the entry it is waiting on as System Settings lists it (for example
`'python3.14'`). Grant Accessibility (the same single guided toggle described above) to:

- **for `stt mic` in a terminal** — the terminal application you ran it from;
- **for the background daemon** — the interpreter launchd starts, which is the first entry
  of `ProgramArguments` in `~/Library/LaunchAgents/com.agenttools.stt.mic.plist`
  (`plutil -p` prints it).

[Ticket #80](https://git.hyperide.ai/ultrabricks/stt-cli/issues/80) tracks both halves: the
diagnostics from Alex's own terminal that would settle the discrepancy above, and naming the
concrete principal in `stt`'s messages — telling the two cases apart where that is
technically possible.

