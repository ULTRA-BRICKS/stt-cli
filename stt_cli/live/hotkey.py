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

WHY TWO INDEPENDENT BINDINGS, NOT ONE CHORD WITH A MODE
    An earlier version of this module had one configurable `hotkey` plus a `hotkey_mode`
    ("toggle" or "hold") that decided what THAT chord meant — so choosing hold-to-talk cost
    you the toggle chord entirely, and vice versa. `resolve_press`/`resolve_hold` below
    are two independent, optionally-set chords instead: press-style (arm on a press, arm
    OFF on the next press of the same chord) and hold-style (arm on press, arm off on
    release) can both be configured and both be live in the same daemon at once — see
    `dictation._watch_daemon`, which now watches for whichever of the two is configured.

WHY HOLD DOES NOT RELAX THE MODIFIER RULE EITHER, EVEN THOUGH IT LOOKED LIKE IT COULD
    A press binding fires on a single, momentary key-down — indistinguishable from one
    ordinary keystroke of the exact same key+modifiers, which is why shift/opt/fn are all
    refused alone (see WHY A HOTKEY MUST CARRY A MODIFIER). A hold binding looked, at
    first, like it could afford to relax that: it does not just need an ordinary keystroke
    to ARM, it needs the SAME chord held down, unchanged, for the whole length of a burst —
    seconds, not milliseconds — to STAY armed, and shift+key sustained for that long is not
    something anybody does in ORDINARY, UNRELATED typing (holding shift+key just spams the
    OS's own key-repeat of one capital letter, which nobody means to sustain by accident).
    An earlier version of this module shipped exactly that relaxation — accepting shift
    alone for `hotkey_hold` — and it was WRONG, caught by an independent review before this
    shipped, for a reason the "accidental collision" framing above never considers: it is
    not about what happens by ACCIDENT, it is about what happens on every INTENTIONAL use.

    `tap.py`'s tap is LISTEN-ONLY (see its own module docstring) — it observes every key
    event, but never blocks one from reaching whatever has focus. Command and Control are
    safe to hold because macOS never uses them, alone or with a letter, to insert a
    character into a text field; an app either binds Cmd+D/Ctrl+D to a shortcut or ignores
    it outright, and either way nothing is TYPED. Shift is fundamentally different: holding
    Shift+D does not merely risk colliding with something else — it reliably, on every
    keyboard, in every text field, TYPES the letter D, then repeats it via the OS's own
    key-repeat, for as long as the chord stays down. A hold binding using `shift+d` would
    therefore corrupt the very document being dictated into on every single legitimate use
    of the feature, not as an edge case but as the guaranteed, designed-in behavior — the
    burst's own "hold the hotkey while you talk" is indistinguishable, to the OS, from
    "hold Shift+D while you talk," and the second one types.

    Option was never a candidate for the same underlying reason, just reached by a
    different route: Option types a dead-key accent or symbol under many letters (opt+e
    starts é), so opt+key held down has the identical self-typing problem PLUS the
    sustained-hold collision already documented above (opt+arrow/opt+delete repeat a
    word-jump/word-delete during ordinary editing). Fn does not type a character for an
    ordinary letter, but is excluded for the sustained-hold collision on its own two key
    families (fn+arrow/fn+delete repeat a page-scroll/forward-delete). No weak modifier
    survives BOTH tests — "never types a character when held" and "never collides with an
    ordinary sustained hold" — the way Command and Control already do, which is why
    `_STRONG_MODIFIERS_HOLD` is, after this correction, identical to
    `_STRONG_MODIFIERS_PRESS`: both require Command or Control, full stop. Kept as two
    separately named constants rather than one, because the two are safe for genuinely
    different reasons (press: a single ordinary keystroke must never arm it; hold: NEITHER
    an ordinary keystroke NOR the sustained hold itself may arm it or corrupt the
    document), and a future, more narrowly-scoped relaxation for one of them — a modifier
    combination proven never to type a character under any layout, restricted to key
    families with no known sustained-hold side effect — has a named place to live without
    re-deriving this whole investigation from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The two independent ways a binding can arm/disarm a burst — "press" arms on a press and
# disarms on the NEXT press of the same chord (a toggle); "hold" arms on press and disarms
# on release (push-to-talk). Which one applies to a burst is a fact about WHICH binding
# fired (`hotkey_press` vs `hotkey_hold`), never a separate setting — see the module
# docstring. A `Literal` rather than a runtime tuple of choices (the old `VALID_MODES`) on
# purpose: nothing parses this from user input any more, so mypy checking every branch that
# switches on it is worth more than a runtime membership test nothing calls.
BindingKind = Literal["press", "hold"]
# Used when `hotkey_press` is unset in both config and `--hotkey-press`. Cmd+Shift+Space is
# not bound by macOS itself and collides with nothing in Terminal, VS Code, Safari or Chrome
# as shipped — checked by hand against each application's own Keyboard Shortcuts panel. It
# is still just a starting point: `stt config set hotkey_press ...` (or the TUI) is how you
# make it yours. `hotkey_hold` has no default of its own — it starts unset (disabled), since
# there is no chord that is obviously right for a push-to-talk style nobody asked for yet.
DEFAULT_PRESS_HOTKEY = "cmd+shift+space"
# What a person types to turn OFF a binding. Checked case- and whitespace-insensitively;
# empty string is also accepted, so `stt config set hotkey_hold ""` and leaving it unset
# mean the same thing (both "disabled" — `hotkey_hold` has no default to fall back to).
# For `hotkey_press` the two are DELIBERATELY different: unset falls back to
# `DEFAULT_PRESS_HOTKEY`, while an explicit disable word (including `""`) turns it off —
# `stt config set hotkey_press ""` is not the same as `stt config unset hotkey_press`, and
# the only way to silence the built-in default entirely is the explicit word.
_DISABLE_WORDS = frozenset({"", "none", "off"})


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
# Shift alone does not make a PRESS/toggle chord safe: every capital letter and every
# shifted symbol ("!", "@", "$", ...) a person types ordinarily is shift+key, so a toggle
# that only required shift would fire constantly during normal typing — exactly the
# "keylogger that occasionally opens a microphone" failure the module docstring warns
# about, just gated on capitalization instead of on nothing at all.
#
# Option and Fn are not safe for PRESS either, even though neither is used for
# capitalization: Option is how macOS types a dead-key accent (opt+e/u/n start é/ü/ñ) and
# punctuation (opt+space is a non-breaking space), and Fn is the laptop-keyboard route to
# Home/End/PageUp/PageDown (fn+left/right/up/down) — both are things people press
# constantly while doing nothing hotkey-like at all. Only Command and Control are never,
# on their own, how macOS itself produces a character or a navigation event; `parse`
# requires at least one of them for a press binding.
#
# A HOLD binding stays down for a burst-length few seconds, so it asks a SECOND question a
# press never has to: does holding this modifier+key down, unbroken, for that whole span
# ALSO type into whatever has focus — since `tap.py`'s tap is listen-only and never blocks
# the real event. Command and Control pass both tests (never a single ordinary keystroke,
# never a sustained one either — no app-independent OS behaviour types a character for
# either of them). Shift fails the SECOND test even though it looked like it might pass the
# first: an earlier version of this constant added Shift for hold on exactly that "nobody
# sustains it by accident" reasoning, and an independent review caught the real problem —
# holding Shift+D is not an accident to guard against, it is the feature's own INTENDED
# behaviour, and it reliably types "D" (then repeats it) into the document being dictated
# into for the whole burst. Option fails both tests (opt+letter types an accent AND
# opt+arrow/opt+delete repeat a word-jump/word-delete). Fn fails the second test alone
# (fn+arrow/fn+delete repeat a page-scroll/forward-delete) even though it never types a
# character for an ordinary letter. See the module docstring's "WHY HOLD DOES NOT RELAX THE
# MODIFIER RULE EITHER" section for the full investigation, including the relaxation this
# replaced and why it was wrong. `_STRONG_MODIFIERS_HOLD` is therefore identical to
# `_STRONG_MODIFIERS_PRESS` — kept as two named constants because the two are safe for
# different reasons, not because the required bits currently differ.
_STRONG_MODIFIERS_PRESS = _CONTROL | _COMMAND
_STRONG_MODIFIERS_HOLD = _STRONG_MODIFIERS_PRESS

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


def parse(spec: str, *, kind: BindingKind = "press") -> Combo:
    """``"cmd+shift+space"`` -> a :class:`Combo`, or a :class:`HotkeyError` saying why not.

    ``kind`` selects which strong-modifier rule applies — "press" (the default, matching
    every existing caller that does not name a binding) and "hold" both require cmd or
    ctrl today (`_STRONG_MODIFIERS_PRESS`/`_STRONG_MODIFIERS_HOLD` are currently the same
    value). See the module docstring's "WHY HOLD DOES NOT RELAX THE MODIFIER RULE EITHER"
    section for why a relaxation was investigated, shipped, and then reverted, and the
    comment above `_STRONG_MODIFIERS_HOLD` for why the parameter still exists.
    """
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
    strong = _STRONG_MODIFIERS_HOLD if kind == "hold" else _STRONG_MODIFIERS_PRESS
    if not mask & strong:
        raise HotkeyError(
            f"{spec!r} has no cmd or ctrl — shift, opt and fn are all things macOS itself "
            "uses during ordinary typing and navigation (and, held down, opt/fn+arrow or "
            "delete repeat an edit while shift+key keeps typing the letter itself), so a "
            f"chord needs cmd or ctrl too, e.g. `cmd+{spec}`"
        )
    if key not in _KEYCODES:
        raise HotkeyError(
            f"{key!r} in {spec!r} is not a key stt knows the position of — try a letter, "
            "digit, space, tab, return, an arrow, or f1-f12"
        )
    return Combo(keycode=_KEYCODES[key], modifiers=mask, spec="+".join(parts))


def _disabled(configured: str | None) -> bool:
    return configured is not None and configured.strip().lower() in _DISABLE_WORDS


def _resolve_binding(
    configured: str | None, *, default: str | None, kind: BindingKind
) -> Combo | None:
    """One binding's chord: ``configured``, the binding's own ``default`` if nothing was
    configured, or ``None`` if the binding is explicitly disabled (``configured`` is one of
    `_DISABLE_WORDS`) — shared by `resolve_press`/`resolve_hold` so the "unset means the
    default, an explicit disable-word means off" rule is written once. ``kind`` is forwarded
    to `parse` unchanged, so each binding is validated against ITS OWN strong-modifier rule.
    """
    if configured is None:
        return parse(default, kind=kind) if default is not None else None
    if _disabled(configured):
        return None
    return parse(configured, kind=kind)


def resolve_press(configured: str | None) -> Combo | None:
    """The press-style (toggle) binding a daemon should listen for: ``configured``, the
    built-in default when nobody has set one, or ``None`` when explicitly disabled (`stt
    config set hotkey_press none`). Raises :class:`HotkeyError` on a bad spec, including a
    bad *default*, which would otherwise fail silently the one time it matters — validated
    against the PRESS strong-modifier rule (`_STRONG_MODIFIERS_PRESS`: cmd or ctrl only).
    """
    return _resolve_binding(configured, default=DEFAULT_PRESS_HOTKEY, kind="press")


def resolve_hold(configured: str | None) -> Combo | None:
    """The hold-style (push-to-talk) binding a daemon should listen for, or ``None`` when
    nobody has configured one — unlike `resolve_press` there is no built-in default to fall
    back to, so "unset" and "disabled" resolve the same way. Validated against the HOLD
    strong-modifier rule (`_STRONG_MODIFIERS_HOLD`), currently identical to press's (cmd or
    ctrl only) — see the module docstring's "WHY HOLD DOES NOT RELAX THE MODIFIER RULE
    EITHER" section for why a relaxation looked possible and was not safe.
    """
    return _resolve_binding(configured, default=None, kind="hold")


def same_chord(a: Combo, b: Combo) -> bool:
    """Would ``a`` and ``b`` fire on the exact same physical key event?

    Deliberately not `Combo.__eq__` (which also compares `spec`): "shift+cmd+space" and
    "cmd+shift+space" parse to the same keycode and the same modifier mask but a DIFFERENT
    `spec` string (modifier order is preserved for `--help`/status text), so the equality
    operator would call them different chords when what matters here — can `hotkey_press`
    and `hotkey_hold` both be armed by literally the same key-down — says they are not.
    """
    return a.keycode == b.keycode and a.modifiers == b.modifiers


def same_keycode(a: Combo, b: Combo) -> bool:
    """Would ``a`` and ``b`` fire on a key-UP for the SAME physical key, regardless of which
    modifiers were held at the time it went down?

    `Combo.released` deliberately ignores modifiers (see its own docstring: whichever
    modifier the hand lets go of first already has its own bit cleared by the time the
    key-up arrives), which means `tap.Watcher` cannot tell two bindings sharing a keycode
    apart on release AT ALL — not just when they also share the same modifiers. Two chords
    that pass `same_chord` (different) but share a keycode — ``cmd+d`` and ``ctrl+d`` — are
    just as broken: holding and releasing ``ctrl+d`` fires a release keyed only on ``d``,
    which `tap.Watcher` attributes to whichever configured combo it checks FIRST (`cmd+d`,
    since `hotkey_press` is always listed before `hotkey_hold` — see
    `dictation._watch_daemon`), so the hold binding's own `_await_release` never sees its
    release and the microphone stays open until the 30-minute idle timeout. `same_chord` is
    a strict subset of this (identical keycode AND identical modifiers implies identical
    keycode) — `validate`/`dictation._resolved_hotkeys` check this one instead, which
    refuses everything `same_chord` would have and the keycode-only collision besides.
    """
    return a.keycode == b.keycode


def validate(key: str, value: str) -> None:
    """Raise a clean, catchable error if ``value`` is not legal for ``key``
    (``"hotkey_press"``/``"hotkey_hold"``) — called at the moment ``stt config set``/the TUI
    store it, not left for `resolve_press`/`resolve_hold` to discover the first time a
    daemon tries to start. That gap was real: an invalid `hotkey` stored this way, uncaught
    until `dictate_daemon` resolved it, surfaced as a raw traceback (`HotkeyError` is a
    `ValueError`, not an `SttError` the CLI's top-level handler recognises) instead of the
    clean refusal every other bad setting in this codebase gets — and would have crashed the
    config TUI outright, since its own error handling only catches `SttError`.

    Also refuses the one thing a single-field check cannot see on its own: `hotkey_press`
    and `hotkey_hold` resolving to combos that share a KEYCODE — not just an identical chord
    — by reading the OTHER field's currently stored value. A key-release carries only the
    keycode, not the modifiers (`Combo.released`), so two bindings sharing a key are
    ambiguous on release even with different modifiers — see `same_keycode`. A daemon
    started from a hand-edited `config.json` that reached this same state anyway is caught
    by the identical check in `dictation._resolved_hotkeys`.

    ``value`` and the OTHER field's stored value are resolved in two SEPARATE `try` blocks,
    not one shared one — found by review: a single shared `try` blamed whichever field was
    being SET for a `HotkeyError` that actually came from the sibling's already-stored,
    unrelated bad value (a hand-edited `config.json`'s `hotkey_hold` gone invalid would have
    made `stt config set hotkey_press cmd+shift+space` — a perfectly valid value — refused
    with a message blaming `hotkey_press`). Each field's own failure is now attributed to
    the field that actually caused it.
    """
    from .. import config
    from .._errors import UsageError

    sibling = "hotkey_hold" if key == "hotkey_press" else "hotkey_press"
    try:
        if key == "hotkey_press":
            press = resolve_press(value)
        elif key == "hotkey_hold":
            hold = resolve_hold(value)
        else:
            return
    except HotkeyError as bad:
        raise UsageError(
            what=f"{key} is not a valid {key.replace('_', ' ')}",
            why=str(bad),
            how=f"stt config set {key} <a chord, e.g. cmd+shift+d>, or "
            f"`stt config set {key} none` to disable it",
        ) from bad
    try:
        if key == "hotkey_press":
            hold = resolve_hold(config.load_settings().hotkey_hold)
        else:
            press = resolve_press(config.load_settings().hotkey_press)
    except HotkeyError as bad:
        raise UsageError(
            what=f"the stored {sibling} is invalid",
            why=str(bad),
            how=f"stt config set {sibling} <a chord, e.g. cmd+shift+d>, or "
            f"`stt config set {sibling} none` to disable it",
        ) from bad
    if press is not None and hold is not None and same_keycode(press, hold):
        raise UsageError(
            what="hotkey_press and hotkey_hold cannot use the same key",
            why=f"{press.spec!r} and {hold.spec!r} both use the same physical key — a key "
            "RELEASE only reports which key was let go, not which modifiers were held with "
            "it, so releasing either could be misattributed to the other binding and leave "
            "the microphone open indefinitely",
            how="pick two different keys for hotkey_press and hotkey_hold (the modifiers "
            "may still differ), or `stt config set hotkey_hold none` (or hotkey_press) to "
            "disable the one you do not need",
        )
