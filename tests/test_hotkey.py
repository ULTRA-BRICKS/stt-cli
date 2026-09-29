"""Push-to-talk: the hotkey spec parser (with its distinct press/hold validation rules), the
tap's multi-hotkey routing, the `stt mic` daemon's idle<->dictating loop for BOTH concurrent
bindings, and the CLI plumbing that wires `--hotkey-press`/`--hotkey-hold` into all three.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import struct

import pytest

from stt_cli import config
from stt_cli._errors import EngineError
from stt_cli.live.gate import HANGOVER_MS, RATE
from stt_cli.live.hotkey import Combo, HotkeyError, parse, resolve_hold, resolve_press, same_chord


def tone(ms: int, amplitude: int) -> bytes:
    count = RATE * ms // 1000
    return struct.pack(
        f"<{count}h",
        *[int(amplitude * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(count)],
    )


SPEECH = tone(1200, 6000)
SILENCE = tone(HANGOVER_MS + 400, 20)


# ── hotkey.parse / resolve_press / resolve_hold ──────────────────────────────


def test_a_chord_parses_to_its_keycode_and_modifier_bits() -> None:
    combo = parse("cmd+shift+space")
    assert combo == Combo(keycode=49, modifiers=(1 << 20) | (1 << 17), spec="cmd+shift+space")


def test_modifier_aliases_and_case_are_normalized() -> None:
    def signature(combo: Combo) -> tuple[int, int]:
        return combo.keycode, combo.modifiers

    assert signature(parse("Command+SPACE")) == signature(parse("cmd+space"))
    assert (
        signature(parse("cmd+option+a"))
        == signature(parse("cmd+opt+a"))
        == signature(parse("cmd+alt+a"))
    )
    assert signature(parse("control+a")) == signature(parse("ctrl+a"))


def test_a_hotkey_with_no_modifier_is_refused() -> None:
    """The whole design rests on telling a command from ordinary typing — a bare key would
    fire on every letter the user types while the daemon is idle. True for either binding
    kind: a missing modifier is refused before `kind` is ever consulted."""
    with pytest.raises(HotkeyError, match="no modifier"):
        parse("d")
    with pytest.raises(HotkeyError, match="no modifier"):
        parse("d", kind="hold")


def test_a_press_binding_refuses_shift_opt_or_fn_alone() -> None:
    """A press/toggle binding fires on a single, momentary key-down — indistinguishable from
    one ordinary keystroke of the exact same key+modifiers, so shift, opt and fn are all
    refused alone: every capital letter is shift+key, opt+e types an accent, fn+arrow is
    Home/End/PageUp/PageDown — all constant, ordinary events with no modifier at all."""
    for spec in ("shift+a", "opt+space", "opt+e", "opt+shift+a", "fn+left", "fn+d"):
        with pytest.raises(HotkeyError, match="cmd or ctrl"):
            parse(spec, kind="press")
    parse("cmd+shift+a", kind="press")  # shift is fine ALONGSIDE a real modifier
    parse("cmd+opt+space", kind="press")
    parse("cmd+fn+d", kind="press")


def test_a_hold_binding_refuses_shift_alone_same_as_a_press_binding() -> None:
    """`hotkey_hold` once relaxed this rule to accept shift alone, reasoning that nobody
    sustains shift+key for a burst-length few seconds by ACCIDENT (the OS's own key-repeat
    spams the capital letter instantly, self-correcting garbage). An independent review
    caught the real problem: `tap.py`'s tap is listen-only, so a sustained `shift+d` hold
    does not just risk an accidental collision, it reliably TYPES "d" (then repeats it) into
    the document being dictated into on every single INTENTIONAL use — cmd/ctrl have no such
    self-typing behaviour, held or not. The relaxation was reverted: both binding kinds now
    require cmd or ctrl, full stop (see the module docstring's "WHY HOLD DOES NOT RELAX THE
    MODIFIER RULE EITHER" section)."""
    with pytest.raises(HotkeyError, match="cmd or ctrl"):
        parse("shift+d", kind="hold")
    with pytest.raises(HotkeyError, match="cmd or ctrl"):
        parse("shift+d", kind="press")

    parse("cmd+shift+d", kind="hold")  # shift is fine ALONGSIDE a real modifier, same as press


def test_a_hold_binding_still_refuses_option_and_fn_alone() -> None:
    """Option and Fn were never candidates for the relaxation shift briefly got: opt+letter
    types a dead-key accent/symbol (identical self-typing problem to shift), and both
    opt+arrow/opt+delete and fn+arrow/fn+delete repeat a word-jump/word-delete or
    page-scroll/forward-delete during ordinary, burst-length sustained use — so both stay
    exactly as forbidden-alone for hold as they are for press."""
    for spec in ("opt+left", "opt+delete", "opt+e", "fn+left", "fn+delete", "fn+d"):
        with pytest.raises(HotkeyError, match="cmd or ctrl"):
            parse(spec, kind="hold")
    parse("cmd+opt+d", kind="hold")  # opt/fn are fine ALONGSIDE a real modifier, same as press
    parse("ctrl+fn+d", kind="hold")


@pytest.mark.parametrize("spec", ["cmd+d", "ctrl+d"])
def test_each_strong_modifier_alone_is_valid_for_either_binding_kind(spec: str) -> None:
    """Only cmd and ctrl are never, on their own, how macOS itself produces a character or a
    navigation event, and never, held down, sustain a self-typing chord either — valid for
    both press and hold, unlike shift/opt (which type a character) or fn (which sustains a
    scroll/forward-delete)."""
    parse(spec, kind="press")
    parse(spec, kind="hold")


def test_an_unknown_modifier_is_refused() -> None:
    with pytest.raises(HotkeyError, match="not a modifier"):
        parse("super+d")


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(HotkeyError, match="not a key"):
        parse("cmd+doesnotexist")


def test_escape_is_not_a_valid_hotkey_key() -> None:
    """`tap.py` already owns Escape for stop-correcting; a hotkey sharing it would fight
    that, so it is not in the table at all — for either binding kind."""
    with pytest.raises(HotkeyError):
        parse("cmd+escape")
    with pytest.raises(HotkeyError):
        parse("cmd+escape", kind="hold")


@pytest.mark.parametrize("spec", ["", "   ", "cmd+", "+d", "cmd++d"])
def test_a_malformed_spec_is_refused(spec: str) -> None:
    with pytest.raises(HotkeyError):
        parse(spec)


def test_resolve_press_falls_back_to_the_stated_default_when_unset() -> None:
    from stt_cli.live.hotkey import DEFAULT_PRESS_HOTKEY

    assert resolve_press(None) == parse(DEFAULT_PRESS_HOTKEY)


def test_resolve_hold_has_no_default_and_is_none_when_unset() -> None:
    """Unlike `resolve_press`, `resolve_hold` has no built-in chord — nothing was ever
    obviously right for a push-to-talk style nobody asked for by default."""
    assert resolve_hold(None) is None


@pytest.mark.parametrize("word", ["", "none", "NONE", " off ", "Off"])
def test_either_resolver_treats_a_disable_word_as_explicitly_off(word: str) -> None:
    """Explicitly off (`stt config set hotkey_press none`) and merely unset must resolve the
    same way for `hotkey_hold` (both `None`) but DIFFERENTLY for `hotkey_press`: unset falls
    back to the built-in default, an explicit disable word does not."""
    assert resolve_press(word) is None
    assert resolve_hold(word) is None


def test_resolve_press_and_resolve_hold_raise_on_a_bad_spec_including_the_default() -> None:
    with pytest.raises(HotkeyError):
        resolve_press("d")
    with pytest.raises(HotkeyError):
        resolve_hold("d")


def test_same_chord_ignores_spec_text_but_not_keycode_or_modifiers() -> None:
    """`"shift+cmd+space"` and `"cmd+shift+space"` parse to the same keycode/modifier bits
    but a DIFFERENT `spec` string (order is preserved for status text) — `same_chord` must
    call them the same chord even though `Combo.__eq__` (which compares `spec` too) would
    not."""
    a = parse("cmd+shift+space")
    b = parse("shift+cmd+space")
    assert a != b, "the spec strings differ, so plain equality says they are different"
    assert same_chord(a, b), "but they fire on the exact same physical key event"
    assert not same_chord(a, parse("cmd+shift+d")), "a different key is not the same chord"
    assert not same_chord(a, parse("cmd+space")), "a different modifier mask is not either"


def test_same_keycode_is_broader_than_same_chord() -> None:
    """`cmd+d` and `ctrl+d` are NOT `same_chord` (different modifiers) but ARE
    `same_keycode` — the distinction `hotkey.validate`/`dictation._resolved_hotkeys` now
    check, because a key-RELEASE only reports the keycode, never the modifiers, so
    `tap.Watcher` cannot tell two bindings sharing a key apart on release even when their
    modifiers genuinely differ."""
    from stt_cli.live.hotkey import same_keycode

    cmd_d = parse("cmd+d")
    ctrl_d = parse("ctrl+d")
    assert not same_chord(cmd_d, ctrl_d), "different modifiers — not the same chord"
    assert same_keycode(cmd_d, ctrl_d), "but the same physical key, which is what matters here"
    assert not same_keycode(cmd_d, parse("cmd+e")), "a genuinely different key is not"


# ── Combo.matches / released — caps lock and keypad bits must not break a held hotkey ──


def test_matches_ignores_caps_lock_and_keypad_bits() -> None:
    combo = parse("cmd+shift+space")
    caps_lock_bit = 1 << 16
    numeric_pad_bit = 1 << 21
    assert combo.matches(49, combo.modifiers | caps_lock_bit | numeric_pad_bit)


def test_matches_requires_the_exact_modifier_set() -> None:
    combo = parse("cmd+shift+space")
    cmd_only = 1 << 20
    assert not combo.matches(49, cmd_only), "shift was dropped"
    cmd_shift_ctrl = combo.modifiers | (1 << 18)
    assert not combo.matches(49, cmd_shift_ctrl), "an extra modifier was added"


def test_matches_requires_the_right_keycode() -> None:
    combo = parse("cmd+shift+space")
    assert not combo.matches(50, combo.modifiers)


# ── tap.Watcher — multi-hotkey routing, faked at the CGEvent boundary ───────


class _FakeServices:
    """Answers the CGEvent field reads `_handle` makes, without a real event."""

    def __init__(self, *, keycode: int, flags: int, stamp: int = 0, autorepeat: int = 0) -> None:
        self._keycode = keycode
        self._flags = flags
        self._stamp = stamp
        self._autorepeat = autorepeat

    def CGEventGetIntegerValueField(self, _event: int, field: int) -> int:
        from stt_cli.live import tap

        if field == tap._SOURCE_USER_DATA:
            return self._stamp
        if field == tap._AUTOREPEAT_FIELD:
            return self._autorepeat
        return self._keycode

    def CGEventGetFlags(self, _event: int) -> int:
        return self._flags

    def CGEventTapEnable(self, _port: int, _enabled: bool) -> None:
        pass


class _FakeAPI:
    def __init__(self, **kw: object) -> None:
        self.services = _FakeServices(**kw)  # type: ignore[arg-type]


def _watcher_with_fake_events(
    monkeypatch, *, keycode: int, flags: int, stamp: int = 0, autorepeat: int = 0, **kw
):
    from stt_cli.live import tap

    monkeypatch.setattr(
        tap,
        "frameworks",
        lambda: _FakeAPI(keycode=keycode, flags=flags, stamp=stamp, autorepeat=autorepeat),
    )
    fired: list[tuple[str, object]] = []
    watcher = tap.Watcher(
        marker=tap.new_marker(),
        on_key=lambda code: fired.append(("key", code)),
        **kw,
    )
    return watcher, fired


def test_a_matching_key_down_fires_on_hotkey_and_also_interrupts_correction(monkeypatch) -> None:
    """The listen-only tap cannot swallow the hotkey's own keystroke — macOS still delivers
    it to whatever has focus (Option+letter can insert a dead-key character; some apps bind
    Control+letter to caret motion), so it must count as "the user touched the keyboard"
    exactly like any other key, in ADDITION to firing the hotkey."""
    combo = parse("cmd+shift+space")
    hotkey_events: list[tuple[Combo, bool]] = []
    watcher, fired = _watcher_with_fake_events(
        monkeypatch,
        keycode=combo.keycode,
        flags=combo.modifiers,
        hotkeys=[combo],
        on_hotkey=lambda c, pressed: hotkey_events.append((c, pressed)),
    )
    from stt_cli.live import tap

    watcher._handle(0, tap._KEY_DOWN, 1, 0)
    assert hotkey_events == [(combo, True)]
    assert fired == [("key", combo.keycode)], "the correction in flight must be disowned too"


def test_a_matching_key_up_fires_on_hotkey_false(monkeypatch) -> None:
    combo = parse("cmd+shift+space")
    hotkey_events: list[tuple[Combo, bool]] = []
    watcher, fired = _watcher_with_fake_events(
        monkeypatch,
        keycode=combo.keycode,
        flags=combo.modifiers,
        hotkeys=[combo],
        on_hotkey=lambda c, pressed: hotkey_events.append((c, pressed)),
    )
    from stt_cli.live import tap

    watcher._handle(0, tap._KEY_UP, 1, 0)
    assert hotkey_events == [(combo, False)]
    assert fired == [], "a release was never the user typing something"


def test_a_release_matches_by_keycode_even_if_the_modifiers_already_let_go(monkeypatch) -> None:
    """Releasing cmd+shift+space does not release every key in the same instant. Whichever
    modifier the hand lets go of first already has its OWN bit cleared by the time the
    actual key's key-up arrives, so requiring the full modifier mask on release (the same
    check `matches` uses for key-down) missed the release far more often than it caught it
    — a hold-mode burst could keep the microphone open, typing into whatever the user
    focused next, until the 30-minute idle timeout."""
    combo = parse("cmd+shift+space")
    hotkey_events: list[tuple[Combo, bool]] = []
    watcher, _fired = _watcher_with_fake_events(
        monkeypatch,
        keycode=combo.keycode,
        flags=0,  # every modifier already released by the time this key-up arrives
        hotkeys=[combo],
        on_hotkey=lambda c, pressed: hotkey_events.append((c, pressed)),
    )
    from stt_cli.live import tap

    watcher._handle(0, tap._KEY_UP, 1, 0)
    assert hotkey_events == [(combo, False)], "the release must fire on keycode alone"


def test_an_os_autorepeat_key_down_does_not_fire_a_second_press(monkeypatch) -> None:
    """Holding a hotkey down makes the OS resend key-down as a repeat after its normal
    repeat delay. Toggling on every one of those turned a single hold into toggle-mode
    self-cancelling almost immediately, and made hold-mode look like a fresh press instead
    of "still holding"."""
    combo = parse("cmd+shift+space")
    hotkey_events: list[tuple[Combo, bool]] = []
    watcher, fired = _watcher_with_fake_events(
        monkeypatch,
        keycode=combo.keycode,
        flags=combo.modifiers,
        autorepeat=1,
        hotkeys=[combo],
        on_hotkey=lambda c, pressed: hotkey_events.append((c, pressed)),
    )
    from stt_cli.live import tap

    watcher._handle(0, tap._KEY_DOWN, 1, 0)
    assert hotkey_events == [], "a repeat must not toggle anything"
    assert fired == [("key", combo.keycode)], (
        "the OS still delivered the repeated keystroke to whatever has focus, so it must "
        "still disown the correction in flight, same as any other key"
    )


def test_an_ordinary_key_still_reaches_on_key_when_a_hotkey_is_configured(monkeypatch) -> None:
    combo = parse("cmd+shift+space")
    watcher, fired = _watcher_with_fake_events(
        monkeypatch,
        keycode=99,  # not the hotkey's keycode
        flags=0,
        hotkeys=[combo],
        on_hotkey=lambda c, pressed: None,
    )
    from stt_cli.live import tap

    watcher._handle(0, tap._KEY_DOWN, 1, 0)
    assert fired == [("key", 99)]


def test_our_own_marker_is_ignored_even_when_it_matches_the_hotkey(monkeypatch) -> None:
    """`quartz.type_text`'s own synthetic keystrokes must never be mistaken for the hotkey."""
    combo = parse("cmd+shift+space")
    from stt_cli.live import tap

    marker = tap.new_marker()
    monkeypatch.setattr(
        tap,
        "frameworks",
        lambda: _FakeAPI(keycode=combo.keycode, flags=combo.modifiers, stamp=marker),
    )
    hotkey_events: list[tuple[Combo, bool]] = []
    fired: list[tuple[str, object]] = []
    watcher = tap.Watcher(
        marker=marker,
        on_key=lambda code: fired.append(("key", code)),
        hotkeys=[combo],
        on_hotkey=lambda c, pressed: hotkey_events.append((c, pressed)),
    )
    watcher._handle(0, tap._KEY_DOWN, 1, 0)
    assert hotkey_events == [] and fired == []


def test_a_watcher_with_no_hotkeys_behaves_exactly_as_before(monkeypatch) -> None:
    """The ad-hoc single-session path never configures a hotkey; this pins that its
    behaviour is untouched by any of the above — key-down reaches `on_key`, nothing else
    is ever consulted."""
    from stt_cli.live import tap

    monkeypatch.setattr(tap, "frameworks", lambda: _FakeAPI(keycode=42, flags=0, stamp=0))
    fired: list[tuple[str, object]] = []
    watcher = tap.Watcher(marker=tap.new_marker(), on_key=lambda code: fired.append(("key", code)))
    watcher._handle(0, tap._KEY_DOWN, 1, 0)
    assert fired == [("key", 42)]


def test_two_configured_hotkeys_are_each_routed_by_their_own_identity(monkeypatch) -> None:
    """A daemon with BOTH `hotkey_press` and `hotkey_hold` configured passes both to the
    SAME `Watcher` (see `dictation._watch_daemon`) — this is the tap-level half of "the
    daemon correctly watches both bindings concurrently": a key-down matching one of the two
    must report exactly THAT combo, never the other, so `dictation._await_release` can tell
    which binding armed the current burst."""
    press = parse("cmd+shift+space")
    hold = parse("ctrl+opt+d")
    hotkey_events: list[tuple[Combo, bool]] = []
    watcher, _fired = _watcher_with_fake_events(
        monkeypatch,
        keycode=hold.keycode,
        flags=hold.modifiers,
        hotkeys=[press, hold],
        on_hotkey=lambda c, pressed: hotkey_events.append((c, pressed)),
    )
    from stt_cli.live import tap

    watcher._handle(0, tap._KEY_DOWN, 1, 0)
    assert hotkey_events == [(hold, True)], "the hold binding's own key-down, not the press one"

    hotkey_events.clear()
    watcher2, _fired2 = _watcher_with_fake_events(
        monkeypatch,
        keycode=press.keycode,
        flags=press.modifiers,
        hotkeys=[press, hold],
        on_hotkey=lambda c, pressed: hotkey_events.append((c, pressed)),
    )
    watcher2._handle(0, tap._KEY_DOWN, 1, 0)
    assert hotkey_events == [(press, True)], "and the press binding's own key-down, not hold's"


def test_key_up_is_only_added_to_the_mask_when_a_hotkey_is_configured() -> None:
    """A watcher with no hotkeys must ask for exactly the same events it always did — adding
    key-up unconditionally would be a behaviour change to the ad-hoc path for no reason."""
    from stt_cli.live import tap

    no_hotkey_mask = tap._event_mask(())
    with_hotkey_mask = tap._event_mask([parse("cmd+shift+space")])
    assert not no_hotkey_mask & (1 << tap._KEY_UP), "no hotkey: key-up is not requested"
    assert with_hotkey_mask & (1 << tap._KEY_UP), "a hotkey: key-up IS requested"
    assert no_hotkey_mask & (1 << tap._KEY_DOWN), "key-down is always requested"
    assert with_hotkey_mask & (1 << tap._KEY_DOWN), "adding key-up never drops key-down"
    for button in tap._MOUSE_DOWN:
        assert no_hotkey_mask & (1 << button) and with_hotkey_mask & (1 << button)
    assert with_hotkey_mask == no_hotkey_mask | (1 << tap._KEY_UP), (
        "a hotkey adds exactly one bit to the ad-hoc mask, nothing else changes"
    )
    # Two hotkeys ask for exactly the same mask as one — key-up is a single bit, not one
    # per configured binding.
    both = tap._event_mask([parse("cmd+shift+space"), parse("ctrl+opt+d")])
    assert both == with_hotkey_mask


# ── config round-trip ────────────────────────────────────────────────────────


def test_hotkey_press_and_hotkey_hold_round_trip_through_config_coerce() -> None:
    assert config.coerce("hotkey_press", "cmd+shift+d") == "cmd+shift+d"
    assert config.coerce("hotkey_hold", "ctrl+opt+d") == "ctrl+opt+d"
    assert "hotkey_press" in config.configurable()
    assert "hotkey_hold" in config.configurable()


def test_validate_accepts_real_press_and_hold_chords(tmp_path, monkeypatch) -> None:
    """End to end through the public entry point `stt config set` actually calls."""
    from stt_cli.live.hotkey import validate

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    validate("hotkey_press", "cmd+shift+d")  # must not raise
    validate("hotkey_hold", "ctrl+opt+d")  # must not raise


def test_validate_refuses_a_shift_only_chord_for_either_binding(tmp_path, monkeypatch) -> None:
    """A shift-only chord is refused for BOTH `hotkey_press` and `hotkey_hold` — an earlier
    version relaxed `hotkey_hold` to accept shift alone, and that relaxation was reverted
    (see hotkey.py's module docstring) once an independent review found `tap.py`'s
    listen-only tap means a sustained `shift+d` hold reliably TYPES "d" into the document
    being dictated into on every intentional use, not just on accidental collision."""
    from stt_cli._errors import SttError
    from stt_cli.live.hotkey import validate

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    with pytest.raises(SttError) as raised:
        validate("hotkey_press", "shift+d")
    assert "cmd or ctrl" in raised.value.why
    with pytest.raises(SttError) as raised:
        validate("hotkey_hold", "shift+d")
    assert "cmd or ctrl" in raised.value.why


def test_validate_refuses_a_bad_chord_with_a_clean_ssterror_not_a_bare_valueerror(
    tmp_path, monkeypatch
) -> None:
    """The bug a security review caught: `config.coerce` alone never calls `hotkey.parse`,
    so `stt config set hotkey_press a` used to succeed and store an unusable value —
    surfacing later as a raw, uncaught `HotkeyError` (a bare `ValueError`) the first time a
    daemon tried to start, instead of the clean refusal every other bad setting gets."""
    from stt_cli._errors import SttError
    from stt_cli.live.hotkey import validate

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    with pytest.raises(SttError):
        validate("hotkey_press", "a")
    with pytest.raises(SttError):
        validate("hotkey_hold", "a")


def test_validate_attributes_a_bad_error_to_the_stored_sibling_not_the_field_being_set(
    tmp_path, monkeypatch
) -> None:
    """Found by review: an earlier version resolved ``value`` and the sibling's stored
    value inside ONE shared `try`, so a `HotkeyError` raised by the sibling's already-
    stored, unrelated bad value (a hand-edited `config.json`) was blamed on whichever field
    was being SET — `stt config set hotkey_press cmd+shift+space` (a perfectly valid value)
    would have been refused with a message saying `hotkey_press is not a valid...` when the
    real problem was a broken `hotkey_hold` already sitting in the file. Each field's
    failure must be attributed to the field that actually caused it."""
    from stt_cli import config
    from stt_cli._errors import SttError
    from stt_cli.live.hotkey import validate

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    config.save_setting("hotkey_hold", "a")  # bypasses validate_extra — an invalid stored
    # value only a hand-edited config.json (or this direct write) could produce.

    with pytest.raises(SttError) as raised:
        validate("hotkey_press", "cmd+shift+space")  # value itself is perfectly valid
    assert "the stored hotkey_hold is invalid" in raised.value.what
    assert "hotkey_press is not a valid" not in raised.value.what

    # The symmetric case: a broken stored hotkey_press, a valid hotkey_hold being set.
    config.unset_setting("hotkey_hold")
    config.save_setting("hotkey_press", "a")
    with pytest.raises(SttError) as raised:
        validate("hotkey_hold", "ctrl+opt+d")
    assert "the stored hotkey_press is invalid" in raised.value.what
    assert "hotkey_hold is not a valid" not in raised.value.what


def test_validate_refuses_hotkey_press_and_hotkey_hold_sharing_a_key(tmp_path, monkeypatch) -> None:
    """A single-field check cannot see this on its own — `validate` reads the OTHER field's
    currently stored value to catch it. Refuses not just an identical chord but ANY shared
    keycode, even with different modifiers (`cmd+d` / `ctrl+d`): a key-release only reports
    which key was let go, not which modifiers were held, so `tap.Watcher` cannot tell two
    bindings sharing a key apart on release — see `hotkey.same_keycode`."""
    from stt_cli._errors import SttError
    from stt_cli.commands.config_cmd import _set
    from stt_cli.live.hotkey import validate

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    _set("hotkey_press", "cmd+shift+d")
    with pytest.raises(SttError, match="same key"):
        validate("hotkey_hold", "cmd+shift+d")  # identical chord
    with pytest.raises(SttError, match="same key"):
        validate("hotkey_hold", "shift+cmd+d")  # same keycode/modifiers, different spec order
    with pytest.raises(SttError, match="same key"):
        validate("hotkey_hold", "ctrl+d")  # same keycode, genuinely DIFFERENT modifiers


def test_setting_a_bad_hotkey_through_the_cli_is_refused_not_stored(tmp_path, monkeypatch) -> None:
    from stt_cli._errors import SttError
    from stt_cli.commands.config_cmd import _set

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    with pytest.raises(SttError):
        _set("hotkey_press", "a")
    assert config.load_settings().hotkey_press is None, "the bad value was never stored"


def test_setting_a_bad_hotkey_through_the_tui_is_refused_not_stored(tmp_path, monkeypatch) -> None:
    from stt_cli.commands._configtui import apply_edit

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    ok, message = apply_edit("hotkey_press", "shift+a")
    assert not ok
    assert message
    assert config.load_settings().hotkey_press is None, "the bad value was never stored"


# ── the daemon's own state machine (_HotkeyBus / edges), no fake hardware needed ─


async def test_hotkey_bus_marshals_combo_and_pressed_onto_the_loop_queue() -> None:
    from stt_cli.live.dictation import _HotkeyBus

    combo = parse("cmd+shift+space")
    bus = _HotkeyBus(asyncio.get_running_loop())
    bus(combo, True)
    bus(combo, False)
    assert await bus.events.get() == (combo, True)
    assert await bus.events.get() == (combo, False)


async def test_next_hotkey_edge_returns_none_when_shutdown_wins() -> None:
    from stt_cli.live.dictation import _HotkeyBus, _next_hotkey_edge

    bus = _HotkeyBus(asyncio.get_running_loop())
    shutdown = asyncio.Event()
    shutdown.set()
    assert await _next_hotkey_edge(bus, shutdown) is None


async def test_next_hotkey_edge_returns_the_edge() -> None:
    from stt_cli.live.dictation import _HotkeyBus, _next_hotkey_edge

    combo = parse("cmd+shift+space")
    bus = _HotkeyBus(asyncio.get_running_loop())
    bus(combo, True)
    assert await _next_hotkey_edge(bus, asyncio.Event()) == (combo, True)


async def test_shutdown_wins_an_edge_already_queued_when_it_fires() -> None:
    """A hotkey edge sitting in the queue when `stt mic stop`/Ctrl-C arrives must not start
    a burst: `asyncio.wait` can resolve both tasks in the same wakeup, and a version of this
    that checked the edge unconditionally opened the microphone right after the user had
    just asked the daemon to stop."""
    from stt_cli.live.dictation import _HotkeyBus, _next_hotkey_edge

    combo = parse("cmd+shift+space")
    bus = _HotkeyBus(asyncio.get_running_loop())
    bus(combo, True)
    shutdown = asyncio.Event()
    shutdown.set()
    assert await _next_hotkey_edge(bus, shutdown) is None


async def test_await_release_press_kind_ignores_release_ends_on_next_press_of_same_combo() -> None:
    from stt_cli.live.dictation import _await_release, _HotkeyBus

    combo = parse("cmd+shift+space")
    bus = _HotkeyBus(asyncio.get_running_loop())
    bus(combo, False)
    bus(combo, True)
    assert await asyncio.wait_for(_await_release(bus, combo, "press"), timeout=1) is True


async def test_await_release_hold_kind_ignores_a_stray_press_and_ends_on_release() -> None:
    from stt_cli.live.dictation import _await_release, _HotkeyBus

    combo = parse("cmd+shift+space")
    bus = _HotkeyBus(asyncio.get_running_loop())
    bus(combo, True)
    bus(combo, False)
    assert await asyncio.wait_for(_await_release(bus, combo, "hold"), timeout=1) is True


async def test_await_release_ignores_edges_from_the_other_configured_binding() -> None:
    """The other half of "the daemon correctly watches both bindings concurrently": a burst
    armed by ONE binding must not be ended (or confused) by the OTHER binding firing
    mid-burst — `_await_release` consumes and drops any edge that is not its own `armed`
    combo, leaving it out of the queue for whatever comes next rather than misfiring."""
    from stt_cli.live.dictation import _await_release, _HotkeyBus

    press = parse("cmd+shift+space")
    hold = parse("ctrl+opt+d")
    bus = _HotkeyBus(asyncio.get_running_loop())

    # A hold-armed burst is running; the press binding fires (start AND stop) mid-burst —
    # neither edge may end this hold burst.
    bus(press, True)
    bus(press, False)
    task = asyncio.create_task(_await_release(bus, hold, "hold"))
    await asyncio.sleep(0.02)
    assert not task.done(), "the other binding's edges must not end this burst"

    bus(hold, False)  # the actual release that DOES end it
    assert await asyncio.wait_for(task, timeout=1) is True


# ── the full idle -> burst -> idle -> shutdown cycle, with a fake tap and mic ─


class _FakeWatcher:
    """Stands in for `tap.Watcher`: no thread, no real CGEventTap — just remembers the
    callbacks so a test can fire them directly, the way a real hotkey press would."""

    def __init__(self, *, marker, on_key, hotkeys=(), on_hotkey=None) -> None:
        self.on_key = on_key
        self.on_hotkey = on_hotkey
        self.failure: str | None = None
        self.stopped = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


async def _burst_audio():
    """One real utterance, then an open mic that keeps streaming quiet forever — so the
    burst only ends when the daemon's own stop logic (the hotkey) says so, the same way a
    real held-open microphone would."""
    yield SPEECH
    yield SILENCE
    while True:
        yield tone(20, 5)
        await asyncio.sleep(0)


class _Answering:
    def __init__(self, text: str) -> None:
        self._text = text

    async def transcribe(self, pcm: bytes, *, prompt: str = "") -> str:
        del pcm, prompt
        return self._text


def _wire_fakes(monkeypatch):
    """Fakes the three things the daemon touches that need real hardware: the keyboard tap,
    the microphone, and typed output — so the idle/burst state machine can be driven and
    observed directly."""
    from stt_cli.live import dictation

    typed: list[str] = []
    monkeypatch.setattr(dictation.quartz, "type_text", lambda text, *, marker: typed.append(text))
    monkeypatch.setattr(dictation.quartz, "press_backspace", lambda times, *, marker: None)
    monkeypatch.setattr(dictation.status, "notifier_available", lambda: False)
    watchers: list[_FakeWatcher] = []

    def fake_watcher(*, marker, on_key, hotkeys=(), on_hotkey=None):
        watcher = _FakeWatcher(marker=marker, on_key=on_key, hotkeys=hotkeys, on_hotkey=on_hotkey)
        watchers.append(watcher)
        return watcher

    monkeypatch.setattr(dictation.tap, "Watcher", fake_watcher)

    @contextlib.asynccontextmanager
    async def fake_listening(device):
        yield _burst_audio()

    monkeypatch.setattr(dictation.capture, "listening", fake_listening)
    return typed, watchers


async def test_the_daemon_idles_then_dictates_on_the_press_binding_and_returns_to_idle(
    monkeypatch,
) -> None:
    from stt_cli.live import capture as capture_mod
    from stt_cli.live import dictation
    from stt_cli.live import hotkey as hotkey_mod

    typed, watchers = _wire_fakes(monkeypatch)
    models = dictation.Models(settled=_Answering("Привет!"), draft=None)
    settings = config.Settings(dictionary=False)
    device = capture_mod.Device(index=0, name="fake mic")
    press = hotkey_mod.parse("cmd+shift+space")
    args = argparse.Namespace(quiet=True, idle_minutes=None)

    task = asyncio.create_task(dictation._idle_loop(args, device, models, settings, press, None))
    await asyncio.sleep(0)
    assert watchers, "the daemon installs its keyboard watcher before idling"
    watcher = watchers[0]

    watcher.on_hotkey(press, True)  # first press: start dictating
    for _ in range(50):
        await asyncio.sleep(0)
        if typed:
            break
    assert typed == ["Привет!"], "the burst actually opened the mic and typed what it heard"

    watcher.on_hotkey(press, True)  # second press (press binding = toggle): stop this burst
    await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert watcher.stopped, "the tap is released when the daemon exits"


async def test_the_daemon_watches_both_bindings_concurrently(monkeypatch) -> None:
    """Ticket #79's central claim: `hotkey_press` and `hotkey_hold` are both live in the
    SAME daemon at once, not a mode switch on one chord. Runs a press-armed toggle burst,
    then — in the SAME running daemon, no restart — a hold-armed push-to-talk burst on a
    DIFFERENT chord, and checks both actually dictated."""
    from stt_cli.live import capture as capture_mod
    from stt_cli.live import dictation
    from stt_cli.live import hotkey as hotkey_mod

    typed, watchers = _wire_fakes(monkeypatch)
    models = dictation.Models(settled=_Answering("Привет!"), draft=None)
    settings = config.Settings(dictionary=False)
    device = capture_mod.Device(index=0, name="fake mic")
    press = hotkey_mod.parse("cmd+shift+space")
    hold = hotkey_mod.parse("ctrl+opt+d")
    args = argparse.Namespace(quiet=True, idle_minutes=None)

    task = asyncio.create_task(dictation._idle_loop(args, device, models, settings, press, hold))
    await asyncio.sleep(0)
    watcher = watchers[0]

    # The press binding: one press starts, a second (of the SAME chord) stops.
    watcher.on_hotkey(press, True)
    for _ in range(50):
        await asyncio.sleep(0)
        if typed:
            break
    assert typed == ["Привет!"]
    watcher.on_hotkey(press, True)
    await asyncio.sleep(0.05)

    # The hold binding, on a different chord entirely: press arms it, release ends it —
    # a second press of the SAME hold chord must NOT end it (push-to-talk, not toggle).
    watcher.on_hotkey(hold, True)
    for _ in range(50):
        await asyncio.sleep(0)
        if len(typed) == 2:
            break
    assert typed == ["Привет!", "Привет!"], "the hold binding dictated too, same daemon"
    watcher.on_hotkey(hold, True)  # a spurious second press mid-hold must not end it
    await asyncio.sleep(0.02)
    watcher.on_hotkey(hold, False)  # the actual release ends it
    await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert watcher.stopped


async def test_a_key_press_reaches_the_live_session_but_not_a_dead_one(
    monkeypatch,
) -> None:
    """`_watch_daemon.arrived` reads `active.stopper` on the tap thread and forwards a key
    press into it while a burst is open — the correction-interrupt this daemon exists to
    give. `_burst`'s `finally` clears it (via `_Active.close`) once the burst ends, so the
    SAME key pressed a moment later, idle, must be silently dropped instead of reaching a
    `Session` nothing is listening to anymore. Proves both directions: a key reaches the
    live session mid-burst (the positive control — a stubbed-out `arrived` that never calls
    `stopper` would fail here), and is dropped once `close()` has run (the negative control;
    `test_active_close_clears_session_and_stopper_together` isolates the `_Active` half of
    that guarantee directly)."""
    from stt_cli.live import capture as capture_mod
    from stt_cli.live import dictation
    from stt_cli.live import hotkey as hotkey_mod
    from stt_cli.live.session import Session

    typed, watchers = _wire_fakes(monkeypatch)
    interrupted: list[Session] = []
    monkeypatch.setattr(Session, "interrupt", lambda self: interrupted.append(self))
    models = dictation.Models(settled=_Answering("Привет!"), draft=None)
    settings = config.Settings(dictionary=False)
    device = capture_mod.Device(index=0, name="fake mic")
    press = hotkey_mod.parse("cmd+shift+space")
    args = argparse.Namespace(quiet=True, idle_minutes=None)

    task = asyncio.create_task(dictation._idle_loop(args, device, models, settings, press, None))
    await asyncio.sleep(0)
    watcher = watchers[0]

    watcher.on_hotkey(press, True)  # first press: start dictating
    for _ in range(50):
        await asyncio.sleep(0)
        if typed:
            break
    assert typed == ["Привет!"]

    watcher.on_key(0)  # mid-burst: must reach the live session
    assert len(interrupted) == 1, "a key pressed during the burst never reached the session"

    watcher.on_hotkey(press, True)  # second press (press binding = toggle): stop this burst
    await asyncio.sleep(0.05)

    watcher.on_key(0)  # a key pressed once idle again — must be dropped, not forwarded
    assert len(interrupted) == 1, "a key pressed after the burst ended reached a dead session"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_active_close_clears_session_and_stopper_together() -> None:
    """`_Active.open`/`close` exist to pair `session` and `stopper` so neither call site has
    to remember to touch two attributes by hand. Isolates that guarantee directly: deleting
    EITHER clear in `close()` fails one of the two asserts below."""
    from stt_cli.live.dictation import _Active

    active = _Active()
    assert active.session is None
    assert active.stopper is None

    stand_in = object()
    active.open(stand_in)  # type: ignore[arg-type]
    assert active.session is stand_in
    assert active.stopper is not None

    active.close()
    assert active.session is None, "close() left session set"
    assert active.stopper is None, "close() left stopper set"


def test_active_open_asserts_when_a_burst_is_already_active() -> None:
    """`open`'s assert is the documented one-burst-at-a-time guarantee, not just belt and
    braces `_idle_loop` happens to uphold by awaiting each burst to completion — pin it
    directly so a future caller that opens a second burst without closing the first fails
    loudly instead of silently overwriting the first burst's `session`/`stopper`."""
    from stt_cli.live.dictation import _Active

    active = _Active()
    active.open(object())  # type: ignore[arg-type]
    with pytest.raises(AssertionError):
        active.open(object())  # type: ignore[arg-type]


def test_active_session_and_stopper_cannot_be_assigned_directly() -> None:
    """The class docstring's third claim: `session`/`stopper` are read-only properties, so
    bypassing `open`/`close` isn't just a mypy error, it's a runtime `AttributeError` — pin
    that so a future change back to plain attributes fails here, not just under mypy."""
    from stt_cli.live.dictation import _Active

    active = _Active()
    with pytest.raises(AttributeError):
        active.session = None  # type: ignore[misc]
    with pytest.raises(AttributeError):
        active.stopper = None  # type: ignore[misc]


async def test_a_burst_that_fails_returns_to_idle_instead_of_ending_the_daemon(
    monkeypatch,
) -> None:
    """The daemon's own contract: only `stt mic stop`/Ctrl-C end the process. A burst that
    fails outright (the microphone disappearing, a decode error) must not take the whole
    daemon down with it — `stt mic enable`'s launchd unit would otherwise restart the whole
    process, reloading both models, for a failure that only needed to end one burst."""
    from stt_cli.live import capture as capture_mod
    from stt_cli.live import dictation
    from stt_cli.live import hotkey as hotkey_mod

    _typed, watchers = _wire_fakes(monkeypatch)

    attempts = 0

    @contextlib.asynccontextmanager
    async def failing_then_working_mic(device):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise EngineError(
                what="the microphone disappeared", why="unplugged", how="plug it back in"
            )
        yield _burst_audio()

    monkeypatch.setattr(dictation.capture, "listening", failing_then_working_mic)
    models = dictation.Models(settled=_Answering("Привет!"), draft=None)
    settings = config.Settings(dictionary=False)
    device = capture_mod.Device(index=0, name="fake mic")
    press = hotkey_mod.parse("cmd+shift+space")
    args = argparse.Namespace(quiet=True, idle_minutes=None)

    task = asyncio.create_task(dictation._idle_loop(args, device, models, settings, press, None))
    await asyncio.sleep(0)
    watcher = watchers[0]

    watcher.on_hotkey(press, True)  # first press: the mic fails to open
    for _ in range(50):
        await asyncio.sleep(0)
        if attempts >= 1 and not task.done():
            break
    assert not task.done(), "the daemon survived the failed burst — this is the whole point"

    watcher.on_hotkey(press, True)  # second press: a real burst, proving the daemon is alive
    for _ in range(50):
        await asyncio.sleep(0)
        if _typed:
            break
    assert _typed == ["Привет!"], "and it can still dictate normally afterward"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_a_release_in_hold_kind_ends_the_burst_a_second_press_does_not() -> None:
    """Distinguishes hold from press at the one place that matters: what ends a burst."""
    from stt_cli.live.dictation import _await_release, _HotkeyBus

    combo = parse("ctrl+opt+d")
    bus = _HotkeyBus(asyncio.get_running_loop())
    bus(combo, True)  # a second press while holding must NOT end a hold-kind burst
    task = asyncio.create_task(_await_release(bus, combo, "hold"))
    await asyncio.sleep(0.05)
    assert not task.done(), "hold is not over yet — only a release ends it"
    bus(combo, False)
    await asyncio.wait_for(task, timeout=1)
    assert task.done()


def test_ready_hint_lists_only_the_bindings_that_are_actually_configured() -> None:
    from stt_cli.live import hotkey as hotkey_mod
    from stt_cli.live.dictation import _ready_hint

    press = hotkey_mod.parse("cmd+shift+space")
    hold = hotkey_mod.parse("ctrl+opt+d")

    both = _ready_hint(press, hold)
    assert "cmd+shift+space" in both and "ctrl+opt+d" in both

    press_only = _ready_hint(press, None)
    assert "cmd+shift+space" in press_only and "ctrl+opt+d" not in press_only


def test_notification_absence_note_is_empty_when_available_and_explicit_when_not(
    monkeypatch,
) -> None:
    """The exact spot ticket #79 names for the daemon's startup banner: silent when the
    notification Stop button works, explicit (naming the install hint) when it does not."""
    from stt_cli.live import dictation, status

    monkeypatch.setattr(status, "notifier_available", lambda: True)
    assert dictation._notification_absence_note() == ""

    monkeypatch.setattr(status, "notifier_available", lambda: False)
    note = dictation._notification_absence_note()
    assert status.INSTALL_HINT in note


def test_off_hint_uses_the_shared_stop_hint_for_press_and_hold(monkeypatch) -> None:
    from stt_cli.live import hotkey as hotkey_mod
    from stt_cli.live import status
    from stt_cli.live.dictation import _off_hint

    monkeypatch.setattr(status, "notifier_available", lambda: True)
    press = hotkey_mod.parse("cmd+shift+space")
    hold = hotkey_mod.parse("ctrl+opt+d")
    assert (
        _off_hint("press", press)
        == "press cmd+shift+space again, or click Stop on the notification, to finish"
    )
    assert (
        _off_hint("hold", hold)
        == "release ctrl+opt+d, or click Stop on the notification, to finish"
    )


def test_refuse_colliding_hotkey_flags_catches_shared_keycode_before_enable_installs_anything(
    tmp_path, monkeypatch
) -> None:
    """Found by the pre-commit review gate: `--hotkey-press cmd+d --hotkey-hold ctrl+d` are
    each individually valid (their own `type=` callback only checks one field at a time),
    but sharing a keycode with different modifiers would leave a hold burst's microphone
    unable to end (see `hotkey.same_keycode`). `_lifecycle` calls this right after parsing,
    BEFORE `enable` can install a launchd `LaunchAgent` for a daemon doomed to crash-loop.
    `STT_HOME` is isolated to an empty `tmp_path` because the fix for the enable-time gap
    below means this now reads stored config even for the single-flag cases."""
    import argparse

    from stt_cli._errors import UsageError
    from stt_cli.commands.mic import _refuse_colliding_hotkey_flags

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    with pytest.raises(UsageError, match="same key"):
        _refuse_colliding_hotkey_flags(
            argparse.Namespace(hotkey_press="cmd+d", hotkey_hold="ctrl+d", action="enable")
        )
    # Distinct keys must never be refused.
    _refuse_colliding_hotkey_flags(
        argparse.Namespace(
            hotkey_press="cmd+shift+space", hotkey_hold="ctrl+opt+d", action="enable"
        )
    )
    # Neither flag given, and an action that does not install/detach a daemon: a no-op —
    # `status`/`stop`/`disable` have no reason to care whether a hotkey is configured.
    _refuse_colliding_hotkey_flags(
        argparse.Namespace(hotkey_press=None, hotkey_hold=None, action="status")
    )


def test_refuse_colliding_hotkey_flags_resolves_the_unspecified_side_from_stored_config(
    tmp_path, monkeypatch
) -> None:
    """The enable-time cross-validation gap: an earlier version returned early whenever
    EITHER flag was `None`, so `stt mic enable --hotkey-hold cmd+shift+space` (leaving
    `--hotkey-press` unset) silently collided with the built-in `hotkey_press` default —
    installing a `LaunchAgent` that crash-loops forever, diagnosable only after the fact.
    The fix resolves the unspecified side from stored config/the built-in default before
    comparing, mirroring `hotkey.validate`'s own `config.load_settings()` pattern."""
    import argparse

    from stt_cli._errors import UsageError
    from stt_cli.commands.mic import _refuse_colliding_hotkey_flags

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    # hotkey_press left unset (nothing stored either) resolves to the built-in default
    # cmd+shift+space — a --hotkey-hold that reuses that exact key must be refused.
    with pytest.raises(UsageError, match="same key"):
        _refuse_colliding_hotkey_flags(
            argparse.Namespace(hotkey_press=None, hotkey_hold="cmd+shift+space", action="enable")
        )
    # The symmetric case: hotkey_hold left unset but stored in config.json, and a
    # --hotkey-press given on this invocation reuses its key.
    from stt_cli.commands.config_cmd import _set

    _set("hotkey_hold", "ctrl+opt+d")
    with pytest.raises(UsageError, match="same key"):
        _refuse_colliding_hotkey_flags(
            argparse.Namespace(hotkey_press="ctrl+opt+d", hotkey_hold=None, action="enable")
        )
    # A single flag with no collision on either the default or the stored side must still
    # be accepted — the fix must not become a false positive for the ordinary case.
    _refuse_colliding_hotkey_flags(
        argparse.Namespace(hotkey_press="cmd+shift+space", hotkey_hold=None, action="enable")
    )


def test_refuse_colliding_hotkey_flags_catches_both_bindings_disabled_on_enable(
    tmp_path, monkeypatch
) -> None:
    """Found by review: an earlier version of the enable-time guard checked collisions
    only, so `stt config set hotkey_press none` (disabling the default, `hotkey_hold` never
    set) followed by a BARE `stt mic enable` — no flags at all — used to return immediately
    from the "neither flag given" early-out before ever resolving stored config, silently
    installing a `LaunchAgent` for a daemon with nothing that could ever open the
    microphone: an immediate, guaranteed crash loop on every restart (`KeepAlive`), the
    exact failure class ticket #54 was filed from. `dictation._resolved_hotkeys` already
    refused this identical state at daemon-start time — this is the same refusal, moved
    earlier, before anything is installed."""
    import argparse

    from stt_cli._errors import UsageError
    from stt_cli.commands.config_cmd import _set
    from stt_cli.commands.mic import _refuse_colliding_hotkey_flags

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    _set("hotkey_press", "none")

    # A bare `stt mic enable` with NO flags at all — the exact repro.
    with pytest.raises(UsageError, match="no hotkey is configured"):
        _refuse_colliding_hotkey_flags(
            argparse.Namespace(hotkey_press=None, hotkey_hold=None, action="enable")
        )
    # `start` gets the identical refusal — it installs/detaches a daemon too.
    with pytest.raises(UsageError, match="no hotkey is configured"):
        _refuse_colliding_hotkey_flags(
            argparse.Namespace(hotkey_press=None, hotkey_hold=None, action="start")
        )
    # `status`/`stop`/`disable` must NOT be refused for the same stored state — they have
    # no reason to care whether a hotkey is configured at all.
    for action in ("status", "stop", "disable"):
        _refuse_colliding_hotkey_flags(
            argparse.Namespace(hotkey_press=None, hotkey_hold=None, action=action)
        )
    # An explicit --hotkey-press on `enable` rescues it, same stored hotkey_hold=disabled.
    _refuse_colliding_hotkey_flags(
        argparse.Namespace(hotkey_press="cmd+shift+space", hotkey_hold=None, action="enable")
    )


# ── mic.py CLI plumbing ───────────────────────────────────────────────────────


def test_daemon_flags_round_trip_into_the_serve_argv() -> None:
    from stt_cli.commands.mic import _daemon_extra_argv, _daemon_parser

    ns = _daemon_parser().parse_args(
        [
            "--hotkey-press",
            "cmd+shift+d",
            "--hotkey-hold",
            "ctrl+opt+d",
            "--model",
            "small",
            "--quiet",
        ]
    )
    argv = _daemon_extra_argv(ns)
    assert argv == [
        "--model",
        "small",
        "--draft-model",
        "base",
        "--hotkey-press",
        "cmd+shift+d",
        "--hotkey-hold",
        "ctrl+opt+d",
        "--quiet",
    ]


def test_an_invalid_hotkey_press_flag_is_refused_at_parse_time_not_a_crash(capsys) -> None:
    """`stt mic enable --hotkey-press d` bakes its argv into a launchd LaunchAgent with
    KeepAlive — a spec that only failed once the daemon actually started would crash-loop
    it, reloading both models on every restart. argparse's own `type=` rejects it before
    any of that (a clean `SystemExit(2)`, not a traceback)."""
    from stt_cli.commands.mic import _daemon_parser

    with pytest.raises(SystemExit) as raised:
        _daemon_parser().parse_args(["--hotkey-press", "d"])
    assert raised.value.code == 2
    assert "modifier" in capsys.readouterr().err


def test_an_invalid_hotkey_hold_flag_is_refused_at_parse_time_same_as_press(
    capsys,
) -> None:
    """`--hotkey-hold` is validated through `resolve_hold`, not `resolve_press`, but the two
    now share the same strong-modifier rule (cmd or ctrl) after the shift-for-hold
    relaxation was reverted — a shift-only chord trips the same argparse-time refusal for
    either flag."""
    from stt_cli.commands.mic import _daemon_parser

    with pytest.raises(SystemExit) as raised:
        _daemon_parser().parse_args(["--hotkey-hold", "shift+d"])
    assert raised.value.code == 2
    assert "cmd or ctrl" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        _daemon_parser().parse_args(["--hotkey-hold", "opt+d"])
    assert raised.value.code == 2
    assert "cmd or ctrl" in capsys.readouterr().err


def test_a_hotkey_thats_invalid_only_in_stored_config_is_a_diagnosed_refusal() -> None:
    """argparse only guards `--hotkey-press`/`--hotkey-hold`; a hand-edited config.json can
    still store a bad value, and that path must fail the same clean way — a raw
    `HotkeyError` is a `ValueError` subclass `guard()` does not catch, which would surface
    as an unhandled traceback."""
    from stt_cli import config
    from stt_cli._errors import UsageError
    from stt_cli.live.dictation import _resolved_hotkeys

    with pytest.raises(UsageError, match="hotkey"):
        _resolved_hotkeys(config.Settings(hotkey_press="d"))


def test_a_stored_hotkey_press_and_hold_that_share_a_key_is_a_diagnosed_refusal() -> None:
    """The same-key collision `hotkey.validate` refuses at set-time must be refused AGAIN
    here for a hand-edited `config.json` that reached that state anyway — this is the exact
    bug an independent review found before this shipped: `cmd+d`/`ctrl+d` pass `same_chord`
    (different modifiers) but share a keycode, and `tap.Watcher`'s release handler cannot
    tell them apart, leaving a hold burst's microphone open forever."""
    from stt_cli import config
    from stt_cli._errors import UsageError
    from stt_cli.live.dictation import _resolved_hotkeys

    with pytest.raises(UsageError, match="same key"):
        _resolved_hotkeys(config.Settings(hotkey_press="cmd+shift+d", hotkey_hold="shift+cmd+d"))
    with pytest.raises(UsageError, match="same key"):
        _resolved_hotkeys(config.Settings(hotkey_press="cmd+d", hotkey_hold="ctrl+d"))


def test_both_hotkeys_disabled_is_a_diagnosed_refusal_not_a_silent_dead_daemon() -> None:
    from stt_cli import config
    from stt_cli._errors import UsageError
    from stt_cli.live.dictation import _resolved_hotkeys

    with pytest.raises(UsageError, match="no hotkey"):
        _resolved_hotkeys(config.Settings(hotkey_press="none", hotkey_hold="none"))


def test_reordered_for_argparse_moves_the_lifecycle_word_to_the_end() -> None:
    from stt_cli.commands.mic import _reordered_for_argparse

    assert _reordered_for_argparse(["start", "--hotkey-press", "cmd+shift+x"]) == [
        "--hotkey-press",
        "cmd+shift+x",
        "start",
    ]
    assert _reordered_for_argparse(["status"]) == ["status"]
    assert _reordered_for_argparse([]) == []


def test_daemon_flags_after_the_lifecycle_word_actually_parse() -> None:
    """The bug a security review caught: `stt mic start --hotkey-press X` — the only order
    this file's own help text and the README show — used to be handed to an empty per-action
    subparser (built by `add_service_subcommands`, which defines no flags of its own) and
    rejected as unrecognized. Reproduces the real parser shape (a parent with `_daemon_parser`'s
    flags, subparsers with none) without needing `agenttools_service` installed."""

    from stt_cli.commands.mic import _daemon_parser, _reordered_for_argparse

    parser = _daemon_parser()
    subs = parser.add_subparsers(dest="action")
    for name in ("run", "start", "status", "stop", "enable", "disable"):
        subs.add_parser(name)

    ns = parser.parse_args(
        _reordered_for_argparse(["start", "--hotkey-press", "cmd+shift+x", "--quiet"])
    )
    assert ns.action == "start"
    assert ns.hotkey_press == "cmd+shift+x"
    assert ns.quiet is True

    with pytest.raises(SystemExit):
        # Without the reorder this would ALSO raise SystemExit, but for the wrong reason
        # (argparse's own "unrecognized arguments" on the un-reordered argv) — assert against
        # the un-reordered call specifically so a future accidental removal of the reorder
        # is caught by the FIRST assertion above failing, not silently passing this one too.
        parser.parse_args(["start", "--hotkey-press", "cmd+shift+x"])


def test_a_bare_lifecycle_word_without_the_shared_lib_names_the_install_fix(
    capsys, monkeypatch
) -> None:
    """Forces the missing-lib path deterministically — asserting on it without this would
    silently pass for the wrong reason on any machine that installed the lib per the README,
    and silently break the moment this repo's own dev environment gains it too."""
    import sys

    from stt_cli.commands.mic import _lifecycle

    monkeypatch.setitem(sys.modules, "agenttools_service", None)
    exit_code = _lifecycle(["status"])
    assert exit_code == 127
    assert "agenttools_service" in capsys.readouterr().err


def test_run_dispatches_the_hidden_serve_token_to_the_daemon(monkeypatch) -> None:
    """`__serve` has to be checked before ordinary argv parsing, or a background daemon
    could never reach `_serve` once `agenttools_service` is installed and starts routing
    through `_lifecycle` instead."""
    from stt_cli.commands import mic
    from stt_cli.live import dictation

    called: list[object] = []

    async def fake_daemon(args: object) -> None:
        called.append(args)

    monkeypatch.setattr(dictation, "dictate_daemon", fake_daemon)
    exit_code = mic.run(["__serve", "--hotkey-press", "cmd+shift+d"])
    assert exit_code == 0
    assert len(called) == 1
    assert called[0].hotkey_press == "cmd+shift+d"  # type: ignore[attr-defined]


def test_mic_service_serve_argv_points_at_bin_stt_in_a_source_checkout() -> None:
    """In THIS repo — a source checkout, where `stt_cli` is only importable via `bin/stt`'s
    own `sys.path` insertion — the persisted daemon argv must re-invoke `bin/stt` itself,
    not `-m stt_cli.cli`: the bare module form only worked when `enable` ran interactively
    (its `-m` implicitly added the repo-root `cwd` to `sys.path`) and silently
    `ModuleNotFoundError`'d the moment launchd re-exec'd the identical argv later, from its
    own cwd-less context — while `status`/`launchctl` kept reporting "running" because
    nothing about enabling the LaunchAgent itself had failed (launchd-autostart bug)."""
    import sys
    from pathlib import Path

    from stt_cli.live.mic_service import SERVE_TOKEN, _serve_argv

    argv = _serve_argv(["--hotkey-press", "cmd+shift+d"])
    repo_root = Path(__file__).resolve().parent.parent
    assert argv == [
        sys.executable,
        str(repo_root / "bin" / "stt"),
        "mic",
        SERVE_TOKEN,
        "--hotkey-press",
        "cmd+shift+d",
    ]


def test_mic_service_serve_argv_falls_back_to_the_module_form_for_a_real_install(
    monkeypatch,
) -> None:
    """Outside a source checkout (`uv tool install` / `pipx install` / `pip install`),
    `stt_cli` is properly installed onto `sys.executable`'s own site-packages, so there is
    no `bin/stt` to re-point at — the bare `-m stt_cli.cli` form is already self-sufficient
    there, and must stay the argv."""
    import sys

    from stt_cli.live import mic_service

    monkeypatch.setattr(mic_service, "_dev_wrapper", lambda: None)
    argv = mic_service._serve_argv(["--hotkey-press", "cmd+shift+d"])
    assert argv == [
        sys.executable,
        "-m",
        "stt_cli.cli",
        "mic",
        mic_service.SERVE_TOKEN,
        "--hotkey-press",
        "cmd+shift+d",
    ]


def test_dev_wrapper_resolves_via_module_file_not_the_callers_cwd(tmp_path, monkeypatch) -> None:
    """`_dev_wrapper` has to answer the same regardless of the CALLER's cwd — launchd
    starts the re-exec'd process with none of the interactive shell's — so it must be
    pinned to `__file__`, not an accidentally-correct `cwd`."""
    from stt_cli.live.mic_service import _dev_wrapper

    monkeypatch.chdir(tmp_path)
    wrapper = _dev_wrapper()
    assert wrapper is not None
    assert wrapper.name == "stt"
    assert wrapper.parent.name == "bin"


# ── ticket #54: `stt mic status` distinguishing alive from crash-looping ────────────────


def test_probe_launchd_reports_dead_for_a_crash_looping_job() -> None:
    """The exact machine state #54 was filed from: `launchctl print` shows the job
    registered and retrying (`state = spawn scheduled`, a real `runs`/`last exit code`) but
    prints no top-level `pid = ` line, because nothing is alive between restarts."""
    import subprocess

    from stt_cli.live import mic_service

    output = (
        "gui/501/com.agenttools.stt.mic = {\n"
        "\tactive count = 0\n"
        "\ttype = LaunchAgent\n"
        "\tstate = spawn scheduled\n"
        "\tdomain = gui/501 [100029]\n"
        "\truns = 6603\n"
        "\tlast exit code = 8\n"
        "}\n"
    )

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    probe = mic_service.probe_launchd("com.agenttools.stt.mic", runner=fake_runner)
    assert probe == mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=8)


