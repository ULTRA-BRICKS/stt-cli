"""configtui's pure surface: grouping stays in sync with `Settings`, navigation, search, and
the edit/reset glue — everything except the actual curses drawing, which nothing else in
this suite exercises against a real terminal either.
"""

from __future__ import annotations

import pytest

from stt_cli import config
from stt_cli.commands import configtui

# ── metadata stays in sync with Settings ─────────────────────────────────────


def test_every_configurable_setting_has_exactly_one_group() -> None:
    """The failure this guards against: a new `Settings` field lands, `_CONFIGURABLE` picks
    it up automatically, and it is now settable by `stt config set` but invisible on the
    screen that exists to help somebody find it. Catches both directions — a setting with no
    group, and a stray/duplicated/renamed one left in `GROUPS`."""
    grouped = [name for _group, names in configtui.GROUPS for name in names]
    assert len(grouped) == len(set(grouped)), "a setting listed in more than one group"
    assert set(grouped) == config.configurable()


def test_every_grouped_setting_has_a_help_line() -> None:
    for _group, names in configtui.GROUPS:
        for name in names:
            assert name in configtui.HELP, f"{name} has no description"
            assert configtui.HELP[name], f"{name}'s description is empty"


def test_group_names_are_unique() -> None:
    names = [group for group, _settings in configtui.GROUPS]
    assert len(names) == len(set(names))


def test_examples_and_choices_only_name_real_settings() -> None:
    known = config.configurable()
    assert set(configtui.EXAMPLES) <= known
    assert set(configtui._choices()) <= known


def test_every_hotkey_example_is_a_spec_parse_actually_accepts() -> None:
    """A misleading example in the TUI is worse than no example: `cmd+shift+;` looked
    plausible and reads back from ``EXAMPLES`` verbatim, but `hotkey.parse` names the key
    `semicolon`, not `;`, so typing the example the screen itself suggested was refused."""
    from stt_cli.live.hotkey import parse

    for example in configtui.EXAMPLES["hotkey"]:
        parse(example)  # raises HotkeyError if this example would be refused


def test_choices_are_internally_consistent_with_their_own_source_of_truth() -> None:
    """Each fixed choice list is imported from wherever that enum is really defined
    (`pipeline.COMPARE_MODES`, `hotkey.VALID_MODES`, ...) rather than retyped here — this
    pins that the import actually landed a non-empty, string-only tuple, not a typo."""
    for key, choices in configtui._choices().items():
        assert choices, f"{key} has an empty choice list"
        assert all(isinstance(c, str) for c in choices), f"{key}'s choices are not all strings"


# ── TuiState navigation ───────────────────────────────────────────────────────


def test_move_clamps_at_both_ends_of_the_current_group() -> None:
    state = configtui.TuiState()
    state.move(-5)
    assert state.row == 0
    state.move(1000)
    assert state.row == len(configtui.GROUPS[0][1]) - 1


def test_next_and_prev_group_wrap_and_reset_the_row() -> None:
    state = configtui.TuiState(row=2)
    state.prev_group()
    assert state.group == len(configtui.GROUPS) - 1 and state.row == 0

    state = configtui.TuiState(group=len(configtui.GROUPS) - 1, row=1)
    state.next_group()
    assert state.group == 0 and state.row == 0


def test_selected_key_returns_the_first_setting_in_the_current_group() -> None:
    state = configtui.TuiState()
    assert state.selected_key() == configtui.GROUPS[0][1][0]


def test_selected_key_is_none_when_nothing_matches() -> None:
    """The genuinely empty case: a search with no matches, not any real group (every real
    group always holds at least one setting)."""
    state = configtui.TuiState()
    state.start_search()
    state.type_search("this matches absolutely nothing at all")
    assert state.matches == ()
    assert state.selected_key() is None


# ── search ─────────────────────────────────────────────────────────────────────


