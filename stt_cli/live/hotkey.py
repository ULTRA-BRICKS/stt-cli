"""hotkey — turn a spec like ``"cmd+shift+space"`` (or ``"fn"``) into what `tap.py` compares
real keyboard events against, and decide which specs each binding may use.

WHY A STRING SPEC AT ALL
    The hotkey has to round-trip through `config.json`, the command line and `stt mic
    --help`, all three of which want a human-typed word, not a keycode nobody can name from
    memory. This module is the one place that word is turned into the two integers Quartz
    actually understands, and the one place that pretends to be a keyboard layout: only the
    US ANSI physical positions are named, because that is what `kVK_*` numbers to begin
    with — a Cyrillic or Kana layout still calls the key left of Return "quote" here.

THREE BINDINGS, EACH WITH ITS OWN CHECK
    `hotkey_press` is a toggle: one press arms a burst, the next press of the same chord ends
    it. `hotkey_hold` is push-to-talk: the burst lasts exactly as long as the chord is held.
    `hotkey_stop` is an optional extra chord that ends a press burst; its default, `same`,
    adds nothing (the re-press always stops). All three are independent and live in one
    daemon at once — see `dictation._watch_daemon`. `parse` checks a spec in layers, and each
    layer refuses with its own message naming the binding and the rule it broke:

    The shared floor (`_check_floor`), for every chord that has a key, whichever binding it
    is for: at least one of cmd or ctrl. `tap.py`'s tap is listen-only — it sees every key
    event and can never stop one from reaching the focused window — so a chord made only of
    shift, opt and fn is also ordinary typing or navigation: shift+d types "D", opt+e starts
    "é", fn+left is Home. Without any modifier a hotkey would fire on every letter typed
    while the daemon idles, which is not a hotkey, it is a keylogger that occasionally opens
    a microphone.

    The press rule (`_check_press`, for `hotkey_press` and `hotkey_stop`): a toggle fires once
    per physical press — `tap.py` never lets an autorepeat key-down toggle — and pressing it
    again is the stop, so one ordinary press of it must never happen by accident. A press
    chord therefore needs a key (a lone modifier is tapped all the time: fn+arrow, the Globe
    key, the start of every shortcut), and must not be one of the editing chords people press
    while dictating (`_EDITING_CHORDS`: undo, cut/copy/paste, select all, save, the cmd+arrow
    jumps, cmd+delete). That collision is silent: an accidental cmd+v would open the
    microphone and leave it open, typing into the document, until somebody noticed.

    The hold rule (`_check_hold`, for `hotkey_hold`): the burst ends on release, so release
    has to be reliable, and that is `tap.py`'s job — it ends a hold on the key's own key-up
    whatever the modifiers say by then, ALSO the moment one of the chord's modifiers is let
    go (a flagsChanged event: releasing cmd first must not leave "d" auto-repeating into the
    document with the microphone open), and never lets an autorepeat key-down re-arm it. On
    top of the floor, hold allows exactly one spec press does not: `fn` alone, on macOS — the
    Mac push-to-talk convention (hold Globe, speak, release). It is seen only through
    flagsChanged, counts only while no key and no other modifier joins it (a key-down while
    fn is held is fn+arrow or fn+delete navigation, and cancels the burst), and arms only
    after `LONE_MODIFIER_ARM_SECONDS` held alone, so a quick fn+arrow never opens the
    microphone at all. Every other lone modifier stays refused: cmd, ctrl, shift and opt are
    held for every shortcut and every capital letter.

    Decided on #101 (2026-09-29): beyond fn alone, press and hold need no different rule for
    a key chord above the floor. An editing chord used for push-to-talk repeats its edit where
    the user is looking, and they let go; the same chord as a toggle silently opens a
    microphone — which is why only press refuses `_EDITING_CHORDS`. A hold binding on shift
    alone was once shipped and reverted: holding shift+d types "D" into the document on every
    intended use, and the floor refuses it.

THE GLOBE KEY
    The tap cannot suppress macOS's own Globe-key action — switch the input source, show the
    emoji picker, or start Dictation — so fn-alone hold works cleanly only when System
    Settings > Keyboard > "Press 🌐 key to" is "Do Nothing" (`AppleFnUsageType` = 0 in
    `com.apple.HIToolbox`). `globe_key_advice` says so whenever fn alone is bound and that
    preference is anything else. stt never changes the preference itself: that needs a
    logout or a private API.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

# The two ways a burst can be armed — "press" arms on a press and disarms on the NEXT press
# of the same chord (a toggle); "hold" arms on press and disarms on release (push-to-talk).
# Which one applies to a burst is a fact about WHICH binding fired (`hotkey_press` vs
# `hotkey_hold`), never a separate setting.
BindingKind = Literal["press", "hold"]
# The settings that each hold one chord. Also the name every refusal message starts with, so
# a person reading one knows which of their bindings it is about.
Binding = Literal["hotkey_press", "hotkey_hold", "hotkey_stop"]
BINDINGS: tuple[Binding, ...] = ("hotkey_press", "hotkey_hold", "hotkey_stop")
# Used when `hotkey_press` is unset in both config and `--hotkey-press`. Cmd+Shift+Space is
# not bound by macOS itself and collides with nothing in Terminal, VS Code, Safari or Chrome
# as shipped — checked by hand against each application's own Keyboard Shortcuts panel. It
# is still just a starting point: `stt config set hotkey_press ...` (or the TUI) is how you
# make it yours. `hotkey_hold` has no default of its own — it starts unset (disabled).
DEFAULT_PRESS_HOTKEY = "cmd+shift+space"
# What a person types to turn OFF a binding. Checked case- and whitespace-insensitively;
# empty string is also accepted, so `stt config set hotkey_hold ""` and leaving it unset
# mean the same thing (both "disabled" — `hotkey_hold` has no default to fall back to).
# For `hotkey_press` the two are DELIBERATELY different: unset falls back to
# `DEFAULT_PRESS_HOTKEY`, while an explicit disable word (including `""`) turns it off —
# `stt config set hotkey_press ""` is not the same as `stt config unset hotkey_press`, and
# the only way to silence the built-in default entirely is the explicit word.
_DISABLE_WORDS = frozenset({"", "none", "off"})
# `hotkey_stop`'s default: no extra chord — the re-press of whichever press chord started
# the burst is the stop, which it always is, with or without a `hotkey_stop` chord. A
# disable word means the same thing, since there is nothing else to turn off.
STOP_SAME = "same"
# Printed by `stt mic --help`, `stt mic run --help`, the config screen and every refusal
# below, so the rules are something a person reads before being refused, not after. macOS
# only: Windows and Linux live dictation do not exist yet, and neither can see fn on most
# keyboards (docs/specs/live-dictation-ux.md, section 2.5).
MODIFIER_RULE = (
    "allowed on macOS: cmd or ctrl (at least one), plus any of shift, opt, fn, and one key "
    "(a letter, digit, punctuation, space, tab, return, delete, an arrow, f1-f12); "
    "fn alone only for hotkey_hold"
)
# How long fn has to be held ALONE before a fn-alone hold opens the microphone. fn+arrow and
# fn+delete are ordinary navigation, and each one starts with fn going down on its own; the
# key that makes it navigation arrives a moment later. Arming at once would flash the
# microphone and its notification on every Page Down. Not measured on real hands yet — the
# live acceptance on #101 is where it gets tuned.
LONE_MODIFIER_ARM_SECONDS = 0.3


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
# The floor: the only modifiers that are never, on their own, how macOS types a character
# or navigates — and never, held down, a chord that keeps typing either.
_STRONG_MODIFIERS = _CONTROL | _COMMAND
# `Combo.keycode` of a chord with no key (fn alone). No real key event carries it, so
# `Combo.matches`/`released` can never fire for one; `tap.Holds` follows it through
# flagsChanged instead.
MODIFIER_ONLY = -1

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
# the twelve function keys. Escape (53) is deliberately absent: people press it constantly
# for unrelated reasons (closing a dialog, leaving a field), which is why the double-Escape
# stop was removed (#79) — a hotkey on it would be the same mistake.
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
    """One parsed hotkey: a physical key plus the exact modifiers held with it — or, for
    `fn` alone, no key at all (`keycode` is `MODIFIER_ONLY`)."""

    keycode: int
    modifiers: int
    spec: str  # the normalized form of what the user typed, for status lines and --help

    @property
    def lone_modifier(self) -> bool:
        """A chord with no key (`fn` alone): followed through flagsChanged, never matched
        against a key-down or key-up."""
        return self.keycode == MODIFIER_ONLY

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


def _mask(names: list[str]) -> int:
    mask = 0
    for name in names:
        mask |= _MODIFIERS[name]
    return mask


def _chord(spec: str) -> tuple[int, int]:
    *mods, key = spec.split("+")
    return _KEYCODES[key], _mask(mods)


# The chords people press while dictating, which a toggle must never be (see the module
# docstring's press rule). Keyed by (keycode, modifier bits), so an alias (`command+v`,
# `cmd+backspace`) is refused the same as the spelling written here.
_EDITING_CHORDS: dict[tuple[int, int], str] = {
    _chord(spec): what
    for spec, what in (
        ("cmd+z", "undo"),
        ("cmd+shift+z", "redo"),
        ("cmd+x", "cut"),
        ("cmd+c", "copy"),
        ("cmd+v", "paste"),
        ("cmd+a", "select all"),
        ("cmd+s", "save"),
        ("cmd+left", "jump to the start of the line"),
        ("cmd+right", "jump to the end of the line"),
        ("cmd+up", "jump to the top"),
        ("cmd+down", "jump to the bottom"),
        ("cmd+delete", "delete to the start of the line"),
    )
}


def parse(spec: str, *, binding: Binding = "hotkey_press") -> Combo:
    """``"cmd+shift+space"`` -> a :class:`Combo`, or a :class:`HotkeyError` saying why not.

    ``binding`` picks the rule on top of the shared floor — the press rule for
    `hotkey_press` (the default, for callers that do not name one) and `hotkey_stop`, the
    hold rule for `hotkey_hold` — and names itself in every message. See the module
    docstring for what each rule is and why.
    """
    if not spec or not spec.strip():
        raise HotkeyError(
            f"{binding}: a hotkey cannot be empty — e.g. `cmd+shift+space`; {MODIFIER_RULE}"
        )
    parts = [p.strip().lower() for p in spec.split("+")]
    if any(not p for p in parts):
        raise HotkeyError(
            f"{binding}: {spec!r} is not a hotkey — join the modifiers and the key with a "
            f"single `+`, e.g. `cmd+shift+space`; {MODIFIER_RULE}"
        )
    named = [_MODIFIERS[part] for part in parts if part in _MODIFIERS]
    if len(set(named)) != len(named):
        # `fn+fn` or `cmd+command+d` would otherwise be stored as typed and shown in every
        # status line, for a chord that is really `fn` or `cmd+d`.
        raise HotkeyError(f"{binding}: {spec!r} names the same modifier twice — {MODIFIER_RULE}")
    normalized = "+".join(parts)
    if all(part in _MODIFIERS for part in parts):
        lone = Combo(keycode=MODIFIER_ONLY, modifiers=_mask(parts), spec=normalized)
        if binding == "hotkey_hold":
            _check_hold(lone)
        else:
            _check_press(lone, binding)
        return lone
    *mods, key = parts
    for name in mods:
        if name not in _MODIFIERS:
            raise HotkeyError(
                f"{binding}: {name!r} in {spec!r} is not a modifier — {MODIFIER_RULE}"
            )
    if key not in _KEYCODES:
        raise HotkeyError(
            f"{binding}: {key!r} in {spec!r} is not a key stt knows the position of — "
            f"{MODIFIER_RULE}"
        )
    combo = Combo(keycode=_KEYCODES[key], modifiers=_mask(mods), spec=normalized)
    _check_floor(combo, binding)
    if binding == "hotkey_hold":
        _check_hold(combo)
    else:
        _check_press(combo, binding)
    return combo


def _check_floor(combo: Combo, binding: Binding) -> None:
    """The one rule every chord with a key obeys, whichever binding it is for: cmd or ctrl."""
    key = combo.spec.rsplit("+", 1)[-1]
    if not combo.modifiers:
        example = _acceptable(binding, f"cmd+shift+{combo.spec}", f"ctrl+opt+{key}")
        raise HotkeyError(
            f"{binding}: {combo.spec!r} has no modifier — a bare key would fire on every "
            f"ordinary keystroke while the daemon waits; add cmd or ctrl, e.g. `{example}`; "
            f"{MODIFIER_RULE}"
        )
    if not combo.modifiers & _STRONG_MODIFIERS:
        example = _acceptable(binding, f"cmd+{combo.spec}", f"ctrl+opt+{key}")
        raise HotkeyError(
            f"{binding}: {combo.spec!r} has no cmd or ctrl — shift, opt and fn on their own "
            "are how macOS types and navigates (shift+d types D, opt+e starts é, fn+left is "
            "Home), and stt only listens to the keyboard, so it cannot stop that keystroke "
            f"reaching your document; e.g. `{example}`; {MODIFIER_RULE}"
        )


def _acceptable(binding: Binding, *candidates: str) -> str:
    """The first of ``candidates`` that ``binding`` would itself accept, else the default —
    a refusal's suggestion must never be refused in turn (a bare `z` once suggested
    `cmd+shift+z`, which is redo and refused for a toggle)."""
    for candidate in candidates:
        try:
            parse(candidate, binding=binding)
        except HotkeyError:
            continue
        return candidate
    return DEFAULT_PRESS_HOTKEY


def _check_press(combo: Combo, binding: Binding) -> None:
    """The toggle's rule (`hotkey_press`, `hotkey_stop`): one ordinary press of it must never
    happen by accident, because each one opens or closes the microphone."""
    if combo.lone_modifier:
        raise HotkeyError(
            f"{binding}: {combo.spec!r} is a modifier with no key — a toggle fires on every "
            "tap, and a modifier is tapped on its own all the time (fn+arrow, the Globe key, "
            "the start of every shortcut), so each tap would open or close the microphone; "
            "use cmd or ctrl plus a key, e.g. `cmd+shift+space` (fn alone works only as "
            f"hotkey_hold, push-to-talk); {MODIFIER_RULE}"
        )
    what = _EDITING_CHORDS.get((combo.keycode, combo.modifiers))
    if what is not None:
        raise HotkeyError(
            f"{binding}: {combo.spec!r} is {what}, which people press while dictating — and "
            "each press of a toggle opens or closes the microphone, so every "
            f"{what} would too; pick a chord no editor uses, e.g. `cmd+shift+space`; "
            f"{MODIFIER_RULE}"
        )


def _check_hold(combo: Combo) -> None:
    """Push-to-talk's rule (`hotkey_hold`): a key chord needs nothing beyond the floor (its
    release is `tap.py`'s job); a lone modifier is allowed only when it is fn, on macOS."""
    if not combo.lone_modifier:
        return
    if combo.modifiers != _FN:
        raise HotkeyError(
            f"hotkey_hold: {combo.spec!r} is a modifier with no key — fn is the only "
            "modifier that can be held alone; cmd, ctrl, shift and opt are held for every "
            "shortcut and every capital letter, so holding one cannot mean 'dictate'; use "
            f"`fn`, or cmd or ctrl plus a key, e.g. `ctrl+opt+d`; {MODIFIER_RULE}"
        )
    if sys.platform != "darwin":
        raise HotkeyError(
            "hotkey_hold: `fn` alone works only on macOS — on Windows and on most Linux "
            "keyboards the fn key is handled inside the keyboard and never reaches software; "
            f"use cmd or ctrl plus a key, e.g. `ctrl+opt+d`; {MODIFIER_RULE}"
        )