def test_probe_launchd_reports_alive_with_the_real_pid() -> None:
    """A genuinely running job prints a top-level `pid = ` line — that is the one fact
    `ServiceManager.status()` cannot see for an OS-managed service, and this is the OS tool
    that can."""
    import subprocess

    from stt_cli.live import mic_service

    output = (
        "gui/501/com.agenttools.stt.mic = {\n"
        "\tstate = running\n"
        "\tpid = 4242\n"
        "\tlast exit code = (never exited)\n"
        "}\n"
    )

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    probe = mic_service.probe_launchd("com.agenttools.stt.mic", runner=fake_runner)
    assert probe == mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None)


def test_probe_launchd_is_none_when_launchctl_has_nothing_registered() -> None:
    """Not enabled, or a domain launchctl itself cannot reach — `launchctl print` exits
    nonzero with no useful stdout. The caller must fall back to the shared lib's own read
    rather than treating "no evidence" as "confirmed dead"."""
    import subprocess

    from stt_cli.live import mic_service

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 113, stdout="", stderr="Bad request.\n")

    assert mic_service.probe_launchd("com.agenttools.stt.nonexistent", runner=fake_runner) is None


def test_probe_launchd_is_none_for_output_with_no_recognizable_state_line() -> None:
    """`launchctl print` is Apple's own undocumented debug format, no stability guarantee
    across macOS versions. Exit 0 with output that doesn't even have a top-level `state =
    ` line must NOT be read as "confirmed no pid" — that would silently misreport a
    healthy daemon as dead the moment the format ever changes. It has to fall back to the
    shared lib's own read instead, the same as launchctl failing outright."""
    import subprocess

    from stt_cli.live import mic_service

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="something unrecognized\n", stderr="")

    assert mic_service.probe_launchd("com.agenttools.stt.mic", runner=fake_runner) is None


