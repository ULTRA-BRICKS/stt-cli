"""configtui — the interactive screen `stt config` opens with no subcommand, on a terminal.

WHY THIS EXISTS
    `stt config list`/`get`/`set` are precise and scriptable, and precise and scriptable is
    a bad way to discover that `hotkey_hold` exists, what it does, or what values it takes.
    Typing a setting name correctly is the wrong first step for somebody who does not yet
    know the name — they know roughly what they want ("make Ctrl-C during dictation act
    differently", "I want Russian by default") and have to guess the rest. This screen is
    the browsable version of the settings `stt config list` already prints: grouped
    the way the code itself groups them, each with a plain-language description, worked
    examples where a bare value would not explain itself, and the exact `stt config set`
    line an edit is equivalent to — so leaving the screen and using the CLI from then on is
    always available, never required.

WHAT IS PURE AND WHAT IS NOT
    `GROUPS`/`HELP`/`EXAMPLES`/`CHOICES` and `TuiState` touch no terminal and are exercised
    directly in `tests/test_configtui.py` — including a completeness check that fails the
    moment a new `Settings` field is added without a `GROUPS` entry, which is the failure
    mode this file exists to prevent (a setting nobody can find because it is not on the
    screen). `_draw`/`_loop`/`launch` are curses, deliberately thin, and not exercised by
    the test suite the way nothing else that opens a real terminal is either.

CURSES, NOT A NEW DEPENDENCY
    curses is the Python standard library's own terminal UI toolkit and ships with every
    CPython on macOS; reaching for a third-party TUI framework here would be the one thing
    this repo's "zero required runtime dependencies" rule exists to prevent.
"""

from __future__ import annotations

import contextlib
import curses
from dataclasses import dataclass, field
from typing import Any

from .. import config
from ._keycodes import (
    BACKSPACE_KEYS,
    ENTER_KEYS,
    ESCAPE_KEYS,
    KEY_TAB,
    PRINTABLE_HIGH,
    PRINTABLE_LOW,
)

# ---------------------------------------------------------------------------------------
# Metadata: grouping, descriptions, examples and fixed choices for every configurable
# setting. `tests/test_configtui.py::test_every_configurable_setting_has_a_group_and_help`
# is what keeps this in sync with `config.Settings` as it grows.
# ---------------------------------------------------------------------------------------

GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("engine", ("backend", "model", "language", "threads", "whispercpp_root")),
    ("context", ("context", "context_compare")),
    ("push-to-talk", ("hotkey_press", "hotkey_hold", "mic_threshold", "mic_daemon_autostart")),
    (
        "voice detection",
        ("vad", "vad_threshold", "vad_min_silence_ms", "vad_speech_pad_ms", "vad_min_speech_ms"),
    ),
    ("cleaning", ("clean", "strict_clean", "max_repeats", "confidence_floor")),
    ("terminology", ("dictionary", "dict_bias", "dict_similarity")),
    ("variants", ("variants", "variant_models", "show_variants")),
    ("correction & summary", ("fix", "fix_with", "summary", "summary_style")),
    ("diarization", ("diarize", "speakers")),
    ("output", ("show_flags", "text_variant", "formats", "timestamps", "timezone")),
    ("archive", ("cache", "keep_media")),
)