def _disabled(configured: str | None) -> bool:
    return configured is not None and configured.strip().lower() in _DISABLE_WORDS


def _resolve_binding(
    configured: str | None, *, default: str | None, binding: Binding
) -> Combo | None:
    """One binding's chord: ``configured``, the binding's own ``default`` if nothing was
    configured, or ``None`` if the binding is explicitly disabled (``configured`` is one of
    `_DISABLE_WORDS`) — shared by `resolve_press`/`resolve_hold` so the "unset means the
    default, an explicit disable-word means off" rule is written once.
    """
    if configured is None:
        return parse(default, binding=binding) if default is not None else None
    if _disabled(configured):
        return None
    return parse(configured, binding=binding)


def resolve_press(configured: str | None) -> Combo | None:
    """The press-style (toggle) binding a daemon should listen for: ``configured``, the
    built-in default when nobody has set one, or ``None`` when explicitly disabled (`stt
    config set hotkey_press none`). Raises :class:`HotkeyError` on a bad spec, including a
    bad *default*, which would otherwise fail silently the one time it matters.
    """
    return _resolve_binding(configured, default=DEFAULT_PRESS_HOTKEY, binding="hotkey_press")


def resolve_hold(configured: str | None) -> Combo | None:
    """The hold-style (push-to-talk) binding a daemon should listen for, or ``None`` when
    nobody has configured one — unlike `resolve_press` there is no built-in default to fall
    back to, so "unset" and "disabled" resolve the same way.
    """
    return _resolve_binding(configured, default=None, binding="hotkey_hold")