def test_probe_launchd_is_none_off_macos(monkeypatch) -> None:
    from stt_cli.live import mic_service

    monkeypatch.setattr(mic_service.sys, "platform", "linux")
    assert mic_service.probe_launchd("com.agenttools.stt.mic") is None


def _fake_service_lib(monkeypatch, *, run_action, dispatch=None, add_service_subcommands=None):
    """A minimal `agenttools_service` stand-in. `run_action` is what `_status` reads;
    `dispatch`/`add_service_subcommands` are only needed by tests that drive `_lifecycle`
    through an action OTHER than `status` (`_manager` itself is monkeypatched separately in
    each test, so nothing here needs to build a real `Service`/`ServiceManager`)."""
    import sys
    import types

    fake = types.ModuleType("agenttools_service")
    fake.run_action = run_action
    if dispatch is not None:
        fake.dispatch = dispatch
    if add_service_subcommands is not None:
        fake.add_service_subcommands = add_service_subcommands
    monkeypatch.setitem(sys.modules, "agenttools_service", fake)


def _fake_add_subcommands(subs, *, manager_factory, service_name):
    """A minimal `add_service_subcommands` stand-in: just enough subparsers for
    `_daemon_parser().parse_args` to set `ns.action` correctly — the real routing/dispatch
    is faked separately via `_fake_service_lib`'s `dispatch`."""
    for name in ("run", "start", "status", "stop", "enable", "disable"):
        subs.add_parser(name)


