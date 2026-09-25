"""Every command must at least RUN — the check that would have caught a name collision.

`backends.whispercpp` shadowed the submodule of the same name and blew up with
`'module' object is not callable` the first time `stt doctor` was invoked, while both mypy
and the whole unit suite stayed green. These smoke tests exercise each command's real entry
point so an import-time or attribute-time mistake cannot reach a user again.

They must not need a model, a GPU or a network: `doctor` reports what is missing rather than
requiring it, and the rest work on an empty archive.
"""

from __future__ import annotations

import pytest

from stt_cli._errors import EXIT_MISSING_DEP, EXIT_OK, EXIT_PERMISSION
from stt_cli.cli import _discover, main


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("STT_HOME", str(tmp_path / "home"))


def test_every_command_module_exposes_the_contract() -> None:
    catalog = _discover()
    assert {"transcribe", "doctor", "archive", "models", "config", "setup"} <= set(catalog)


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        ["models"],
        ["models", "ls"],
        ["models", "where"],
        ["archive"],
        ["archive", "ls"],
        ["archive", "usage"],
        ["archive", "gc"],
        ["config"],
        ["config", "list"],
        ["config", "path"],
        ["diarize"],
        ["diarize", "status"],
    ],
)
def test_read_only_commands_run(argv: list[str], monkeypatch) -> None:
    """A missing optional dependency is a reported exit code, never a traceback.

    Three codes, not two, and which one comes back is a property of the MACHINE rather than
    of the command: `stt diarize status` answers "not ready" where the wheels are absent and
    "ready, but no token" where they are cached, and both are correct reports of a real
    state. The claim being made here is that none of them is an exception.
    """
    # The readiness probe is answered here rather than run. It launches a real `uv run` and,
    # with a warm diarization cache, imports torch — tens of seconds, and a different answer
    # on every machine. This file's contract is that a command needs no model, no GPU and no
    # network, and a smoke test that spawns a subprocess is not keeping it.
    from stt_cli import diarize as diarize_mod

    async def not_ready() -> bool:
        return False

    monkeypatch.setattr(diarize_mod, "ready", lambda **_: not_ready())

    # The third code is allowed for diarization ALONE. Widening it for every command would
    # have made this quietly accept "you are not signed in" from `stt config list`, which
    # would be a defect rather than a state.
    allowed = {EXIT_OK, EXIT_MISSING_DEP}
    if argv[0] == "diarize":
        allowed.add(EXIT_PERMISSION)
    assert main(argv) in allowed


@pytest.mark.parametrize(
    "argv", [["transcribe", "--help"], ["archive", "--help"], ["doctor", "--help"]]
)
def test_help_for_each_command(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 0


def test_config_round_trip_through_the_cli(capsys) -> None:
    assert main(["config", "set", "language", "ru"]) == EXIT_OK
    capsys.readouterr()
    assert main(["config", "get", "language"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "ru"


@pytest.fixture
def no_diarize_probe(monkeypatch):
    """`doctor`'s optional section always calls `diarize.ready()`, which shells out to a
    real `uv run` and, on a warm cache, imports torch — tens of seconds. Every doctor test
    below stubs it out to keep this file's own no-GPU/no-network contract (see the module
    docstring), matching the same stub `test_read_only_commands_run` already applies.
    """
    from stt_cli import diarize as diarize_mod

    async def not_ready() -> bool:
        return False

    monkeypatch.setattr(diarize_mod, "ready", lambda **_: not_ready())


def test_doctor_fix_prompts_when_accessibility_not_granted(
    monkeypatch, capsys, no_diarize_probe
) -> None:
    """`stt doctor --fix` triggers the native prompt exactly when Accessibility is missing,
    and reports the state the poll afterwards settles in."""
    import sys

    from stt_cli.live import quartz

    state = {"granted": False}
    asked = []

    def fake_ask_for_trust() -> bool:
        asked.append(True)
        state["granted"] = True  # the "click" a human would make, simulated instantly
        return False

    monkeypatch.setattr(quartz, "trusted", lambda: state["granted"])
    monkeypatch.setattr(quartz, "ask_for_trust", fake_ask_for_trust)
    # The prompt is only raised on macOS; CI runs this file on ubuntu-latest too (see
    # .github/workflows/tests.yml), so without pinning this here the new platform gate in
    # `_fix_section` would short-circuit before `ask_for_trust` and this test would fail.
    monkeypatch.setattr(sys, "platform", "darwin")

    main(["doctor", "--fix"])

    assert asked == [True]
    out = capsys.readouterr().out
    assert "Accessibility: granted" in out


def test_doctor_fix_skips_prompt_when_already_granted(
    monkeypatch, capsys, no_diarize_probe
) -> None:
    """Nothing to fix, nothing shown."""
    from stt_cli.live import quartz

    asked = []
    monkeypatch.setattr(quartz, "trusted", lambda: True)
    monkeypatch.setattr(quartz, "ask_for_trust", lambda: asked.append(True))

    main(["doctor", "--fix"])

    assert asked == []
    out = capsys.readouterr().out
    assert "already granted, nothing to fix" in out


def test_doctor_fix_reports_not_applicable_off_macos(monkeypatch, capsys, no_diarize_probe) -> None:
    """Off macOS, `trusted`/`ask_for_trust` both fail soft to `False` — indistinguishable
    from "prompt shown, not yet granted" — so `--fix` must gate on the platform itself
    rather than tell a Linux user to follow a dialog that can never appear."""
    import sys

    from stt_cli.live import quartz

    asked = []
    monkeypatch.setattr(quartz, "trusted", lambda: False)
    monkeypatch.setattr(quartz, "ask_for_trust", lambda: asked.append(True))
    monkeypatch.setattr(sys, "platform", "linux")

    main(["doctor", "--fix"])

    assert asked == []
    assert "not applicable" in capsys.readouterr().out


def test_doctor_without_fix_never_prompts(monkeypatch, capsys, no_diarize_probe) -> None:
    """The default `stt doctor` (no `--fix`) never pops the native dialog."""
    from stt_cli.live import quartz

    asked = []
    monkeypatch.setattr(quartz, "trusted", lambda: False)
    monkeypatch.setattr(quartz, "ask_for_trust", lambda: asked.append(True))

    main(["doctor"])

    assert asked == []
    assert "\nfix" not in capsys.readouterr().out


def test_wait_for_trust_retries_until_the_predicate_flips() -> None:
    """The whole point of the poll: it retries rather than trusting a single stale read.

    Deleting the retry loop (returning `is_trusted()` once) would make this fail, unlike
    the CLI-level tests above where the fake flips synchronously on the first check.
    """
    from stt_cli.commands.doctor import _wait_for_trust

    calls = {"n": 0}

    def flips_on_the_third_check() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    assert _wait_for_trust(flips_on_the_third_check, attempts=5, interval=0) is True
    assert calls["n"] == 3


def test_wait_for_trust_times_out_when_never_granted() -> None:
    from stt_cli.commands.doctor import _wait_for_trust

    calls = {"n": 0}

    def never_trusted() -> bool:
        calls["n"] += 1
        return False

    assert _wait_for_trust(never_trusted, attempts=4, interval=0) is False
    assert calls["n"] == 4