def resolve_stop(configured: str | None) -> Combo | None:
    """The extra chord that ends a press burst, or ``None`` for `same` — unset, `same`, or a
    disable word: the re-press of the press chord is then the only stop, as it always is
    anyway. Checked with the press rule, since it is pressed, not held.
    """
    if configured is None or _disabled(configured) or configured.strip().lower() == STOP_SAME:
        return None
    return parse(configured, binding="hotkey_stop")


def resolve(binding: Binding, configured: str | None) -> Combo | None:
    """`resolve_press`/`resolve_hold`/`resolve_stop`, chosen by the setting's name."""
    if binding == "hotkey_press":
        return resolve_press(configured)
    if binding == "hotkey_hold":
        return resolve_hold(configured)
    return resolve_stop(configured)


def same_chord(a: Combo, b: Combo) -> bool:
    """Would ``a`` and ``b`` fire on the exact same physical key event?

    Deliberately not `Combo.__eq__` (which also compares `spec`): "shift+cmd+space" and
    "cmd+shift+space" parse to the same keycode and the same modifier mask but a DIFFERENT
    `spec` string (modifier order is preserved for `--help`/status text), so the equality
    operator would call them different chords when what matters here — can two bindings
    both be armed by literally the same key-down — says they are not.
    """
    return a.keycode == b.keycode and a.modifiers == b.modifiers