def test_status_falls_through_to_the_shared_lib_for_a_pidfile_tracked_instance(
    monkeypatch,
) -> None:
    """Any state other than `"autostart"` (a real pidfile-tracked instance, a genuinely
    stopped one, a foreign listener) is already reported correctly by the shared lib —
    `_status` must not second-guess those, only the one gap #54 names."""
    from stt_cli.commands import mic

    class _FakeManager:
        def status(self) -> object:
            return argparse.Namespace(state="running")

    calls: list[str] = []
    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    _fake_service_lib(monkeypatch, run_action=lambda manager, action: calls.append(action) or 0)

    assert mic._status(argparse.Namespace()) == 0
    assert calls == ["status"]


def test_status_falls_through_when_launchctl_itself_is_unreachable(monkeypatch) -> None:
    """`state == "autostart"` but `probe_launchd` returns `None` (off macOS, or launchctl
    itself failed): no evidence either way, so the shared lib's own read still applies —
    this is the one case `test_probe_launchd_is_none_*` cannot exercise on its own, since
    it never reaches `_status`'s own fall-through decision."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(state="autostart")

    calls: list[str] = []
    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(mic_service, "probe_launchd", lambda label: None)
    _fake_service_lib(monkeypatch, run_action=lambda manager, action: calls.append(action) or 0)

    assert mic._status(argparse.Namespace()) == 0
    assert calls == ["status"]


def test_status_prints_the_pid_launchctl_found_when_genuinely_alive(monkeypatch, capsys) -> None:
    """`state == "autostart"` but `probe_launchd` finds a live pid: genuinely healthy.
    `ServiceManager.status()` has no pid to offer here (an OS-managed instance has no
    pidfile) — launchctl's answer is strictly better, so `_status` prints it directly
    rather than discarding it through a `run_action` call that would omit it."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(state="autostart")

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=999, last_exit_code=None),
    )
    # `_status`'s top-of-function `from agenttools_service import run_action` runs
    # unconditionally even though this alive=True path never calls it - every sibling
    # `_status` test below stubs the module for exactly that reason; this one was missing
    # it, so it only passed on a machine that happened to already have the real (editable,
    # this-machine-only) package installed, not on a clean env like CI's.
    _fake_service_lib(monkeypatch, run_action=lambda manager, action: 0)

    exit_code = mic._status(argparse.Namespace())
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "running" in out
    assert "999" in out