HELP: dict[str, str] = {
    "backend": "which speech engine decodes: whispercpp, mlx, or auto to pick what's installed",
    "model": "the ASR model name",
    "language": "spoken language code, or unset to auto-detect it",
    "threads": "CPU threads per model; 0 lets the engine choose",
    "whispercpp_root": "path to a whisper.cpp checkout, if it is not on PATH",
    "context": "how much of the model's own previous output feeds back as context",
    "context_compare": "cross-check against a context-off decode to catch a self-reinforcing loop",
    "hotkey_press": "press-style chord for the `stt mic` daemon (press again to stop)",
    "hotkey_hold": "hold-style chord for push-to-talk; unset disables it (no default)",
    "mic_threshold": "how loud a frame must be to count as speech; 0 uses the built-in bar",
    "mic_daemon_autostart": "record only: use `stt mic enable`/`disable` to change the daemon",
    "vad": "which voice activity detector finds speech in the audio",
    "vad_threshold": "silero's speech-probability cutoff, 0.0 to 1.0",
    "vad_min_silence_ms": "milliseconds of quiet before a speech span is considered over",
    "vad_speech_pad_ms": "milliseconds of padding kept around a detected speech span",
    "vad_min_speech_ms": "the shortest span the detector will still call speech",
    "clean": "scrub hallucinated and repeated phrases from the transcript",
    "strict_clean": "drop a flagged phrase instead of only flagging it",
    "max_repeats": "how many times a phrase may repeat before it counts as a loop",
    "confidence_floor": "segments scored below this are treated as suspect",
    "dictionary": "apply your terminology dictionary to fix known spellings",
    "dict_bias": "also feed the dictionary to the speech model as a prompt, not just fix after",
    "dict_similarity": "how close a word must be to a term before it is flagged as a misspelling",
    "variants": "extra decodings to try per low-confidence segment",
    "variant_models": "extra models used for variant decodings",
    "show_variants": "print every variant considered, not just the winner",
    "fix": "run an LLM correction pass over the transcript",
    "fix_with": "which LLM CLI powers --fix/--summary",
    "summary": "generate a structured summary alongside the transcript",
    "summary_style": "the summary's shape",
    "diarize": "label who is speaking (needs `stt diarize install` first)",
    "speakers": "the exact number of speakers, if known; unset lets diarization guess",
    "show_flags": "print which settings a run actually used",
    "text_variant": "which text a renderer sees: fixed, raw, or both",
    "formats": "which output formats a run writes",
    "timestamps": "timestamp style in the rendered output",
    "timezone": "timezone for absolute timestamps; unset uses the system one",
    "cache": "reuse a cached transcript for identical input+settings instead of re-decoding",
    "keep_media": "keep the original audio/video in the archive after transcribing",
}

EXAMPLES: dict[str, tuple[str, ...]] = {
    "model": ("large-v3-turbo", "small", "base"),
    "language": ("ru", "en", "auto"),
    "hotkey_press": ("cmd+shift+space", "ctrl+opt+d", "cmd+shift+semicolon", "none"),
    "hotkey_hold": ("ctrl+opt+d", "cmd+shift+semicolon", "none"),
    "timezone": ("Europe/Belgrade", "UTC", "America/New_York"),
    "variant_models": ("small,medium",),
    "formats": ("txt,md", "txt,srt,vtt"),
    "dict_similarity": ("0.82",),
    "speakers": ("2",),
}


def _choices() -> dict[str, tuple[str, ...]]:
    """Fixed value sets, one source of truth each — imported lazily (and only from modules
    that are themselves cheap) so this screen's own import stays light."""
    from .. import backends, llm
    from ..pipeline import COMPARE_MODES

    return {
        "backend": ("auto", *backends.ORDER),
        "context": ("off", "short", "full"),
        "context_compare": COMPARE_MODES,
        "vad": ("auto", "silero", "ffmpeg", "none"),
        "fix_with": ("auto", *llm.ORDER),
        "text_variant": ("fixed", "raw", "both"),
        "timestamps": config.TIMESTAMP_MODES,
    }


def _all_settings() -> tuple[str, ...]:
    return tuple(name for _group, names in GROUPS for name in names)


# ---------------------------------------------------------------------------------------
# Pure state: navigation and the edit/reset glue. No terminal touches anything below here.
# ---------------------------------------------------------------------------------------


@dataclass
class TuiState:
    """Where the cursor is, and the one line of feedback the last action left behind."""

    group: int = 0
    row: int = 0
    message: str = ""
    # Whether `message` is confirming success (green) or reporting a refusal (red) — a
    # rejected edit ("shift+a has no cmd or ctrl...") was drawn in the SAME green as a
    # successful one before this existed, because only the text was ever tracked.
    message_ok: bool = True
    search: str | None = None
    matches: tuple[str, ...] = field(default_factory=tuple)

    def visible(self) -> tuple[str, ...]:
        """The settings the current screen shows: a global search's matches, or the
        selected group's own list."""
        if self.search is not None:
            return self.matches
        return GROUPS[self.group][1]

    def selected_key(self) -> str | None:
        names = self.visible()
        if not names:
            return None
        return names[max(0, min(self.row, len(names) - 1))]

    def move(self, delta: int) -> None:
        names = self.visible()
        if not names:
            return
        self.row = max(0, min(self.row + delta, len(names) - 1))

    def next_group(self) -> None:
        if self.search is not None:
            return
        self.group = (self.group + 1) % len(GROUPS)
        self.row = 0

    def prev_group(self) -> None:
        if self.search is not None:
            return
        self.group = (self.group - 1) % len(GROUPS)
        self.row = 0

    def start_search(self) -> None:
        self.search = ""
        self.matches = _all_settings()
        self.row = 0

    def type_search(self, text: str) -> None:
        self.search = text
        needle = text.lower()
        self.matches = tuple(
            name
            for name in _all_settings()
            if needle in name.lower() or needle in HELP.get(name, "").lower()
        )
        self.row = 0

    def end_search(self) -> None:
        """Leave search mode, landing on whichever group actually holds the selected key —
        so the cursor does not silently jump back to group 0 after a search picks a setting
        from group 5."""
        key = self.selected_key()
        self.search = None
        self.matches = ()
        if key is not None:
            for index, (_name, names) in enumerate(GROUPS):
                if key in names:
                    self.group = index
                    self.row = names.index(key)
                    return
        self.row = 0