def same_keycode(a: Combo, b: Combo) -> bool:
    """Would ``a`` and ``b`` fire on a key-UP for the SAME physical key, regardless of which
    modifiers were held at the time it went down?

    `Combo.released` deliberately ignores modifiers (see its own docstring: whichever
    modifier the hand lets go of first already has its own bit cleared by the time the
    key-up arrives), which means `tap.Holds` cannot tell two bindings sharing a keycode apart
    on release AT ALL — not just when they also share the same modifiers. Two chords that
    pass `same_chord` (different) but share a keycode — ``cmd+d`` and ``ctrl+d`` — are just
    as broken: releasing ``ctrl+d`` fires a release keyed only on ``d``, which `tap.Holds`
    attributes to whichever configured combo it checks FIRST, so the hold binding's own
    `_await_release` never sees its release and the microphone stays open until the
    30-minute idle timeout. `same_chord` is a strict subset of this.
    """
    return a.keycode == b.keycode


def shared_key(
    bindings: Mapping[str, Combo | None],
) -> tuple[tuple[str, Combo], tuple[str, Combo]] | None:
    """The first two configured bindings, as (name, combo), that use the same physical key
    — see `same_keycode` for why sharing a key is ambiguous even with different modifiers —
    or ``None`` when every configured one has its own."""
    seen: list[tuple[str, Combo]] = []
    for name, combo in bindings.items():
        if combo is None:
            continue
        for earlier in seen:
            if same_keycode(earlier[1], combo):
                return earlier, (name, combo)
        seen.append((name, combo))
    return None


