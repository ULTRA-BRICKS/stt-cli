"""The burst's notification (#99): stt's own `stt.app`, terminal-notifier 3.x, osascript.

Nothing here needs a screen. The helper and terminal-notifier are replaced by small shell
scripts at the paths stt looks in, so every test goes through a real process started with
the real argv, and reads a real answer from its standard output. The terminal-notifier stand-in
parses its arguments the way 3.x does (`Terminal Notifier/AppDelegate.m`: a button only for
an argument exactly equal to `-action`), which is what makes the flag stt passes a fact the
test can check rather than a string it echoes back.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import stat
import sys
from pathlib import Path

import pytest

from stt_cli.live import helper, mic_service, permissions, status
from stt_cli.live.dictation import _race_burst_signals

_FAKE_HELPER = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_HELPER_ARGV"
if [ -n "$FAKE_HELPER_SAYS" ]; then echo "$FAKE_HELPER_SAYS"; exit 0; fi
trap 'echo withdrawn > "$FAKE_HELPER_MARK"; kill $pid 2>/dev/null; exit 0' TERM
sleep 30 & pid=$!
wait $pid
"""

# terminal-notifier 3.x: titles come only from an argument exactly equal to `-action`;
# without one there is no button, nothing to wait for, and nothing printed.
_FAKE_TERMINAL_NOTIFIER = """#!/bin/sh
if [ "$1" = "-version" ]; then echo "terminal-notifier ${FAKE_TN_VERSION:-3.1.0}."; exit 0; fi
printf '%s\\n' "$@" >> "$FAKE_TN_ARGV"
while [ $# -gt 0 ]; do
  if [ "$1" = "-action" ] && [ $# -gt 1 ]; then echo "${2%%,*}"; exit 0; fi
  shift
done
exit 0
"""
_FAKE_OSASCRIPT = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_OSASCRIPT_ARGV"
"""


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Mac with nothing installed: an empty Applications folder (conftest), and a PATH
    holding only a fake `osascript` and the system directories."""
    bin_dir = tmp_path / "bin"
    _script(bin_dir / "osascript", _FAKE_OSASCRIPT)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    monkeypatch.setenv("FAKE_OSASCRIPT_ARGV", str(tmp_path / "osascript.argv"))
    monkeypatch.setenv("FAKE_HELPER_ARGV", str(tmp_path / "helper.argv"))
    monkeypatch.setenv("FAKE_HELPER_MARK", str(tmp_path / "helper.mark"))
    monkeypatch.setenv("FAKE_TN_ARGV", str(tmp_path / "tn.argv"))
    monkeypatch.delenv("FAKE_HELPER_SAYS", raising=False)
    monkeypatch.setattr(helper, "_MACOS", True)
    return tmp_path


def _install_helper(digest: str | None = None) -> Path:
    app = helper.app_path()
    info = helper.info_plist()
    if digest is not None:
        info[helper._DIGEST_KEY] = digest
    (app / "Contents").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return _script(helper.executable(app), _FAKE_HELPER)


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


# ── the helper's protocol ──────────────────────────────────────────────────────────────