def apply_edit(key: str, raw: str) -> tuple[bool, str]:
    """Coerce and store one typed value, the same way `stt config set` does — the TUI's
    edit prompt and the CLI command share this exact path (including the extra hotkey/mode
    check `config_cmd.validate_extra` does — a bad chord is refused here too, with a clean
    message, instead of only failing later when a daemon tries to start), so an edit here
    can never accept something `stt config set` would refuse, or vice versa."""
    from .._errors import SttError
    from .config_cmd import validate_extra

    try:
        value = config.coerce(key, raw)
        validate_extra(key, value)
        config.save_setting(key, value)
    except SttError as failed:
        return False, failed.what
    return True, f"{key} = {_shown(value)}"


def apply_toggle(key: str, current: bool) -> tuple[bool, str]:
    """Flip a boolean setting and store it — errors caught the same way `apply_edit` does,
    so a locked/unwritable config.json is reported on the footer instead of crashing the
    whole screen (nothing here catches exceptions on the way back into `_loop`)."""
    from .._errors import SttError

    value = not current
    try:
        config.save_setting(key, value)
    except SttError as failed:
        return False, failed.what
    return True, f"{key} = {_shown(value)}"


def apply_reset(key: str) -> tuple[bool, str]:
    """Remove a stored value, back to its default — same error handling as `apply_toggle`."""
    from .._errors import SttError

    try:
        config.unset_setting(key)
    except SttError as failed:
        return False, failed.what
    return True, f"{key} reset to its default"


def _shown(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(v) for v in value) or "(empty)"
    return "(unset)" if value is None else str(value)


def command_line(key: str, value: object) -> str:
    """The `stt config set` invocation an edit is equivalent to — shown in the detail panel
    so the TUI is a way to LEARN the CLI, not a replacement that hides it."""
    return f"stt config set {key} {_shown(value)}"


# ---------------------------------------------------------------------------------------
# curses — thin drawing over the pure state above.
# ---------------------------------------------------------------------------------------

_HELP_BAR = (
    "\u2191/\u2193 move  \u2190/\u2192 tab  Enter edit  Space toggle  r reset  / search  q quit"
)


def launch() -> int:
    """Entry point `commands/config_cmd.py` calls for a bare, interactive `stt config`."""
    from .._errors import EXIT_OK

    try:
        curses.wrapper(_loop)
    except curses.error as broken:
        print(
            f"stt config: the screen hit a terminal error ({broken}), most often a "
            "terminal too small or lacking TUI support; falling back to `stt config list`"
        )
        from .config_cmd import list_settings

        return list_settings()
    return EXIT_OK


def _loop(stdscr: curses.window) -> None:
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)  # the active tab / selected row
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selected row, inverted
    curses.init_pair(3, curses.COLOR_GREEN, -1)  # success feedback
    curses.init_pair(4, curses.COLOR_RED, -1)  # error feedback
    curses.init_pair(5, curses.COLOR_YELLOW, -1)  # examples / hints

    state = TuiState()
    settings = config.load_settings()
    stored = config.stored()
    while True:
        _draw(stdscr, state, settings, stored)
        key = stdscr.getch()
        settings, stored, keep_going = _handle(stdscr, state, key, settings, stored)
        if not keep_going:
            return


