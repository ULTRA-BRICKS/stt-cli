"""hotkey — turn a spec like ``"cmd+shift+space"`` into the (keycode, modifiers) `tap.py`
compares real key-down/key-up events against.

WHY A STRING SPEC AT ALL
    The hotkey has to round-trip through `config.json`, the command line and `stt mic
    --help`, all three of which want a human-typed word, not a keycode nobody can name from
    memory. This module is the one place that word is turned into the two integers Quartz
    actually understands, and the one place that pretends to be a keyboard layout: only the
    US ANSI physical positions are named, because that is what `kVK_*` numbers to begin
    with — a Cyrillic or Kana layout still calls the key left of Return "quote" here.

WHY A HOTKEY MUST CARRY A MODIFIER
    `tap.py`'s whole design rests on telling stt's own synthetic keystrokes apart from the
    user's by a marker, and on the user's own keystrokes never being mistaken for a command.
    A hotkey with no modifier would fire on every ordinary letter the user types while a
    daemon is idle, which is not a hotkey, it is a keylogger that occasionally opens a
    microphone. `parse` refuses one.
"""

from __future__ import annotations

from dataclasses import dataclass

VALID_MODES = ("toggle", "hold")
DEFAULT_MODE = "toggle"
# Used when `hotkey` is unset in both config and `--hotkey`. Cmd+Shift+Space is not bound by
# macOS itself and collides with nothing in Terminal, VS Code, Safari or Chrome as shipped —
# checked by hand against each application's own Keyboard Shortcuts panel. It is still just a
# starting point: `stt config set hotkey ...` (or the TUI) is how you make it yours.
DEFAULT_HOTKEY = "cmd+shift+space"


class HotkeyError(ValueError):
    """A hotkey spec that cannot be turned into a real key + modifier combination."""


# CGEventFlags bits that matter for a combo (`CGEventTypes.h`). Deliberately excludes
# `kCGEventFlagMaskAlphaShift` (caps lock) and the housekeeping bits
# (`NonCoalesced`/`NumericPad`/`Help`) — holding the hotkey with caps lock on, or from a
# keypad-duplicate key code, must still match; those bits say nothing about which chord was
# pressed.
_SHIFT = 1 << 17
_CONTROL = 1 << 18
_ALTERNATE = 1 << 19
_COMMAND = 1 << 20
_FN = 1 << 23
RELEVANT_FLAGS = _SHIFT | _CONTROL | _ALTERNATE | _COMMAND | _FN
# Shift alone does not make a chord safe: every capital letter and every shifted symbol
# ("!", "@", "$", ...) a person types ordinarily is shift+key, so a hotkey that only
# required shift would fire constantly during normal typing — exactly the "keylogger that
# occasionally opens a microphone" failure the module docstring warns about, just gated on
# capitalization instead of on nothing at all.
#
# Option and Fn are not safe either, even though neither is used for capitalization: Option
# is how macOS types a dead-key accent (opt+e/u/n start é/ü/ñ) and punctuation (opt+space is
# a non-breaking space), and Fn is the laptop-keyboard route to Home/End/PageUp/PageDown
# (fn+left/right/up/down) — both are things people press constantly while doing nothing
# hotkey-like at all. Only Command and Control are never, on their own, how macOS itself
# produces a character or a navigation event; `parse` requires at least one of them.
_STRONG_MODIFIERS = _CONTROL | _COMMAND

_MODIFIERS: dict[str, int] = {
    "cmd": _COMMAND,
    "command": _COMMAND,
    "shift": _SHIFT,
    "ctrl": _CONTROL,
    "control": _CONTROL,
    "opt": _ALTERNATE,
    "option": _ALTERNATE,
    "alt": _ALTERNATE,
    "fn": _FN,
}

# macOS virtual keycodes (Carbon `Events.h` `kVK_*`), US ANSI layout — the physical key, not
# the character it types under a different layout. Only the keys a push-to-talk hotkey
# plausibly names: letters, digits, the punctuation row, space/tab/return/delete, arrows and
# the twelve function keys. Escape (53) is deliberately absent: `tap.py` already owns it for
# "stop correcting" / "end the session", and a hotkey that shared it would fight that.
_KEYCODES: dict[str, int] = {
    # Letters, alphabetical (not by keycode, which QWERTY scatters unpredictably) so a
    # reader looking for "where's j" does not have to scan the whole table.
    "a": 0,
    "b": 11,
    "c": 8,
    "d": 2,
    "e": 14,
    "f": 3,
    "g": 5,
    "h": 4,
    "i": 34,
    "j": 38,
    "k": 40,
    "l": 37,
    "m": 46,
    "n": 45,
    "o": 31,
    "p": 35,
    "q": 12,
    "r": 15,
    "s": 1,
    "t": 17,
    "u": 32,
    "v": 9,
    "w": 13,
    "x": 7,
    "y": 16,
    "z": 6,
    # Digits, numeric order.
    "0": 29,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
    # Punctuation row, left to right on a US ANSI keyboard.
    "minus": 27,
    "equal": 24,
    "leftbracket": 33,
    "rightbracket": 30,
    "backslash": 42,
    "semicolon": 41,
    "quote": 39,
    "comma": 43,
    "period": 47,
    "slash": 44,
    "grave": 50,
    # Whitespace and editing keys.
    "tab": 48,
    "space": 49,
    "return": 36,
    "enter": 36,
    "delete": 51,
    "backspace": 51,
    # Arrows.
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    # Function keys, F1-F12 — not sequential in keycode (F13+ would need the media-key
    # path this module does not implement), so listing them in order here matters most.
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}