def refuse_shared_key(bindings: Mapping[str, Combo | None], *, how: str | None = None) -> None:
    """Raise a diagnosed `UsageError` when two of ``bindings`` share a key — the one check
    `stt config set`, the daemon's flags and daemon start all make, worded once. ``how``
    defaults to advice naming the two clashing settings, for callers whose keys are setting
    names; the flag check passes its own, since its keys are flags."""
    clash = shared_key(bindings)
    if clash is None:
        return
    from .._errors import UsageError

    (first, a), (second, b) = clash
    raise UsageError(
        what=f"{first} and {second} cannot use the same key",
        why=f"{a.spec!r} and {b.spec!r} both use the same physical key — a key RELEASE only "
        "reports which key was let go, not which modifiers were held with it, so releasing "
        "either could be misattributed to the other binding and leave the microphone open "
        "indefinitely",
        how=how
        or (
            "pick a different key for each (the modifiers may still differ), or turn one "
            f"off: {_turn_off(first)} or {_turn_off(second)}"
        ),
    )


def _turn_off(name: str) -> str:
    word = STOP_SAME if name == "hotkey_stop" else "none"
    return f"`stt config set {name} {word}`"


def how_to_fix(binding: Binding) -> str:
    """The one command that repairs a refused or broken ``binding``."""
    if binding == "hotkey_stop":
        return (
            "stt config set hotkey_stop <a chord, e.g. cmd+shift+x>, or "
            "`stt config set hotkey_stop same` to stop only by pressing the press chord again"
        )
    return (
        f"stt config set {binding} <a chord, e.g. cmd+shift+d>, or "
        f"`stt config set {binding} none` to disable it"
    )