def test_status_names_the_crash_loop_from_the_daemons_own_exit_code(monkeypatch, capsys) -> None:
    """The exact #54 scenario: registered/enabled, launchd has no live process, and the
    daemon's own last exit code is `EXIT_PERMISSION` — the only failure anywhere
    `dictate_daemon` can reach that produces it (`require_accessibility`). Must say so,
    not print "running", and must NOT depend on `quartz.trusted()` of the STATUS
    process — an earlier version of this fix read that instead, which is a DIFFERENT TCC
    principal than the launchd-spawned daemon: a terminal already granted Accessibility
    (the common case — ad-hoc `stt mic` already worked, which is why `enable` ran at all)
    made that check answer for the wrong process and silently skip this exact diagnosis."""
    from stt_cli._errors import EXIT_PERMISSION
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic", logfile="/fake/mic.log")

        def status(self) -> object:
            return argparse.Namespace(state="autostart")

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=8),
    )
    _fake_service_lib(monkeypatch, run_action=lambda manager, action: 0)

    exit_code = mic._status(argparse.Namespace())
    out = capsys.readouterr().out
    assert exit_code == EXIT_PERMISSION
    assert "not running" in out
    assert "Accessibility" in out
    assert "running" not in out.split("not running")[0]  # never a bare "running" prefix