@dataclass(frozen=True)
class Combo:
    """One parsed hotkey: a physical key plus the exact modifiers held with it."""

    keycode: int
    modifiers: int
    spec: str  # the normalized form of what the user typed, for status lines and --help

    def matches(self, keycode: int, flags: int) -> bool:
        """Does a real key-DOWN (``keycode``, raw ``CGEventFlags``) fire this combo?

        Only the modifier bits this combo cares about are compared — caps lock and the
        keypad/coalescing bits in ``flags`` are masked out first, so those never cause a
        held hotkey to silently stop matching. Deliberately not used for a key-UP: see
        `released`, and why the two must not share a check.
        """
        return keycode == self.keycode and (flags & RELEVANT_FLAGS) == self.modifiers

    def released(self, keycode: int) -> bool:
        """Does a key-UP end this combo's hold? Keycode only — the modifiers are NOT
        compared.

        Releasing a chord like cmd+shift+space does not release every key in the same
        instant: whichever modifier comes up first already has its OWN bit cleared by the
        time the actual key's key-up event carries the (now smaller) flag set, so requiring
        `matches`' exact modifier mask on release missed the release far more often than it
        caught it — a real hold-mode session could keep the microphone open, typing into
        whatever the user had focus on next, until the 30-minute idle timeout. The key that
        was pressed down is the one fact that cannot be ambiguous: that is what a physical
        release means, independent of what else the hand let go of first.
        """
        return keycode == self.keycode


def parse(spec: str) -> Combo:
    """``"cmd+shift+space"`` -> a :class:`Combo`, or a :class:`HotkeyError` saying why not."""
    if not spec or not spec.strip():
        raise HotkeyError("a hotkey cannot be empty — e.g. `cmd+shift+space`")
    parts = [p.strip().lower() for p in spec.split("+")]
    if any(not p for p in parts):
        raise HotkeyError(
            f"{spec!r} is not a hotkey — join modifiers and the key with a single `+`, "
            "e.g. `cmd+shift+space`"
        )
    *mods, key = parts
    if not mods:
        raise HotkeyError(
            f"{spec!r} has no modifier — a bare key would fire on every ordinary keystroke; "
            f"add at least one of cmd/shift/ctrl/opt/fn, e.g. `cmd+shift+{spec}`"
        )
    mask = 0
    for name in mods:
        if name not in _MODIFIERS:
            raise HotkeyError(
                f"{name!r} in {spec!r} is not a modifier — use cmd, shift, ctrl, opt or fn"
            )
        mask |= _MODIFIERS[name]
    if not mask & _STRONG_MODIFIERS:
        raise HotkeyError(
            f"{spec!r} has no cmd or ctrl — shift, opt and fn are all things macOS itself "
            "uses during ordinary typing and navigation, so a chord needs cmd or ctrl too, "
            f"e.g. `cmd+{spec}`"
        )
    if key not in _KEYCODES:
        raise HotkeyError(
            f"{key!r} in {spec!r} is not a key stt knows the position of — try a letter, "
            "digit, space, tab, return, an arrow, or f1-f12"
        )
    return Combo(keycode=_KEYCODES[key], modifiers=mask, spec="+".join(parts))


def resolve(configured: str | None) -> Combo:
    """The hotkey a daemon should actually listen for: ``configured``, or the built-in
    default when nobody has set one. Raises :class:`HotkeyError` on a bad spec either way —
    including a bad *default*, which would otherwise fail silently the one time it matters.
    """
    return parse(configured if configured else DEFAULT_HOTKEY)


def resolve_mode(configured: str | None) -> str:
    """The activation mode a daemon should use: ``configured``, or the default, validated."""
    mode = (configured or DEFAULT_MODE).strip().lower()
    if mode not in VALID_MODES:
        raise HotkeyError(f"{mode!r} is not a hotkey mode — use {' or '.join(VALID_MODES)}")
    return mode


def validate(key: str, value: str) -> None:
    """Raise a clean, catchable error if ``value`` is not legal for ``key``
    (``"hotkey"``/``"hotkey_mode"``) — called at the moment ``stt config set``/the TUI store
    it, not left for `resolve`/`resolve_mode` to discover the first time a daemon tries to
    start. That gap was real: an invalid `hotkey` stored this way, uncaught until
    `dictate_daemon` called `resolve`, surfaced as a raw traceback (`HotkeyError` is a
    `ValueError`, not an `SttError` the CLI's top-level handler recognises) instead of the
    clean refusal every other bad setting in this codebase gets — and would have crashed the
    config TUI outright, since its own error handling only catches `SttError`.
    """
    from .._errors import UsageError

    try:
        if key == "hotkey":
            parse(value)
        elif key == "hotkey_mode":
            resolve_mode(value)
    except HotkeyError as bad:
        raise UsageError(
            what=f"{key} is not a valid {key.replace('_', ' ')}",
            why=str(bad),
            how=f"stt config set {key} <a value from --help>, or leave it unset for the default",
        ) from bad
