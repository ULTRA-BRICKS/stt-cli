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