def test_status_names_down_between_restarts_for_an_undiagnosed_crash(monkeypatch, capsys) -> None:
    """Registered/enabled, launchd has no live process, but the last exit code is NOT
    `EXIT_PERMISSION` — an undiagnosed crash (missing whisper-server, a bad stored
    hotkey, ...). Must still say "not running", must NOT claim Accessibility (that would
    be a wrong diagnosis for a different failure), and must point at the log."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic", logfile="/fake/mic.log")

        def status(self) -> object:
            return argparse.Namespace(state="autostart")

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=127),
    )
    _fake_service_lib(monkeypatch, run_action=lambda manager, action: 0)

    exit_code = mic._status(argparse.Namespace())
    out = capsys.readouterr().out
    assert exit_code == mic._EXIT_NOT_RUNNING
    assert "not running" in out
    assert "Accessibility" not in out
    assert "/fake/mic.log" in out


# ── bare invocation: a literal alias for `stt mic --help` (#85) ─────────────────


def test_a_bare_invocation_is_a_literal_alias_for_help(capsys) -> None:
    """`stt mic` with zero arguments must produce EXACTLY the same output and exit code as
    `stt mic --help` — argparse's own, unchanged mechanism, not a custom screen replacing
    it (#85)."""
    from stt_cli.commands import mic

    with pytest.raises(SystemExit) as bare:
        mic.run([])
    bare_out = capsys.readouterr()

    with pytest.raises(SystemExit) as explicit:
        mic.run(["--help"])
    explicit_out = capsys.readouterr()

    assert bare.value.code == explicit.value.code == 0
    assert bare_out.out == explicit_out.out
    assert "run/start/status/stop/enable/disable" in bare_out.out


def test_an_explicit_flag_still_dictates_ad_hoc_exactly_as_before(monkeypatch) -> None:
    """Only the truly bare invocation changes meaning — any flag at all (`--device 1`,
    `--check`, ...) must still reach dictation exactly as it always has (#85)."""
    from stt_cli.commands import mic
    from stt_cli.live import dictation as dictation_mod

    called: list[object] = []

    async def fake_dictate(args):
        called.append(args)
        return 0

    monkeypatch.setattr(mic, "_adhoc_banner", lambda **_: None)
    monkeypatch.setattr(mic, "_maybe_autostart_daemon", lambda **_: None)
    monkeypatch.setattr(dictation_mod, "dictate", fake_dictate)

    assert mic.run(["--device", "1"]) == 0
    assert len(called) == 1
    assert called[0].device == "1"


# ── ad-hoc (non-daemon) dictation: the daemon-status/hotkey banner (#85) ────────


def test_ad_hoc_dictation_prints_the_check_banner_before_opening_the_microphone(
    monkeypatch, capsys
) -> None:
    """The daemon-status/hotkey screen the prior review caught wired to the WRONG trigger
    (bare invocation) belongs here instead: right before an ad-hoc, non-daemon dictation
    opens the microphone — Alex: "при запуске не в виде демона нужно запускать
    проверочное окно" (#85)."""
    from stt_cli.commands import mic
    from stt_cli.live import dictation as dictation_mod

    monkeypatch.setattr(mic, "_daemon_status_line", lambda: "daemon:  not running")
    monkeypatch.setattr(mic, "_hotkey_lines", lambda: "hotkey:  cmd+shift+space (toggle)")
    monkeypatch.setattr(mic, "_maybe_autostart_daemon", lambda **_: None)

    async def fake_dictate(args):
        return 0

    monkeypatch.setattr(dictation_mod, "dictate", fake_dictate)

    assert mic.run(["--device", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "", "the banner belongs on stderr, not mixed into stdout"
    assert "daemon:  not running" in captured.err
    assert "hotkey:  cmd+shift+space (toggle)" in captured.err


def test_check_flag_also_prints_the_ad_hoc_banner_but_never_triggers_autostart(
    monkeypatch, capsys
) -> None:
    """`--check` opens the microphone exactly like ad-hoc dictation does — the SAME banner
    (#85), printed before `_check` runs, same as the plain-dictation path above. But
    `--check` is a DIAGNOSTIC, not a real dictation: `run()` only calls
    `_maybe_autostart_daemon` after `dictation.dictate` itself returns `EXIT_OK` — `--check`
    returns through `_check` instead and must never reach that trigger, unlike a genuine
    dictation completing (#87). Required coverage: the diff adds the `--check` banner path
    with no test proving it independently of a real dictation."""
    from stt_cli.commands import mic

    monkeypatch.setattr(mic, "_daemon_status_line", lambda: "daemon:  not running")
    monkeypatch.setattr(mic, "_hotkey_lines", lambda: "hotkey:  cmd+shift+space (toggle)")

    def boom(**_: object) -> None:
        raise AssertionError("--check must never trigger the autostart decision")

    monkeypatch.setattr(mic, "_maybe_autostart_daemon", boom)

    async def fake_check(args: object) -> int:
        return 0

    monkeypatch.setattr(mic, "_check", fake_check)

    assert mic.run(["--check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "", "the banner belongs on stderr, not mixed into stdout"
    assert "daemon:  not running" in captured.err
    assert "hotkey:  cmd+shift+space (toggle)" in captured.err


def test_quiet_suppresses_the_ad_hoc_check_banner(monkeypatch, capsys) -> None:
    from stt_cli.commands import mic
    from stt_cli.live import dictation as dictation_mod

    def boom() -> str:
        raise AssertionError("--quiet must skip the daemon probe entirely")

    monkeypatch.setattr(mic, "_daemon_status_line", boom)
    monkeypatch.setattr(mic, "_maybe_autostart_daemon", lambda **_: None)

    async def fake_dictate(args):
        return 0

    monkeypatch.setattr(dictation_mod, "dictate", fake_dictate)

    assert mic.run(["--device", "1", "--quiet"]) == 0
    assert capsys.readouterr().err == ""


def test_list_devices_never_shows_the_ad_hoc_banner(monkeypatch, capsys) -> None:
    """`--list-devices` never opens the microphone — a pure query, not a "launched not as
    the daemon" dictation — so it skips the banner entirely (#85)."""
    from stt_cli.commands import mic

    def boom() -> str:
        raise AssertionError("--list-devices must never touch the daemon probe")

    monkeypatch.setattr(mic, "_daemon_status_line", boom)

    async def fake_list_devices() -> int:
        return 0

    monkeypatch.setattr(mic, "_list_devices", fake_list_devices)

    assert mic.run(["--list-devices"]) == 0
    assert capsys.readouterr().err == ""


def test_daemon_status_line_reports_not_running(monkeypatch) -> None:
    from stt_cli.commands import mic

    class _FakeManager:
        def status(self) -> object:
            return argparse.Namespace(state="stopped", running=False, pid=None)

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    assert mic._daemon_status_line() == "daemon:  not running"


def test_daemon_status_line_reports_a_live_pidfile_tracked_instance(monkeypatch) -> None:
    from stt_cli.commands import mic

    class _FakeManager:
        def status(self) -> object:
            return argparse.Namespace(state="running", running=True, pid=4242)

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    assert mic._daemon_status_line() == "daemon:  running (pid 4242)"


def test_daemon_status_line_names_a_crash_loop_via_launchd(monkeypatch) -> None:
    """The same real-state distinction #54 gave `stt mic status` (a genuinely alive daemon
    versus a launchd job registered but currently down between restarts) has to reach the
    ad-hoc dictation banner too, not just `stt mic status` itself."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(state="autostart", running=True, pid=None)

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=8),
    )
    line = mic._daemon_status_line()
    assert "not currently running" in line
    assert "last exit code 8" in line


def test_daemon_status_line_says_unavailable_without_the_shared_lib(monkeypatch) -> None:
    import sys

    from stt_cli.commands import mic

    monkeypatch.setitem(sys.modules, "agenttools_service", None)
    assert "not available" in mic._daemon_status_line()


def test_daemon_status_line_reports_a_live_autostart_managed_instance(monkeypatch) -> None:
    """The branch every already-enabled machine actually hits day to day — a live process
    reported through the launchd probe, not the pidfile fall-through — had no coverage."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(state="autostart", running=True, pid=None)

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=777, last_exit_code=None),
    )
    assert mic._daemon_status_line() == "daemon:  running (pid 777) [autostart on]"


def test_daemon_status_line_degrades_on_an_unexpected_probe_failure(monkeypatch) -> None:
    """`_adhoc_banner` runs ahead of every ad-hoc dictation — a launchctl subprocess error
    or a malformed pidfile must degrade to a plain line here, not crash the banner (or the
    dictation behind it) the way an `except ImportError`-only guard would. Caught in review
    before merge; this is the regression test."""
    from stt_cli.commands import mic

    class _FakeManager:
        def status(self) -> object:
            raise OSError("launchctl: no such process")

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    line = mic._daemon_status_line()
    assert "status unavailable" in line
    assert "stt mic status" in line


def test_hotkey_lines_shows_the_configured_chord_and_how_to_change_it(
    tmp_path, monkeypatch
) -> None:
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    config.save_setting("hotkey_press", "ctrl+opt+d")
    config.save_setting("hotkey_hold", "cmd+shift+d")

    lines = mic._hotkey_lines()
    assert "ctrl+opt+d" in lines
    assert "toggle" in lines
    assert "cmd+shift+d" in lines
    assert "hold" in lines
    assert "configured" in lines
    assert "stt config set hotkey_press" in lines
    assert "stt config set hotkey_hold" in lines


def test_hotkey_lines_names_the_built_in_default_when_unconfigured(tmp_path, monkeypatch) -> None:
    """`hotkey_press` falls back to the built-in default when unset; `hotkey_hold` has no
    built-in default at all (see its own doc comment) — unset means disabled, not "use
    some default hold chord"."""
    from stt_cli.commands import mic
    from stt_cli.live import hotkey as hotkey_mod

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    lines = mic._hotkey_lines()
    assert hotkey_mod.DEFAULT_PRESS_HOTKEY in lines
    assert "not yet configured" in lines
    assert "hold disabled" in lines


def test_hotkey_lines_degrades_on_a_hand_edited_unusable_chord(tmp_path, monkeypatch) -> None:
    """A hand-edited config.json can hold a syntactically-fine (type: str) but semantically
    unusable chord — `config._validate` only checks the TYPE, `hotkey.validate` only runs at
    `stt config set`/TUI time. `HotkeyError` is a `ValueError`, not the `SttError` the CLI's
    top-level `guard` recognises — left uncaught this would raw-traceback the bare-invocation
    screen, the one path meant to be safe to run blind. Caught in review before merge; this
    is the regression test."""
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    config.save_setting("hotkey_press", "banana")  # bypasses stt config set's own validation

    lines = mic._hotkey_lines()
    assert "invalid stored hotkey_press" in lines
    assert "stt config set hotkey_press" in lines


# ── daemon default-on after the first successful ad-hoc dictation (#87) ─────────


def test_run_triggers_the_autostart_check_only_after_a_successful_dictation(
    tmp_path, monkeypatch
) -> None:
    from stt_cli.commands import mic
    from stt_cli.live import dictation as dictation_mod

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    monkeypatch.setattr(mic, "_adhoc_banner", lambda **_: None)

    calls: list[int] = []
    monkeypatch.setattr(mic, "_maybe_autostart_daemon", lambda **_: calls.append(1))

    async def ok(args):
        return 0

    monkeypatch.setattr(dictation_mod, "dictate", ok)
    assert mic.run(["--device", "1"]) == 0
    assert calls == [1]

    calls.clear()

    async def failed(args):
        return 8

    monkeypatch.setattr(dictation_mod, "dictate", failed)
    assert mic.run(["--device", "1"]) == 8
    assert calls == [], "a failed/denied dictation is not proof anything works here"


def test_a_successful_ad_hoc_dictation_enables_the_daemon_when_undecided(
    tmp_path, monkeypatch
) -> None:
    """The first ad-hoc dictation that actually completes is proof Accessibility and the
    microphone both work here — the trigger `_maybe_autostart_daemon` turns into "enable
    the daemon", exactly once, but ONLY once the daemon it installed is verified genuinely
    alive (#87/#54)."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    assert config.load_settings().mic_daemon_autostart is None

    enabled: list[bool] = []

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(running=False, enabled=False)

        def enable(self) -> None:
            enabled.append(True)

        def autostart_active(self) -> bool:
            return True

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None),
    )
    mic._maybe_autostart_daemon(quiet=True)

    assert enabled == [True]
    assert config.load_settings().mic_daemon_autostart is True


def test_autostart_still_installs_when_something_is_already_running_but_not_enabled(
    tmp_path, monkeypatch
) -> None:
    """A manager-spawned or foreign process already being ALIVE is not the same fact as
    autostart being CONFIGURED — the whole point of this trigger. Keying the guard off
    `running` instead of `enabled` let an already-running-but-never-enabled daemon record
    `mic_daemon_autostart = True` without `enable()` ever being called, permanently
    skipping the one thing this function exists to do (the flag then blocks every future
    check). Caught in review before merge; this is the regression test."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    enabled: list[bool] = []

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(running=True, enabled=False)

        def enable(self) -> None:
            enabled.append(True)

        def autostart_active(self) -> bool:
            return True

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None),
    )
    mic._maybe_autostart_daemon(quiet=True)

    assert enabled == [True], "enable() must still run: something running is not autostart"
    assert config.load_settings().mic_daemon_autostart is True


def test_autostart_never_fires_twice_or_after_an_explicit_decision(tmp_path, monkeypatch) -> None:
    """Once `mic_daemon_autostart` is set — by this same auto-enable OR by an explicit
    `stt mic enable`/`disable` — a later successful dictation must never touch the daemon
    again (#87).

    Asserts on an OBSERVED call list rather than on `status()` raising: an earlier version
    of this test raised `AssertionError` from `status()` instead, which the caller's own
    broad `except Exception` (a deliberate, separate contract: never break a dictation that
    just worked) swallowed right along with a real bug — deleting the early-return this test
    is supposed to guard left `status()` genuinely called, `AssertionError` raised, and
    silently caught, and the test still passed. Caught in review; this is the fix."""
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    config.save_setting("mic_daemon_autostart", False)

    calls: list[str] = []

    class _FakeManager:
        def status(self) -> object:
            calls.append("status")
            return argparse.Namespace(enabled=False, running=False)

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    mic._maybe_autostart_daemon(quiet=True)  # must not raise

    assert calls == [], "must not even ask the daemon's status once already decided"
    assert config.load_settings().mic_daemon_autostart is False


def test_the_daemon_lock_prevents_an_explicit_disable_from_interleaving_with_auto_enable(
    tmp_path, monkeypatch
) -> None:
    """The deeper race #2 identifies: `manager.enable()` (the automatic path) and
    `dispatch()` (an explicit `stt mic disable`) both used to run with NO lock around the
    real daemon-control call itself — only the final config WRITE was atomic
    (`save_setting_if_unset`). A concurrent disable's real launchd-off could land in the gap
    between the automatic path's own status check and its `enable()` call, re-installing
    autostart the user had just removed, even though the config record afterward correctly
    read `False`. Both paths now share `config.locked_for_daemon_decision()` around their
    WHOLE control-then-persist sequence — this proves it with two REAL OS threads, not a
    single-threaded simulation: the explicit disable must genuinely block (not merely be
    scheduled after) until the automatic path's `enable()` call has released the lock."""
    import threading
    import time

    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    order_lock = threading.Lock()
    order: list[str] = []

    def record(tag: str) -> None:
        with order_lock:
            order.append(tag)

    entered_enable = threading.Event()
    release_enable = threading.Event()

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(enabled=False, running=False)

        def enable(self) -> None:
            record("auto_enter")
            entered_enable.set()
            # Held open long enough that a concurrent disable, if the lock genuinely did
            # not cover this call, would have every opportunity to land here.
            release_enable.wait(timeout=2.0)
            record("auto_exit")

        def autostart_active(self) -> bool:
            return True

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None),
    )

    auto_thread = threading.Thread(target=lambda: mic._maybe_autostart_daemon(quiet=True))
    auto_thread.start()
    assert entered_enable.wait(timeout=2.0), "the automatic path never reached enable()"

    def fake_dispatch(ns, on_no_subcommand=None) -> int:
        record("disable_enter")
        record("disable_exit")
        return 0

    _fake_service_lib(
        monkeypatch,
        run_action=lambda manager, action: 0,
        dispatch=fake_dispatch,
        add_service_subcommands=_fake_add_subcommands,
    )
    disable_result: dict[str, int] = {}
    disable_thread = threading.Thread(
        target=lambda: disable_result.update(exit_code=mic._lifecycle(["disable"]))
    )
    disable_thread.start()
    time.sleep(0.2)  # a real chance to race in, if the lock did not actually stop it
    assert order == ["auto_enter"], "the disable must still be blocked on the lock"

    release_enable.set()
    auto_thread.join(timeout=2.0)
    disable_thread.join(timeout=2.0)
    assert not auto_thread.is_alive() and not disable_thread.is_alive()

    assert order == ["auto_enter", "auto_exit", "disable_enter", "disable_exit"], (
        "the two critical sections must never interleave"
    )
    assert disable_result["exit_code"] == 0
    assert config.load_settings().mic_daemon_autostart is False, (
        "the explicit disable ran second and must be the decision that is recorded"
    )