def _handle(
    stdscr: curses.window,
    state: TuiState,
    key: int,
    settings: config.Settings,
    stored: dict[str, Any],
) -> tuple[config.Settings, dict[str, Any], bool]:
    if state.search is not None:
        return _handle_search(state, key, settings, stored)
    if key in (ord("q"), *ESCAPE_KEYS):  # q or plain Escape
        return settings, stored, False
    if key in (curses.KEY_UP, ord("k")):
        state.move(-1)
    elif key in (curses.KEY_DOWN, ord("j")):
        state.move(1)
    elif key in (curses.KEY_LEFT, ord("h"), KEY_TAB):  # Tab also cycles groups
        state.prev_group()
    elif key in (curses.KEY_RIGHT, ord("l")):
        state.next_group()
    elif key == ord("/"):
        state.start_search()
    elif key == ord("r"):
        selected = state.selected_key()
        if selected is not None:
            ok, state.message = apply_reset(selected)
            state.message_ok = ok
            if ok:
                settings, stored = _reload()
    elif key in (*ENTER_KEYS, ord(" ")):
        settings, stored = _activate(stdscr, state, settings, stored, toggle_only=key == ord(" "))
    elif key == curses.KEY_MOUSE:
        settings, stored = _handle_mouse(stdscr, state, settings, stored)
    return settings, stored, True


def _handle_search(
    state: TuiState,
    key: int,
    settings: config.Settings,
    stored: dict[str, Any],
) -> tuple[config.Settings, dict[str, Any], bool]:
    if key in (*ESCAPE_KEYS, *ENTER_KEYS):  # Escape or Enter both leave search mode
        state.end_search()
    elif key == curses.KEY_UP:
        state.move(-1)
    elif key == curses.KEY_DOWN:
        state.move(1)
    elif key in BACKSPACE_KEYS:
        state.type_search((state.search or "")[:-1])
    elif PRINTABLE_LOW <= key < PRINTABLE_HIGH:
        state.type_search((state.search or "") + chr(key))
    return settings, stored, True


def _activate(
    stdscr: curses.window,
    state: TuiState,
    settings: config.Settings,
    stored: dict[str, Any],
    *,
    toggle_only: bool,
) -> tuple[config.Settings, dict[str, Any]]:
    selected = state.selected_key()
    if selected is None:
        return settings, stored
    current = getattr(settings, selected)
    if isinstance(current, bool):
        ok, state.message = apply_toggle(selected, current)
        state.message_ok = ok
        if ok:
            return _reload()
        return settings, stored
    if toggle_only:  # Space on a non-boolean does nothing
        return settings, stored
    typed = _prompt(stdscr, selected, current)
    if typed is None:
        return settings, stored
    ok, state.message = apply_edit(selected, typed)
    state.message_ok = ok
    if ok:
        return _reload()
    return settings, stored


def _reload() -> tuple[config.Settings, dict[str, Any]]:
    return config.load_settings(), config.stored()


_TAB_X0 = 2  # left margin before the first group tab
_ROW_TOP = 3  # first row a setting can be drawn/hit-tested on, below the header + tab bar
_DETAIL_ROWS = 5  # separator + description + examples + command line + footer


def _tab_spans(width: int) -> list[tuple[int, int, int, str]]:
    """``(start_x, end_x, group_index, name)`` for every tab that fits in ``width`` columns
    — the ONE place the tab bar's layout is computed, so `_draw` (what's painted) and
    `_handle_mouse` (what a click hits) can never quietly disagree about where a tab is or
    how wide it is."""
    spans = []
    x = _TAB_X0
    for index, (name, _names) in enumerate(GROUPS):
        end = x + len(name) + 4
        if x >= width - 1:
            break
        spans.append((x, end, index, name))
        x = end
    return spans


def _handle_mouse(
    stdscr: curses.window,
    state: TuiState,
    settings: config.Settings,
    stored: dict[str, Any],
) -> tuple[config.Settings, dict[str, Any]]:
    _id, mouse_x, mouse_y, _z, _bstate = curses.getmouse()
    if mouse_y == 1 and state.search is None:  # the tab bar
        _height, width = stdscr.getmaxyx()
        for x, end, index, _name in _tab_spans(width):
            if x <= mouse_x < end:
                state.group, state.row = index, 0
                return settings, stored
        return settings, stored
    names = state.visible()
    picked = mouse_y - _ROW_TOP
    if 0 <= picked < len(names):
        if picked == state.row:
            return _activate(stdscr, state, settings, stored, toggle_only=False)
        state.row = picked
    return settings, stored