def test_search_matches_by_name_or_by_description() -> None:
    state = configtui.TuiState()
    state.start_search()
    state.type_search("hotkey")
    assert "hotkey" in state.matches and "hotkey_mode" in state.matches
    assert "model" not in state.matches


def test_search_matching_a_description_word_finds_the_setting_by_meaning() -> None:
    """The point of searching descriptions too: somebody who does not know the setting is
    called `diarize` can still find it by typing what it does."""
    state = configtui.TuiState()
    state.start_search()
    state.type_search("who is speaking")
    assert "diarize" in state.matches


def test_ending_a_search_lands_on_the_selected_settings_real_group() -> None:
    state = configtui.TuiState()
    state.start_search()
    state.type_search("hotkey_mode")
    assert state.matches == ("hotkey_mode",)
    state.end_search()
    assert state.search is None
    assert state.selected_key() == "hotkey_mode"
    _group_name, names = configtui.GROUPS[state.group]
    assert "hotkey_mode" in names


def test_group_navigation_is_disabled_while_searching() -> None:
    state = configtui.TuiState()
    state.start_search()
    before = state.group
    state.next_group()
    state.prev_group()
    assert state.group == before


# ── edit / toggle / reset, through the same path `stt config set` uses ──────────


def test_apply_edit_stores_a_coerced_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    ok, message = configtui.apply_edit("threads", "4")
    assert ok
    assert "threads = 4" in message
    assert config.load_settings().threads == 4


def test_apply_edit_reports_a_refusal_without_raising(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    ok, message = configtui.apply_edit("threads", "not-a-number")
    assert not ok
    assert message  # a human-readable reason, not silence
    assert config.load_settings().threads == 0, "the bad value was never stored"


def test_activate_records_whether_the_footer_message_is_success_or_error(
    tmp_path, monkeypatch
) -> None:
    """`state.message_ok` is what `_draw` colors the footer by (green for a confirmed
    change, red for a refusal) — before it existed, both cases shared the same green,
    so a rejected edit read on screen as if it had succeeded."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    threads_index = configtui.GROUPS[0][1].index("threads")
    state = configtui.TuiState(row=threads_index)
    settings = config.load_settings()
    stored = config.stored()

    monkeypatch.setattr(configtui, "_prompt", lambda stdscr, key, current: "not-a-number")
    configtui._activate(None, state, settings, stored, toggle_only=False)
    assert state.message_ok is False, "a refused edit must not be recorded as a success"

    monkeypatch.setattr(configtui, "_prompt", lambda stdscr, key, current: "4")
    configtui._activate(None, state, settings, stored, toggle_only=False)
    assert state.message_ok is True


def test_apply_toggle_flips_a_boolean(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    ok, message = configtui.apply_toggle("clean", True)
    assert ok and "clean = False" in message
    assert config.load_settings().clean is False


def test_apply_reset_removes_a_stored_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    configtui.apply_edit("hotkey", "cmd+shift+d")
    assert config.load_settings().hotkey == "cmd+shift+d"
    ok, message = configtui.apply_reset("hotkey")
    assert ok and "reset" in message
    assert config.load_settings().hotkey is None


def test_command_line_shows_the_equivalent_cli_invocation() -> None:
    assert configtui.command_line("hotkey", "cmd+shift+d") == "stt config set hotkey cmd+shift+d"
    assert configtui.command_line("language", None) == "stt config set language (unset)"


@pytest.mark.parametrize("key", ["backend", "context", "vad", "text_variant", "hotkey_mode"])
def test_every_choice_offered_is_itself_a_value_apply_edit_accepts(
    key: str, tmp_path, monkeypatch
) -> None:
    """A choice the picker offers has to actually be settable — a stale/typo'd entry in
    `_choices()` would let somebody select a value the coercer then refuses."""
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    for choice in configtui._choices()[key]:
        ok, _message = configtui.apply_edit(key, choice)
        assert ok, f"{key}={choice!r} from its own choice list was refused"