def test_autostart_is_a_no_op_without_the_shared_service_lib(tmp_path, monkeypatch) -> None:
    """A machine without `agenttools_service` completes ad-hoc dictation exactly as before —
    no traceback, no attempted daemon control — and the flag stays undecided so this can try
    again once the lib is installed (#87)."""
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    def boom(ns):
        raise ImportError("no agenttools_service")

    monkeypatch.setattr(mic, "_manager", boom)
    mic._maybe_autostart_daemon(quiet=True)  # must not raise

    assert config.load_settings().mic_daemon_autostart is None


def test_a_hand_edited_null_decision_still_persists_and_notifies_on_autostart(
    tmp_path, monkeypatch
) -> None:
    """A hand-edited `{"mic_daemon_autostart": null}` is documented as a valid, explicit
    "undecided" — but `save_setting_if_unset` used to test presence (`key in raw`) while
    the gate above it tested value (`is not None`): the gate let the decision proceed, the
    real `enable()` ran, and the persist step then refused to write over the merely-PRESENT
    key — no confirmation note, and the record stayed `null` forever. Caught in review
    before merge; this is the regression test."""
    import json

    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    config.ensure_dirs()
    config.config_path().write_text(json.dumps({"mic_daemon_autostart": None}), "utf-8")

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(enabled=False, running=False)

        def enable(self) -> None:
            pass

        def autostart_active(self) -> bool:
            return True

    notes: list[str] = []
    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None),
    )
    monkeypatch.setattr("stt_cli.live.status.note", lambda message, **_: notes.append(message))
    mic._maybe_autostart_daemon(quiet=True)

    assert config.load_settings().mic_daemon_autostart is True, "the decision must persist"
    assert notes, "the user must be told the daemon is now on"


def test_the_shared_service_libs_attribute_surface_still_matches_what_mic_py_reads() -> None:
    """`_maybe_autostart_daemon`/`_status` read `st.enabled`, `st.running`, `st.pid`,
    `st.state` off `manager.status()`, and call `manager.autostart_active()` — none of that
    is checked by mypy (`agenttools_service` is `ignore_missing_imports` for this repo), so
    a silent rename upstream would permanently and silently disable feature #87 on every
    machine with no test anywhere catching it. Gated: only runs where the real lib is
    actually installed (a documented follow-up where it is not, e.g. this environment
    today). Read-only: `status()`/`autostart_active()` are queries; this never calls
    `enable()`/`disable()`, which have real launchd/systemd side effects."""
    pytest.importorskip("agenttools_service")
    from stt_cli.commands import mic

    manager = mic._default_manager()
    for method in ("autostart_active", "enable", "disable"):
        assert callable(getattr(manager, method, None)), f"ServiceManager.{method} is gone"

    st = manager.status()
    for attr in ("enabled", "running", "pid", "state"):
        assert hasattr(st, attr), f"ServiceManager.status() no longer exposes {attr!r}"


def test_a_failure_enabling_the_daemon_never_breaks_the_dictation_that_just_worked(
    tmp_path, monkeypatch
) -> None:
    """`enable()` can fail for its own reasons (launchd unreachable, ...) — that must never
    surface as an exception from a function called after dictation already succeeded, and
    must leave the flag undecided so a later run can retry (#87)."""
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    class _FakeManager:
        def status(self) -> object:
            return argparse.Namespace(running=False, enabled=False)

        def enable(self) -> None:
            raise RuntimeError("launchctl exploded")

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    mic._maybe_autostart_daemon(quiet=True)  # must not raise

    assert config.load_settings().mic_daemon_autostart is None


def test_a_failure_persisting_the_decision_never_breaks_the_dictation_that_just_worked(
    tmp_path, monkeypatch
) -> None:
    """The promise is "never breaks the dictation that just succeeded", not "never breaks
    it unless the failure happens to be in config.save_setting_if_unset" — a config.json
    write can itself raise (a lock file that cannot be opened, the file grown past the
    size guard), and that must be caught exactly like a daemon-manager failure. Caught in
    review before merge; this is the regression test."""
    from stt_cli import config as config_mod
    from stt_cli._errors import UsageError
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(running=False, enabled=False)

        def enable(self) -> None:
            pass

        def autostart_active(self) -> bool:
            return True

    def boom(key: str, value: object, *, unset: tuple = (None,)) -> bool:
        raise UsageError(what="config.json is broken", why="test", how="test")

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None),
    )
    monkeypatch.setattr(config_mod, "save_setting_if_unset", boom)
    mic._maybe_autostart_daemon(quiet=True)  # must not raise


