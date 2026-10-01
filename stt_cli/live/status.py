"""status — what live dictation uses instead of a menu bar icon.

WHY THERE IS NO ICON
    The obvious design is Apple's: an icon in the menu bar and a notification when dictation
    starts. Both want an application. A menu bar item needs an `NSApplication` event loop
    running inside the process, and a notification that says "stt" rather than "Script
    Editor" needs the whole thing packaged as an app bundle with its own identifier. That is
    a real amount of machinery to tell somebody something they asked for two seconds ago.

    So the state is reported in the two places that cost nothing and are already there. A
    single line in the terminal `stt mic` was started from, rewritten in place, which shows
    what is being heard and whether it has settled. And a short system sound when the
    microphone opens and when it closes, for when that terminal is behind another window —
    which is most of the time, because the whole point is to be typing somewhere else.

    The one thing that did need an application is the notification's Stop button (#99), so
    stt builds a small one of its own, `stt.app` (see `helper.py`), and asks it to post that
    notification — nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import math
import os
import random
import re
import shutil
import subprocess
import sys
import unicodedata
import wave
from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# The cue is generated rather than borrowed. macOS ships fourteen sounds and none of them
# says "the microphone is live": `Tink` is a thin tick that reads as an error, `Pop` and
# `Glass` run past a second and a half, and none has an opposite — the open and close sounds
# were two unrelated noises the user had to learn separately. Two tones do the whole job: the
# same pair rising means on and falling means off, which needs no learning at all, and 180 ms
# of it is over before the first word. Written by hand with `wave` and `math`, so this costs
# no dependency and no shipped asset.
# Both cues start on the same note and part company from there: up means the microphone is
# open, down means it is closed. Siblings rather than two noises to learn separately.
_OPEN_TONES = (262.0, 349.0)
_CLOSE_TONES = (262.0, 208.0)
# Bumped whenever the sound changes, so an old cached file is replaced rather than kept.
_CHIME_VERSION = 4
_TONE_MS = 26
# A plucked string, not a beep. The first version held each tone at full volume and faded it
# linearly at both ends, which is a tone generator's envelope and sounds like one. MEASURED
# against a dictation cue that people like (Wispr Flow's): 180 ms long, a peak at fifteen
# percent of full scale, energy around 450 Hz, and a level that falls to two percent within
# seventy milliseconds. All three of this module's first numbers were wrong in the same
# direction — an octave too high, four times too loud, and with no decay at all. It is now
# quieter, lower and shorter than the reference as well: a longer attack takes the edge off
# the onset, which is what makes a tone read as bright however low its pitch is.
#
# The length to match is the AUDIBLE one, not the file's. Wispr's start is a 180 ms file
# holding 47 ms of sound and 130 ms of tail; comparing file lengths made this module's cue
# look the same length as the reference while being more than twice as long to the ear —
# which is what a listener noticed before the measurement did.
_ATTACK_MS = 5
_DECAY_MS = 11
# The last few milliseconds ramp to nothing. An exponential decay is still at a tenth of its
# volume when a fixed-length tone ends, and a step from a tenth to zero is heard as a click —
# the one artefact this envelope exists to avoid, reintroduced at the other end of the note.
_RELEASE_MS = 5
# Under ten percent of full scale. A cue is heard beside whatever else is playing, not over
# it — and this one is heard while somebody is about to speak, so it has to get out of the
# way faster than a notification would.
_AMPLITUDE = 3_000
_CHIME_RATE = 44_100

_LISTENING = "\N{BLACK CIRCLE} listening"
# The idle head (no speech heard yet this utterance): an invitation, not a state
# announcement — "listening" was accurate but read as a passive system label, easy to
# mistake for frozen since nothing else on the line moved either (see the caret/example
# additions to `render` below). Left as a SEPARATE constant from `_LISTENING` rather than
# reusing/renaming it, because `_LISTENING` also prefixes an in-progress draft/settled
# sentence ("listening ... privet") where "start speaking" would read backwards — only the
# genuinely-idle head changes.
_INVITATION = "\N{BLACK CIRCLE} start speaking"
_SETTLED = "\N{CHECK MARK} "
_DRAFT = "\N{HORIZONTAL ELLIPSIS} "
_CURSOR_ON = "_"
_CURSOR_OFF = " "
# Shown beside the idle invitation before any dictionary term exists. Short, spoken
# sentences rather than commands — the one thing every language stt supports has in common
# is that dictation is spoken, not typed, so an example has to read as something SAID.
_BUILTIN_EXAMPLES = (
    'try saying "remind me to call them back tomorrow"',
    'try saying "send me the notes from yesterday\'s meeting"',
)


class Line:
    """One line of terminal, rewritten in place. Silent when it is not a terminal.

    The redraw is a bare `\\r` before each repaint — never a `\\n` — which reads as a stacked,
    NEW-line-per-update artefact through anything that is not a real terminal (a `\\r`-to-`\\n`
    text-mode reader, `subprocess.Popen(text=True)`, most log/capture tooling). That is a
    property of the reader, not a bug here — see ticket #89 for the full live reproduction.

    A bare `\\r` also only works while the line fits on ONE physical row: it returns to the
    start of the row the cursor is on, so a frame the terminal wrapped leaves its first rows
    behind, and every repaint adds another copy of them (#100 — a 50-column phone terminal).
    So every frame, padding included, is fitted to the width of the terminal it is written
    to — standard error, measured again at each repaint so a resize between two frames counts.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self._width = 0
        self._text: str | None = None

    def show(self, text: str) -> None:
        if not self.enabled:
            return
        room = _room()
        painted = _within(text, room)
        width = _columns(painted)
        # The erase is capped as well: after the terminal narrows, padding out to the old
        # frame's width would wrap on its own.
        sys.stderr.write("\r" + painted + " " * max(0, min(self._width, room) - width))
        sys.stderr.flush()
        self._width = width
        self._text = text

    def clear(self) -> None:
        if self.enabled and self._width:
            sys.stderr.write("\r" + " " * min(self._width, _room()) + "\r")
            sys.stderr.flush()
            self._width = 0
        self._text = None

    def note(self, message: str, *, quiet: bool) -> None:
        """`note`, while this line may be on screen: the frame comes off, the note gets a row
        of its own, and the frame is painted again below it — printed over a frame, the note
        would be glued to its end and the frame's next repaint left a row behind."""
        painted = self._text
        self.clear()
        note(message, quiet=quiet)
        if painted is not None:
            self.show(painted)


def _room() -> int:
    """How many columns a repainted frame may use: one short of the terminal's width.

    Measured on standard error, the stream the line is written to. `shutil.get_terminal_size`
    is the wrong question: it reads `COLUMNS` first (set once, stale after a resize) and then
    measures standard OUTPUT, which is a file for `stt mic > notes.txt` — so it answered 80
    for a 50-column terminal. The last column is left empty because writing into it puts some
    terminals into a pending wrap that the next character, or the `\\r`, resolves differently.
    """
    try:
        columns = os.get_terminal_size(sys.stderr.fileno()).columns
    except (AttributeError, OSError, ValueError):
        columns = 0
    if columns <= 0:
        # A pty that never had a size set reports zero; the environment's guess beats none.
        columns = shutil.get_terminal_size((80, 24)).columns
    return max(1, columns - 1)


# Zero-width on screen: CSI (colour, cursor), OSC (title, hyperlink) and two-byte escapes.
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_RESET = "\x1b[0m"


def _cells(text: str) -> Iterator[tuple[str, int]]:
    """`text` as (piece, columns) pairs: an escape sequence costs nothing, a combining or
    format character (an accent, a zero-width joiner, a variation selector) costs nothing, an
    East Asian wide or fullwidth character costs two, and any other control character — a
    newline in dictated text, a stray escape — is shown as a space, since it would otherwise
    move the cursor off the row the next `\\r` returns to."""
    at = 0
    while at < len(text):
        escape = _ESCAPE.match(text, at)
        if escape:
            yield escape.group(), 0
            at = escape.end()
            continue
        mark = text[at]
        at += 1
        kind = unicodedata.category(mark)
        if kind == "Cc":
            yield " ", 1
        elif kind in ("Mn", "Me", "Cf"):
            yield mark, 0
        else:
            yield mark, 2 if unicodedata.east_asian_width(mark) in "WF" else 1


def _columns(text: str) -> int:
    """How many terminal cells `text` occupies, which is not how many characters it has.

    Everywhere else in `live/` a length is counted in the unit that matters — UTF-16 code
    units for the Quartz call, grapheme clusters for the backspaces — and the status line was
    the one place that assumed a character is a column. It is not for the languages this
    feature exists to serve: a line of dictated Japanese is twice as wide as its length, so it
    wrapped instead of fitting, and the erase that follows it was short by the difference and
    left the tail of the previous line on screen.
    """
    return sum(cost for _, cost in _cells(text))


def _within(text: str, room: int) -> str:
    """`text`, made printable on one row and trimmed to fit `room` columns, with an ellipsis
    when anything was dropped. Escape sequences are kept, and a trimmed line that carried any
    ends with a reset, so a colour cut off before its own reset does not leak past the line."""
    pieces = list(_cells(text))
    if sum(cost for _, cost in pieces) <= room:
        return "".join(piece for piece, _ in pieces)
    kept: list[str] = []
    used = 0
    styled = False
    for piece, cost in pieces:
        if used + cost > room - 1:
            break
        kept.append(piece)
        used += cost
        styled = styled or piece.startswith("\x1b")
    return "".join(kept) + "\N{HORIZONTAL ELLIPSIS}" + (_RESET if styled else "")


def note(message: str, *, quiet: bool = False) -> None:
    """One line to stderr, prefixed. What a session says that is not the status line itself.

    It lives here rather than in `commands/mic.py` because `live/` was importing it from
    there while `commands/mic.py` imports `live/` — a dependency pointing both ways, held
    apart only by deferring both imports into function bodies. The library must not reach
    up into the command that drives it.

    `quiet` is not decoration: `-q` says "no status line, sounds or notifications", and this
    was the one channel that ignored it, so somebody who asked for a clean terminal still got
    "loading large-v3-turbo and base" and "listening — Ctrl-C … to finish" — exactly the
    lines the flag exists to suppress.
    """
    if quiet:
        return
    print(f"stt: {message}", file=sys.stderr)


def render(
    *, listening: bool, text: str, settled: bool, cursor: bool = True, hint: str | None = None
) -> str:
    """The status line: whether the microphone is open, and the sentence in progress.

    ``cursor``/``hint`` matter only for the genuinely-idle frame (``listening`` and no
    ``text`` yet): an alternating on/off caret and one example phrase, so a line that has
    not heard anything yet reads as waiting rather than frozen — see
    `dictation._blink_while_idle`, the only caller that ever passes them. The instant real
    speech arrives (``text`` non-empty) the line reverts to exactly its pre-existing
    rendering, unaffected by either argument.
    """
    if not listening:
        # `paused` still carries whatever sentence was in progress — a caller CAN report a
        # paused Progress with residual text (the field shape allows it even though nothing
        # in this codebase currently constructs one), and silently dropping that text here
        # would be a landmine for the next caller that does, not a simplification.
        if not text:
            return "\N{WHITE CIRCLE} paused"
        return f"\N{WHITE CIRCLE} paused  {_SETTLED if settled else _DRAFT}{text}"
    if not text:
        glyph = _CURSOR_ON if cursor else _CURSOR_OFF
        suffix = f"  {hint}" if hint else ""
        return f"{_INVITATION} {glyph}{suffix}"
    return f"{_LISTENING}  {_SETTLED if settled else _DRAFT}{text}"


def example_hint(*, dictionary_enabled: bool) -> str:
    """One example phrase for the idle status line, meant to be computed ONCE per burst by
    the caller and reused across every blink frame — re-reading the dictionary on every
    redraw would mean disk I/O twice a second for no benefit (`Line.show` itself stays pure
    stdlib; this is the one place status.py touches something heavier, and only ever once
    per burst). A term from the user's own terminology dictionary when they have one — a
    concrete, PERSONAL example is the whole point of a dictionary the user wrote down
    themselves — a built-in one otherwise.

    `dictionary_enabled` mirrors every other call site's `settings.dictionary` gate (see
    `live/dictation.py`): a user who turned the dictionary off gets no disk read here
    either, not just no crash. `dictionary.load()` also deliberately RAISES (`SttError`) on
    a malformed or oversized dictionary.json; this is the one caller with no `settings`
    object in reach and nothing at stake worth a crash over, so even when the dictionary IS
    enabled, a stray broken dictionary.json must fall back to a built-in example instead of
    taking the session down mid-burst.
    """
    terms: list[str] = []
    if dictionary_enabled:
        from .. import dictionary
        from .._errors import SttError

        try:
            terms = [t.term for t in dictionary.load().terms if t.term]
        except SttError:
            terms = []
    if terms:
        return f'try saying "{random.choice(terms)}"'
    return random.choice(_BUILTIN_EXAMPLES)


async def opened(*, quiet: bool = False) -> None:
    """The rising chime. The notification that goes with it is `stop_notification`'s, posted
    by the burst itself, because that one says how to stop and this one used not to."""
    await _play(_chime(_OPEN_TONES, "open"), quiet=quiet)


async def closed(*, said: str = "", quiet: bool = False) -> None:
    words = len(said.split())
    await asyncio.gather(
        _play(_chime(_CLOSE_TONES, "close"), quiet=quiet),
        _banner(f"stopped — {words} word(s) dictated" if words else "stopped", quiet=quiet),
    )


async def _banner(message: str, *, quiet: bool) -> None:
    """A notification, through the one tool that can show one without an application.

    Fire and forget, and silent about its own failure: the user may have notifications off
    for whatever `osascript` is attributed to, and that is their setting, not an error in the
    middle of dictation. The command is `notify.command`'s, which quotes the message — it
    reaches AppleScript as source code, and a stray quote in a device name would otherwise be
    a syntax error at best.
    """
    if quiet:
        return
    from ..notify import command

    found = command("stt", message, platform="darwin")
    if found is None:
        return
    try:
        process = await asyncio.create_subprocess_exec(
            *found.argv,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
    except OSError:
        return
    await _briefly(process)


# terminal-notifier's own `-group` id: a fresh notification with the same id REPLACES the
# previous one on screen instead of stacking, and `-remove` withdraws by this same id — so a
# stale "Stop" button left over from a burst that already ended cannot linger for the next
# one to inherit.
_STOP_GROUP = "stt-mic-stop"

# What `stt doctor` and the daemon's startup line tell somebody with no Stop button to run.
# It builds `stt.app` (see `helper.py`); `terminal-notifier` is only the fallback for a Mac
# that cannot build it.
INSTALL_HINT = "stt mic helper"

# One id per notification, so the helper withdraws its own and never another burst's.
_BURSTS = itertools.count(1)


def _helper() -> Path | None:
    """The helper, when it can draw a button: current, and not turned off for notifications
    — a helper macOS refuses answers DENIED, so naming its Stop button would be a promise the
    notification then breaks."""
    from . import helper

    found = helper.current()
    return None if found is None or _notifications_denied() else found


def _notifications_denied() -> bool:
    """Whether the installed helper has been refused notifications (System Settings >
    Notifications > stt) — asked without prompting, in milliseconds."""
    from . import helper

    return (helper.notification_permission() or "").startswith("denied")


# What a burst says when macOS refuses stt's notifications: the one switch that brings the
# button back.
DENIED_NOTE = (
    "notifications are turned off for stt, so there is no Stop button — turn them on in "
    "System Settings > Notifications > stt"
)


def terminal_notifier() -> str | None:
    """terminal-notifier's path, if it is 3.x or later: 2.x draws no buttons at all, and
    `-action` exists only from 3.0.0, so an older one is no Stop button."""
    found = shutil.which("terminal-notifier")
    return found if found is not None and _major_version(found) >= 3 else None


@functools.lru_cache(maxsize=4)
def _major_version(program: str) -> int:
    """`terminal-notifier -version` prints "terminal-notifier 3.1.0." — asked once per
    process, synchronously: it answers in milliseconds, and only the first burst asks."""
    try:
        said = subprocess.run(
            [program, "-version"], capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return 0
    found = re.search(r"(\d+)\.\d+", said.stdout + said.stderr)
    return int(found.group(1)) if found else 0


@dataclass(frozen=True)
class StopButton:
    """What draws a burst's Stop button: `program` is stt's helper (``via_helper``) or
    terminal-notifier 3.x."""

    program: str
    via_helper: bool


def stop_button() -> StopButton | None:
    """What will draw a Stop button on a burst's notification on this machine: stt's own
    helper application (`helper.py`), or else `terminal-notifier` 3.x — `None` for neither.

    Asked ONCE per burst, off the event loop (`asyncio.to_thread`), and the answer handed to
    both the status line and `stop_notification`: asking the helper whether it may notify
    is a process, and two answers could disagree, a status line promising a button the
    notification then lacks.

    `osascript display notification` cannot carry a button, and `display dialog` — which
    can — takes the keyboard focus the dictated words are being typed into, so a dialog
    popping up mid-sentence would send them into the dialog. A notification needs an
    application bundle with its own identifier to have a button; that is what the helper
    is. Without either, the notification is still posted and still says how to stop.
    """
    found = _helper()
    if found is not None:
        return StopButton(str(found), via_helper=True)
    notifier = terminal_notifier()
    return StopButton(notifier, via_helper=False) if notifier is not None else None


def stop_hint(guaranteed: str, *, button: bool) -> str:
    """How to stop, in the words every notification and status line of a burst uses (spec
    section 6.2): ``guaranteed`` is what always works — "Ctrl-C in the terminal", "press
    X again", "release X" — and the Stop button is named only where there is one."""
    return f"{guaranteed}, or click Stop, to finish" if button else f"{guaranteed} to finish"


async def stop_notification(
    listening: str, guaranteed: str, button: StopButton | None, *, quiet: bool
) -> bool:
    """Post the burst's notification and wait on its Stop button; ``True`` only when Stop
    was the button clicked.

    The helper prints one word and exits: ``STOP``, ``DISMISSED``, ``CLOSED`` (the body was
    clicked, or it could not be shown) or ``DENIED`` (notifications are off for stt).
    terminal-notifier 3.x prints the clicked title, ``Stop``, given ``-action Stop`` — the
    singular option; ``-actions`` is ignored there and draws no button. With neither
    (``button`` is `None`), the same text goes out through ``osascript`` with no button, so
    the notification still says how to stop — worded without "click Stop", since that one
    has no button to click. ``listening`` is the head ("listening on <device>"),
    ``guaranteed`` what always stops the burst ("press X again", "release X", "Ctrl-C in
    the terminal"), ``button`` this burst's `stop_button()`.

    ``False`` for everything but Stop, and never raises: the hotkey (or Ctrl-C) always
    works, so a notification that could not be used is not a reason to end the burst. The
    caller races this against the microphone (`dictation._race_burst_signals`) and cancels
    it when the burst ends another way; the cancellation terminates the helper, which then
    takes its Stop button off the screen.
    """
    if quiet:
        return False
    plain = f"{listening} — {stop_hint(guaranteed, button=False)}"
    if button is None:
        await _banner(plain, quiet=quiet)
        return False
    message = f"{listening} — {stop_hint(guaranteed, button=True)}"
    if button.via_helper:
        burst = f"{os.getpid()}-{next(_BURSTS)}"
        argv = [button.program, "notify", "--burst", burst, "--title", "stt", "--body", message]
        stop_word = "STOP"
    else:
        argv = [button.program, "-title", "stt", "-message", message,
                "-action", "Stop", "-group", _STOP_GROUP]  # fmt: skip
        stop_word = "Stop"
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        await _banner(plain, quiet=quiet)
        return False
    try:
        out, _err = await process.communicate()
    except asyncio.CancelledError:
        await _withdraw(process)
        raise
    said = out.decode(errors="replace").strip()
    if said == stop_word:
        note("Stop clicked on the notification — finishing this burst", quiet=quiet)
        return True
    if said == "DENIED":
        note(DENIED_NOTE, quiet=quiet)
    if said == "DENIED" or (said == "" and button.via_helper):
        # No button was shown — refused, or the helper died without a word — so the same
        # words go out without one: the notification still says how to stop. Not for
        # terminal-notifier, whose "" is a notification shown and dismissed without Stop.
        await _banner(plain, quiet=quiet)
    return False


async def _withdraw(process: asyncio.subprocess.Process) -> None:
    """End a notification process whose burst is over. SIGTERM first: the helper answers it
    by taking its notification off the screen before exiting, and a kill would leave the
    Stop button behind. Killed if it has not gone within two seconds."""
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def close_stop_notification(*, quiet: bool) -> None:
    """Withdraw terminal-notifier's banner once the burst is over, however it ended — a
    terminated terminal-notifier leaves its notification behind, so it is removed by group.
    Asked whenever terminal-notifier is there, not only when this burst used it: the helper
    may have been installed mid-burst, and removing a group nobody posted costs nothing.
    The helper withdraws its own (`_withdraw`)."""
    notifier = terminal_notifier()
    if quiet or notifier is None:
        return
    try:
        process = await asyncio.create_subprocess_exec(
            notifier, "-remove", _STOP_GROUP,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
    except OSError:
        return
    await _briefly(process)


def _chime(tones: tuple[float, float], name: str) -> Path | None:
    """The two-tone cue as a file, written once and kept.

    Returns None if it cannot be written — a sound is a convenience, and a dictation session
    that refuses to start because a cache directory is read-only would be a poor trade.
    """
    from .. import config

    where = config.app_home() / "sounds"
    file = where / f"{name}-v{_CHIME_VERSION}.wav"
    if file.is_file():
        return file
    try:
        where.mkdir(parents=True, exist_ok=True)
        with wave.open(str(file), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(_CHIME_RATE)
            handle.writeframes(_two_tones(tones))
    except OSError:
        return None
    return file


def _two_tones(tones: tuple[float, float]) -> bytes:
    """One tone into the next, each faded in and out so neither end clicks."""
    samples = array("h")
    for pitch in tones:
        count = _CHIME_RATE * _TONE_MS // 1000
        for at in range(count):
            value = math.sin(2 * math.pi * pitch * at / _CHIME_RATE)
            samples.append(int(_AMPLITUDE * value * _envelope(at, count)))
    return samples.tobytes()


def _envelope(at: int, count: int) -> float:
    """Struck, then decaying — the shape of something plucked rather than switched on.

    The attack is a few milliseconds because an instant start is heard as a click, and the
    decay is exponential because that is what a physical object does and what the ear reads
    as a note rather than a signal.
    """
    attack = max(1, _CHIME_RATE * _ATTACK_MS // 1000)
    rising = at / attack if at < attack else 1.0
    falling = math.exp(-max(0, at - attack) / (_CHIME_RATE * _DECAY_MS / 1000))
    release = max(1, _CHIME_RATE * _RELEASE_MS // 1000)
    left = count - at
    return min(rising, 1.0) * falling * min(1.0, left / release)


async def _play(sound: Path | None, *, quiet: bool) -> None:
    """Fire and forget. A missing `afplay` or a missing sound file is not worth a word."""
    if quiet or sound is None or not sound.is_file():
        return
    try:
        process = await asyncio.create_subprocess_exec(
            "afplay", str(sound),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
    except OSError:
        return
    await _briefly(process)


async def _briefly(process: asyncio.subprocess.Process) -> None:
    """Wait for a two-hundred-millisecond noise, and not a moment longer than that.

    Long enough for a two-hundred-millisecond noise, which measured at 1.4 seconds because
    `afplay` spends most of that starting up, and short enough that one which wedges is not
    dictation's problem. Whoever calls this must not be in front of anything the user is
    waiting on — see `_through_the_microphone`, where it runs beside the session rather than
    before it. The timeout is not an error, and a process that outlives it is killed rather
    than left to reap itself whenever it feels like it.
    """
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