async def test_the_helper_is_started_with_the_burst_text_and_its_stop_ends_the_burst(
    machine: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exe = _install_helper()
    monkeypatch.setenv("FAKE_HELPER_SAYS", "STOP")
    text = "listening on Mic — press cmd+shift+space again, or click Stop, to finish"

    assert await status.stop_notification(text, quiet=False) is True

    argv = _lines(machine / "helper.argv")
    assert argv[0] == "notify"
    assert argv[1] == "--burst" and argv[2], "every notification has an id of its own"
    assert argv[3:] == ["--title", "stt", "--body", text]
    assert "Stop clicked on the notification" in capsys.readouterr().err, "the daemon log says so"
    assert not (machine / "osascript.argv").exists(), "one notification, not two"
    assert exe == helper.current()


@pytest.mark.parametrize("answer", ["DISMISSED", "CLOSED", "DENIED", ""])
async def test_only_the_stop_button_is_a_request_to_stop(
    machine: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    _install_helper()
    if answer:
        monkeypatch.setenv("FAKE_HELPER_SAYS", answer)
    else:
        # A helper that exits without a word (crashed): not a stop either.
        _script(helper.executable(), "#!/bin/sh\nexit 3\n")

    assert await status.stop_notification("listening", quiet=False) is False


async def test_notifications_turned_off_for_stt_still_say_how_to_stop_through_osascript(
    machine: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_helper()
    monkeypatch.setenv("FAKE_HELPER_SAYS", "DENIED")

    assert await status.stop_notification("listening — Ctrl-C to finish", quiet=False) is False

    assert "Ctrl-C to finish" in "\n".join(_lines(machine / "osascript.argv"))
    assert "System Settings > Notifications > stt" in capsys.readouterr().err


async def test_a_burst_that_ends_another_way_withdraws_the_helper_notification(
    machine: Path,
) -> None:
    """The hotkey ends the burst: `_race_burst_signals` cancels the notification, and the
    helper is told (SIGTERM) so that it takes its Stop button off the screen."""
    _install_helper()

    class _Dictating:
        def stop(self) -> None:  # pragma: no cover - the microphone ends on its own here
            raise AssertionError("the notification did not ask to stop")

    async def microphone() -> str:
        while not (machine / "helper.argv").exists():
            await asyncio.sleep(0.01)
        return "said"

    mic = asyncio.create_task(microphone())
    notif = asyncio.create_task(status.stop_notification("listening", quiet=False))

    assert await _race_burst_signals(_Dictating(), mic, notif) == "said"  # type: ignore[arg-type]

    assert _lines(machine / "helper.mark") == ["withdrawn"]
    assert notif.cancelled()


async def test_a_stale_helper_is_not_asked_to_notify(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy built from older source may speak an older protocol; the notification then goes
    out without a button, and `stt doctor` says to rebuild."""
    _install_helper(digest="built-from-something-else")
    monkeypatch.setenv("FAKE_HELPER_SAYS", "STOP")

    assert helper.state() == "stale"
    assert await status.stop_notification("listening — Ctrl-C to finish", quiet=False) is False
    assert not (machine / "helper.argv").exists()
    assert "Ctrl-C to finish" in "\n".join(_lines(machine / "osascript.argv"))


def test_helper_state_follows_the_installed_copy(machine: Path) -> None:
    assert helper.state() == "missing"
    assert helper.current() is None and helper.installed() is None
    _install_helper(digest="old")
    assert helper.state() == "stale"
    assert helper.current() is None
    assert helper.installed() == helper.executable()
    _install_helper()
    assert helper.state() == "current"
    assert helper.current() == helper.executable()


# ── without the helper ─────────────────────────────────────────────────────────────────


async def test_terminal_notifier_3_gets_the_singular_action_option(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(machine / "bin" / "terminal-notifier", _FAKE_TERMINAL_NOTIFIER)

    assert await status.stop_notification("listening — Ctrl-C to finish", quiet=False) is True

    argv = _lines(machine / "tn.argv")
    assert argv[argv.index("-message") + 1] == "listening — Ctrl-C to finish"
    assert "-actions" not in argv


async def test_terminal_notifier_s_banner_is_removed_when_the_burst_ends(
    machine: Path,
) -> None:
    _script(machine / "bin" / "terminal-notifier", _FAKE_TERMINAL_NOTIFIER)

    await status.close_stop_notification(quiet=False)

    assert _lines(machine / "tn.argv") == ["-remove", "stt-mic-stop"]


async def test_with_no_button_anywhere_the_notification_still_says_how_to_stop(
    machine: Path,
) -> None:
    hint = status.stop_hint("Ctrl-C in the terminal")
    assert hint == "Ctrl-C in the terminal to finish", "no Stop button is promised"

    assert await status.stop_notification(f"listening on Mic — {hint}", quiet=False) is False

    script = "\n".join(_lines(machine / "osascript.argv"))
    assert "display notification" in script and "Ctrl-C in the terminal to finish" in script


async def test_quiet_posts_no_notification_at_all(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_helper()
    monkeypatch.setenv("FAKE_HELPER_SAYS", "STOP")

    assert await status.stop_notification("listening", quiet=True) is False

    assert not (machine / "helper.argv").exists()
    assert not (machine / "osascript.argv").exists()


def test_the_stop_button_is_promised_only_where_one_can_be_drawn(machine: Path) -> None:
    assert status.stop_hint("Ctrl-C in the terminal") == "Ctrl-C in the terminal to finish"
    _install_helper()
    assert (
        status.stop_hint("Ctrl-C in the terminal")
        == "Ctrl-C in the terminal, or click Stop, to finish"
    )


# ── the daemon's principal ─────────────────────────────────────────────────────────────


def test_launchd_starts_the_helper_which_starts_stt_once_it_is_installed(machine: Path) -> None:
    before = mic_service._serve_argv([])
    assert before[0] != str(helper.executable())

    _install_helper(digest="an older build still carries the daemon")
    after = mic_service._serve_argv([])

    assert after[:3] == [str(helper.executable()), "run", "--"]
    assert after[3:] == before, "the command it starts is exactly the one launchd ran before"


def test_under_the_helper_the_daemon_asks_for_stt_not_for_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(permissions.HELPER_ENV, raising=False)
    assert permissions.principal_key() == os.path.realpath(sys.executable)

    app = tmp_path / "stt.app"
    monkeypatch.setenv(permissions.HELPER_ENV, str(app))
    assert permissions.principal_name() == "'stt'"
    assert permissions.principal_key() == os.path.realpath(app)


def test_the_bundle_names_stt_and_explains_the_microphone() -> None:
    info = helper.info_plist()
    assert info["CFBundleIdentifier"] == helper.BUNDLE_ID
    assert info["CFBundleName"] == "stt"
    assert info["CFBundleExecutable"] == helper.EXECUTABLE
    assert info["LSUIElement"] is True
    assert "stt" in str(info["NSMicrophoneUsageDescription"])


async def test_terminal_notifier_2_is_not_taken_for_a_stop_button(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2.x draws no buttons and has no `-action`: promising "click Stop" there would be a
    promise the screen cannot keep, so the burst falls back to the plain notification."""
    _script(machine / "bin" / "terminal-notifier", _FAKE_TERMINAL_NOTIFIER)
    monkeypatch.setenv("FAKE_TN_VERSION", "2.0.0")

    assert status.stop_hint("Ctrl-C in the terminal") == "Ctrl-C in the terminal to finish"
    assert await status.stop_notification("listening — Ctrl-C to finish", quiet=False) is False
    assert not (machine / "tn.argv").exists()
    assert "Ctrl-C to finish" in "\n".join(_lines(machine / "osascript.argv"))


_FAKE_SECURITY = """#!/bin/sh
# `security list-keychains -d user [-s K...]`, keeping the list in a file and printing each
# entry resolved the way the real one does (`/tmp` comes back as `/private/tmp`).
state="$FAKE_SECURITY_LIST"
[ "$1" = "list-keychains" ] || exit 1
if [ "$4" = "-s" ]; then
  shift 4; : > "$state"; for k in "$@"; do echo "$k" >> "$state"; done; exit 0
fi
while read -r k; do
  d=$(cd "$(dirname "$k")" && pwd -P); echo "    \\"$d/$(basename "$k")\\""
done < "$state"
"""


def test_the_signing_keychain_is_on_the_search_list_only_while_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codesign` finds an identity only on the user's keychain search list, so stt's own
    keychain goes on it for one signature. It must come off again even when the path it
    was added by is printed back resolved — the first build left it on the list for good."""
    bin_dir = tmp_path / "bin"
    _script(bin_dir / "security", _FAKE_SECURITY)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    listed = tmp_path / "list"
    monkeypatch.setenv("FAKE_SECURITY_LIST", str(listed))
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real)
    login = str(real / "login.keychain-db")
    listed.write_text(login + "\n")
    ours = tmp_path / "linked" / "stt-signing.keychain-db"

    with helper._on_search_list(ours):
        during = helper._search_list()
        assert os.path.realpath(ours) in map(os.path.realpath, during)

    assert [os.path.realpath(k) for k in helper._search_list()] == [os.path.realpath(login)]