def validate(key: str, value: str) -> None:
    """Raise a clean, catchable error if ``value`` is not legal for ``key`` (one of
    `BINDINGS`; any other key is not this module's business) — called at the moment `stt
    config set`/the TUI store it, not left for the resolvers to discover the first time a
    daemon tries to start. That gap was real: an invalid hotkey stored this way surfaced as a
    raw traceback (`HotkeyError` is a `ValueError`, not an `SttError` the CLI's top-level
    handler recognises) and would have crashed the config TUI outright.

    Also refuses the one thing a single-field check cannot see on its own: two bindings
    sharing a KEYCODE, by reading the OTHER fields' stored values (see `same_keycode`). A
    daemon started from a hand-edited `config.json` that reached that state anyway is caught
    by the identical check in `dictation._resolved_hotkeys`.

    ``value`` and each stored sibling are resolved in SEPARATE `try` blocks — found by
    review: one shared `try` blamed the field being SET for a `HotkeyError` that came from a
    sibling's already-stored bad value. Each failure names the field that caused it.
    """
    binding = next((name for name in BINDINGS if name == key), None)
    if binding is None:
        return
    from .. import config
    from .._errors import UsageError

    try:
        mine = resolve(binding, value)
    except HotkeyError as bad:
        raise UsageError(
            what="not a valid hotkey chord",
            why=str(bad),
            how=how_to_fix(binding),
        ) from bad
    stored = config.load_settings()
    resolved: dict[str, Combo | None] = {}
    for name in BINDINGS:
        if name == binding:
            resolved[name] = mine
            continue
        try:
            resolved[name] = resolve(name, getattr(stored, name))
        except HotkeyError as bad:
            raise UsageError(
                what=f"the stored {name} is invalid", why=str(bad), how=how_to_fix(name)
            ) from bad
    refuse_shared_key(resolved)


# `AppleFnUsageType` in `com.apple.HIToolbox`: 0 is "Do Nothing"; 1 changes the input
# source, 2 shows the emoji picker, 3 starts Dictation; absent is the system default, which
# is NOT 0.
FN_USAGE_DO_NOTHING = 0
GLOBE_KEY_ADVICE = (
    'fn alone is your push-to-talk key: set System Settings > Keyboard > "Press 🌐 key to" '
    'to "Do Nothing" — otherwise every press of fn also switches the input source, opens the '
    "emoji picker or starts macOS Dictation"
)


def read_fn_usage() -> int | None:
    """The Globe-key preference (`FN_USAGE_DO_NOTHING` and friends), or ``None`` when it is
    unset or cannot be read. `defaults` rather than the plist file on disk: preferences are
    cached by `cfprefsd`, and the file can lag behind a change made a moment ago."""
    try:
        done = subprocess.run(
            ["defaults", "read", "com.apple.HIToolbox", "AppleFnUsageType"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        return int(done.stdout.strip())
    except ValueError:
        return None


def globe_key_advice(
    hold: Combo | None, *, read: Callable[[], int | None] | None = None
) -> str | None:
    """`GLOBE_KEY_ADVICE` when ``hold`` is fn alone and the Globe key is not set to Do
    Nothing (an unreadable or unset preference included — neither is Do Nothing); ``None``
    otherwise, without reading anything. ``read`` defaults to `read_fn_usage`, looked up
    when called. Advice only: the binding is never refused for it."""
    if hold is None or not hold.lone_modifier or hold.modifiers != _FN:
        return None
    if (read or read_fn_usage)() == FN_USAGE_DO_NOTHING:
        return None
    return GLOBE_KEY_ADVICE