def _prompt(stdscr: curses.window, key: str, current: object) -> str | None:
    """A one-line editor pre-filled with the current value; Tab cycles fixed choices when
    the setting has any. Enter confirms, Escape cancels (returns ``None``)."""
    height, width = stdscr.getmaxyx()
    choices = _choices().get(key, ())
    buf = "" if current is None else _shown(current)
    cursor = len(buf)
    while True:
        line = f" set {key} = "
        _safe_addstr(stdscr, height - 1, 0, " " * (width - 1))
        _safe_addstr(stdscr, height - 1, 0, (line + buf)[: width - 1], curses.color_pair(1))
        if choices:
            hint = "  (Tab cycles: " + " / ".join(choices) + ")"
            remaining = width - 1 - len(line) - len(buf)
            if remaining > 0:
                _safe_addstr(
                    stdscr, height - 1, len(line) + len(buf), hint[:remaining], curses.color_pair(5)
                )
        curses.curs_set(1)
        with contextlib.suppress(curses.error):
            stdscr.move(height - 1, min(len(line) + cursor, max(0, width - 2)))
        code = stdscr.getch()
        if code in ESCAPE_KEYS:
            curses.curs_set(0)
            return None
        if code in ENTER_KEYS:
            curses.curs_set(0)
            return buf
        if code == KEY_TAB and choices:  # Tab: cycle to the next fixed choice
            here = choices.index(buf) if buf in choices else -1
            buf = choices[(here + 1) % len(choices)]
            cursor = len(buf)
        elif code in BACKSPACE_KEYS:
            buf, cursor = buf[: max(0, cursor - 1)] + buf[cursor:], max(0, cursor - 1)
        elif code == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif code == curses.KEY_RIGHT:
            cursor = min(len(buf), cursor + 1)
        elif PRINTABLE_LOW <= code < PRINTABLE_HIGH:
            buf = buf[:cursor] + chr(code) + buf[cursor:]
            cursor += 1


def _safe_addstr(stdscr: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    """``addstr``, but a write that starts or lands off the visible screen is dropped rather
    than raising — curses refuses outright the moment ``x``/``y`` is already out of bounds
    (it does not just silently clip, the way it does for a string that only trails off the
    right edge), and a narrow terminal, a long group name, or a bottom-right-corner write are
    all ordinary ways to hit that. One real bug already reached this exact wall: the tab bar
    kept advancing `x` past the screen and crashed on GROUPS with enough categories to run
    out of columns — this is the general fix, not a special case for that one row.
    """
    if y < 0 or x < 0:
        return
    with contextlib.suppress(curses.error):
        stdscr.addstr(y, x, text, attr)


def _draw(
    stdscr: curses.window,
    state: TuiState,
    settings: config.Settings,
    stored: dict[str, Any],
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    _safe_addstr(stdscr, 0, 0, " stt config ".center(width - 1, "\u2500"), curses.A_BOLD)

    if state.search is not None:
        _safe_addstr(stdscr, 1, 0, f" / {state.search}", curses.color_pair(1) | curses.A_BOLD)
    else:
        for x, _end, index, name in _tab_spans(width):
            attr = curses.color_pair(1) | curses.A_BOLD if index == state.group else curses.A_DIM
            _safe_addstr(stdscr, 1, x, f" {name} ", attr)

    names = state.visible()
    for offset, name in enumerate(names):
        y = _ROW_TOP + offset
        if y >= height - _DETAIL_ROWS:
            break
        value = getattr(settings, name)
        source = "config" if name in stored else "default"
        text = f"{name:<20} {_shown(value):<22} {source}"
        attr = curses.color_pair(2) if offset == state.row else curses.A_NORMAL
        _safe_addstr(stdscr, y, 1, text[: width - 2], attr)

    selected = state.selected_key()
    detail_top = height - _DETAIL_ROWS
    if 0 <= detail_top < height and selected is not None:
        _safe_addstr(stdscr, detail_top, 0, "\u2500" * (width - 1), curses.A_DIM)
        _safe_addstr(stdscr, detail_top + 1, 1, HELP.get(selected, "")[: width - 2])
        examples = EXAMPLES.get(selected)
        if examples and detail_top + 2 < height:
            _safe_addstr(
                stdscr,
                detail_top + 2,
                1,
                ("examples: " + ", ".join(examples))[: width - 2],
                curses.color_pair(5),
            )
        if detail_top + 3 < height:
            line = command_line(selected, getattr(settings, selected))
            _safe_addstr(stdscr, detail_top + 3, 1, line[: width - 2], curses.A_DIM)

    footer = state.message or _HELP_BAR
    if not state.message:
        attr = curses.A_DIM
    else:
        attr = curses.color_pair(3) if state.message_ok else curses.color_pair(4)
    _safe_addstr(stdscr, height - 1, 0, footer[: width - 1], attr)
    stdscr.refresh()