def test_autostart_never_reverses_an_explicit_disable_recorded_while_it_was_deciding(
    tmp_path, monkeypatch
) -> None:
    """A `stt mic disable` in another terminal can complete WHILE this function is between
    reading the (undecided) flag and writing its own decision — `manager.enable()` is the
    slow daemon-control call that window sits around. `save_setting_if_unset` does the
    check-and-write as ONE lock acquisition, so the race is closed no matter how it lands,
    not merely narrowed. Caught in review before merge; this is the regression test."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(running=False, enabled=False)

        def enable(self) -> None:
            # Simulates a concurrent `stt mic disable` completing while THIS `enable()`
            # call (the slow, real daemon-control step) is still in flight.
            config.save_setting("mic_daemon_autostart", False)

        def autostart_active(self) -> bool:
            return True

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None),
    )
    mic._maybe_autostart_daemon(quiet=True)

    assert config.load_settings().mic_daemon_autostart is False, (
        "the explicit disable that landed mid-decision must win, not be overwritten"
    )


def test_a_crash_looping_daemon_after_autostart_is_never_recorded_as_confirmed(
    tmp_path, monkeypatch
) -> None:
    """End-to-end regression for the correctness blocker review caught: `enable()`
    succeeding only proves the AD-HOC process's own TCC identity works, never the
    separately-spawned launchd daemon's (#54's own investigation, this session's TCC-scope
    investigation). Runs the REAL `stt mic` entry point — `run()` -> a genuinely successful
    dictation -> the real `_maybe_autostart_daemon`, faking only `_manager` (no real
    launchd) and `mic_service.probe_launchd` (the daemon's own liveness/exit-code probe,
    diagnosing the exact #54 crash-loop: registered, no live pid, last exit code
    `EXIT_PERMISSION`). Must NOT persist `True` (that would silently claim a permanently
    crash-looping daemon is confirmed working, never retrying, never telling the user
    anything is wrong — exactly the failure this session kept hitting) and must NOT
    persist `False` either (nobody explicitly declined) — only the dedicated retryable
    state, with the real permission principal named in the user-facing message, not a
    generic 'grant Accessibility' hint."""
    from stt_cli._errors import EXIT_PERMISSION
    from stt_cli.commands import mic
    from stt_cli.live import dictation as dictation_mod
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    assert config.load_settings().mic_daemon_autostart is None

    enabled: list[bool] = []

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(running=False, enabled=False, state="stopped", pid=None)

        def enable(self) -> None:
            enabled.append(True)

        def autostart_active(self) -> bool:
            return True

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(mic, "_adhoc_banner", lambda **_: None)
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(
            alive=False, pid=None, last_exit_code=EXIT_PERMISSION
        ),
    )
    notes: list[str] = []
    monkeypatch.setattr("stt_cli.live.status.note", lambda message, **_: notes.append(message))

    async def fake_dictate(args: object) -> int:
        return 0

    monkeypatch.setattr(dictation_mod, "dictate", fake_dictate)

    assert mic.run(["--device", "1"]) == 0

    assert enabled == [True], "enable() must still have run: the OS control itself succeeded"
    settled = config.load_settings().mic_daemon_autostart
    assert settled == config.MIC_AUTOSTART_RETRY, (
        f"a crash-looping daemon must record the retryable state, got {settled!r}"
    )
    assert settled is not True and settled is not False
    assert notes, "the user must be told immediately, not left to discover this later"
    message = notes[0]
    assert "Accessibility" in message
    assert "python interpreter" in message or "bin/stt" in message, (
        "must name the real permission principal (the launchd-spawned process), not a "
        "generic hint about the terminal"
    )
    assert "launchd-spawned process" in message


def test_a_crash_looping_daemon_can_be_retried_by_a_later_successful_dictation(
    tmp_path, monkeypatch
) -> None:
    """The retryable state left by a previous crash-loop must not permanently wedge the
    trigger — once the daemon comes up genuinely alive (Accessibility was granted since),
    the very next successful ad-hoc dictation confirms it for real (#87/#54)."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    config.save_setting("mic_daemon_autostart", config.MIC_AUTOSTART_RETRY)

    enabled: list[bool] = []

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            # The OS install from the earlier attempt is still there — only the LIVENESS
            # was never confirmed, so `enable()` must not be called a second time.
            return argparse.Namespace(running=False, enabled=True)

        def enable(self) -> None:
            enabled.append(True)

        def autostart_active(self) -> bool:
            return True

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(
        mic_service,
        "probe_launchd",
        lambda label: mic_service.LaunchdProbe(alive=True, pid=4242, last_exit_code=None),
    )
    mic._maybe_autostart_daemon(quiet=True)

    assert enabled == [], "already-enabled OS autostart must not be re-installed"
    assert config.load_settings().mic_daemon_autostart is True


def test_a_daemon_that_never_resolves_within_the_probe_budget_stays_retryable(
    tmp_path, monkeypatch
) -> None:
    """`probe_launchd` answering (registered, but neither alive nor with a diagnosed exit
    code yet — genuinely still starting, or stuck) for the WHOLE bounded probe budget must
    still refuse to persist `True`: "the OS accepted the control command" is not "verified
    alive", and running out of patience is not evidence of success either (#87/#54)."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    monkeypatch.setattr(mic.time, "sleep", lambda _seconds: None)
    # The bounded multi-probe loop only exists on macOS — `_wait_for_daemon_liveness`
    # probes once and returns off it — so this has to pin the platform the way the
    # sibling test below does, or Linux CI sees one probe instead of the full budget (#96).
    monkeypatch.setattr(mic.sys, "platform", "darwin")

    probes: list[str] = []

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(running=False, enabled=False)

        def enable(self) -> None:
            pass

        def autostart_active(self) -> bool:
            return True

    def probe_launchd(label: str) -> mic_service.LaunchdProbe:
        probes.append(label)
        return mic_service.LaunchdProbe(alive=False, pid=None, last_exit_code=None)

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(mic_service, "probe_launchd", probe_launchd)
    mic._maybe_autostart_daemon(quiet=True)

    assert len(probes) == mic._AUTOSTART_PROBE_ATTEMPTS, "must poll the full bounded budget"
    assert config.load_settings().mic_daemon_autostart == config.MIC_AUTOSTART_RETRY


def test_launchctl_never_answering_on_macos_never_falsely_confirms_autostart(
    tmp_path, monkeypatch
) -> None:
    """Regression for the correctness bug review caught: `probe_launchd` returning `None`
    on EVERY attempt (launchctl unreachable, timed out, or an unrecognized output shape)
    is NO evidence the daemon is alive — but it is also not "off macOS", where a bare
    `None` is the ONLY answer `probe_launchd` can ever give and is correctly trusted
    (see the sibling test below). On macOS specifically, #87 requires VERIFIED liveness
    before persisting `True`; silently trusting an unanswered probe would let a genuinely
    crash-looping daemon get marked confirmed forever, exactly the failure this whole
    fix exists to prevent."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    monkeypatch.setattr(mic.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(mic.sys, "platform", "darwin")

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(running=False, enabled=False)

        def enable(self) -> None:
            pass

        def autostart_active(self) -> bool:
            return True

    notes: list[str] = []
    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(mic_service, "probe_launchd", lambda label: None)
    monkeypatch.setattr("stt_cli.live.status.note", lambda message, **_: notes.append(message))
    mic._maybe_autostart_daemon(quiet=True)

    settled = config.load_settings().mic_daemon_autostart
    assert settled == config.MIC_AUTOSTART_RETRY, (
        f"an unanswered probe on macOS must not be trusted as confirmed, got {settled!r}"
    )
    assert notes and "could not be verified" in notes[0]


def test_an_unanswerable_probe_off_macos_still_confirms_via_autostart_active(
    tmp_path, monkeypatch
) -> None:
    """Off macOS, `probe_launchd` returns `None` by design — there is no launchd to ask —
    the ONLY answer it can ever give, not a failure to diagnose. That must keep trusting
    `autostart_active()` exactly as it did before #87's liveness verification was added,
    so this launchd-specific check never regresses a platform with no launchd at all."""
    from stt_cli.commands import mic
    from stt_cli.live import mic_service

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    monkeypatch.setattr(mic.sys, "platform", "linux")

    class _FakeManager:
        service = argparse.Namespace(label="com.agenttools.stt.mic")

        def status(self) -> object:
            return argparse.Namespace(running=False, enabled=False)

        def enable(self) -> None:
            pass

        def autostart_active(self) -> bool:
            return True

    probes: list[str] = []

    def probe_launchd(label: str) -> None:
        probes.append(label)

    monkeypatch.setattr(mic, "_manager", lambda ns: _FakeManager())
    monkeypatch.setattr(mic_service, "probe_launchd", probe_launchd)
    mic._maybe_autostart_daemon(quiet=True)

    # Off macOS the bounded retry loop must not run at all (#96) — it would only hold the
    # config lock for the whole budget waiting on a launchd that does not exist. At most one
    # probe, not exactly one: skipping the pointless call entirely would be just as correct.
    assert len(probes) <= 1, "off macOS the bounded retry loop must not run (#96)"
    assert config.load_settings().mic_daemon_autostart is True


def test_explicit_enable_records_the_same_flag_autostart_reads(tmp_path, monkeypatch) -> None:
    """`stt mic enable` is an explicit decision — it must record the SAME flag
    `_maybe_autostart_daemon` reads, so a later ad-hoc dictation never second-guesses it
    (#87)."""
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    _fake_service_lib(
        monkeypatch,
        run_action=lambda manager, action: 0,
        add_service_subcommands=_fake_add_subcommands,
        dispatch=lambda ns, on_no_subcommand: 0,
    )

    assert mic._lifecycle(["enable"]) == 0
    assert config.load_settings().mic_daemon_autostart is True


def test_explicit_disable_records_the_flag_too(tmp_path, monkeypatch) -> None:
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))
    config.save_setting("mic_daemon_autostart", True)  # as if enable ran earlier

    _fake_service_lib(
        monkeypatch,
        run_action=lambda manager, action: 0,
        add_service_subcommands=_fake_add_subcommands,
        dispatch=lambda ns, on_no_subcommand: 0,
    )

    assert mic._lifecycle(["disable"]) == 0
    assert config.load_settings().mic_daemon_autostart is False


def test_a_failed_enable_does_not_falsely_record_success(tmp_path, monkeypatch) -> None:
    """`dispatch` returning a nonzero exit code (the OS control command failed) must not be
    recorded as a successful opt-in — the flag would then lie about the daemon's real state
    (#87)."""
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    _fake_service_lib(
        monkeypatch,
        run_action=lambda manager, action: 0,
        add_service_subcommands=_fake_add_subcommands,
        dispatch=lambda ns, on_no_subcommand: 4,
    )

    assert mic._lifecycle(["enable"]) == 4
    assert config.load_settings().mic_daemon_autostart is None


def test_a_successful_enable_that_fails_to_persist_still_reports_ok_but_warns(
    tmp_path, monkeypatch
) -> None:
    """The daemon action itself already succeeded (`dispatch` returned `EXIT_OK`) by the
    time `_lifecycle` tries to persist that decision — a lock file that cannot be opened,
    config.json grown past the size guard, or an `OSError` out of the write itself
    (`ENOSPC` and friends are not `SttError`s) must not turn a real, successful `stt mic
    enable` into a reported failure. But it must not go unnoticed either: an un-persisted
    `enable` leaves `mic_daemon_autostart` at whatever it was before, so a later ad-hoc
    dictation could re-decide this on its own — the user is told immediately instead."""
    from stt_cli import config as config_mod
    from stt_cli._errors import UsageError
    from stt_cli.commands import mic

    monkeypatch.setenv("STT_HOME", str(tmp_path))

    _fake_service_lib(
        monkeypatch,
        run_action=lambda manager, action: 0,
        add_service_subcommands=_fake_add_subcommands,
        dispatch=lambda ns, on_no_subcommand: 0,
    )

    def boom(key: str, value: object) -> None:
        raise UsageError(what="config.json is broken", why="test", how="test")

    monkeypatch.setattr(config_mod, "save_setting", boom)
    notes: list[str] = []
    monkeypatch.setattr("stt_cli.live.status.note", lambda message, **_: notes.append(message))

    assert mic._lifecycle(["enable"]) == 0, "the OS control command itself succeeded"
    assert config.load_settings().mic_daemon_autostart is None, "the write genuinely failed"
    assert notes, "a failed persist must not pass silently"
    assert "enable" in notes[0] and "recording that decision failed" in notes[0]
