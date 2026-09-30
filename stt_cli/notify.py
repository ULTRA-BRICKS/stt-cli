"""notify — one desktop notification, through whatever the operating system already has.

stt is not an application bundle, so it cannot post a notification of its own. Each platform
has one tool that can post one for a command-line program, and this module builds the command
line for it and runs it:

- macOS: `osascript`'s ``display notification`` — the banner `live/status.py` already shows
  when dictation starts. Fire and forget; it cannot carry a button.
- Linux: `notify-send` (libnotify), or `gdbus` calling the freedesktop notification service
  when libnotify is absent — the order oh-my-pi's `packages/tui/src/desktop-notify.ts` uses.
- Windows: a toast through Windows PowerShell 5.1's WinRT projection, attributed to
  PowerShell's own application id, since stt has none registered. (PowerShell 7 dropped
  the projection, so this is `powershell.exe`, not `pwsh`.)

Only the macOS path has been seen working on a real machine; the Linux and Windows commands
are built to their documented interfaces and their shape is unit-tested.

The text never becomes code. AppleScript source and a D-Bus call each get a quoted literal
(`quoted`), and the PowerShell script is fixed: the title and body reach it as environment
variables, which it XML-escapes itself.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# Long enough for the slowest of these tools to hand the text over; short enough that one
# which wedges does not keep the process that asked alive.
TIMEOUT_SECONDS = 10.0

APP_NAME = "stt"

# PowerShell's own AppUserModelID. A toast must name a registered application, and Windows
# PowerShell is one on every Windows 10 and 11 machine.
_POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_WINDOWS_SCRIPT = "\n".join(
    (
        "$null = [Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]",
        "$null = [Windows.Data.Xml.Dom.XmlDocument,"
        "Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]",
        "$title = [System.Security.SecurityElement]::Escape($env:STT_NOTIFY_TITLE)",
        "$body = [System.Security.SecurityElement]::Escape($env:STT_NOTIFY_BODY)",
        "$text = '<toast><visual><binding template=\"ToastGeneric\">'",
        '$text += "<text>$title</text><text>$body</text></binding></visual></toast>"',
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
        "$xml.LoadXml($text)",
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml",
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{_POWERSHELL_APP_ID}').Show($toast)",
    )
)


@dataclass(frozen=True, slots=True)
class Command:
    """What to run to post one notification, and the variables it reads the text from."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


def command(title: str, body: str, *, platform: str = sys.platform) -> Command | None:
    """The command that posts ``title``/``body`` here, or ``None`` when nothing can."""
    if platform == "darwin":
        script = f"display notification {quoted(body)} with title {quoted(title)}"
        return Command(["osascript", "-e", script])
    if platform.startswith("linux"):
        return _freedesktop(title, body)
    if platform == "win32":
        encoded = base64.b64encode(_WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")
        return Command(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env={"STT_NOTIFY_TITLE": title, "STT_NOTIFY_BODY": body},
        )
    return None


def post(title: str, body: str) -> bool:
    """Show one notification. True when the tool accepted it; never raises.

    "Accepted" is all anyone can know: a user who turned notifications off for `osascript`
    still gets exit 0, and that is their setting, not a failure.
    """
    found = command(title, body)
    if found is None:
        return False
    try:
        result = subprocess.run(
            found.argv,
            env={**os.environ, **found.env} if found.env else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def quoted(text: str) -> str:
    """One double-quoted string literal, with nothing in it that can end the literal early.

    AppleScript and GVariant text write a string the same way — backslash escapes a backslash
    or a quote — so one function serves both, and control characters are dropped from each.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(ch for ch in escaped if ch >= " ") + '"'


def _freedesktop(title: str, body: str) -> Command | None:
    notify_send = shutil.which("notify-send")
    if notify_send:
        # `--` so a body starting with a dash is text, not an option.
        return Command([notify_send, f"--app-name={APP_NAME}", "--", title, body])
    gdbus = shutil.which("gdbus")
    if gdbus:
        # org.freedesktop.Notifications.Notify(app_name, replaces_id, app_icon, summary, body,
        # actions, hints, expire_timeout). gdbus parses each argument as GVariant text, so the
        # strings are passed as quoted literals rather than left to its guess.
        return Command(
            [
                gdbus, "call", "--session",
                "--dest", "org.freedesktop.Notifications",
                "--object-path", "/org/freedesktop/Notifications",
                "--method", "org.freedesktop.Notifications.Notify",
                quoted(APP_NAME), "0", '""',
                quoted(title), quoted(body), "[]", "{}", "-1",
            ]
        )  # fmt: skip
    return None
