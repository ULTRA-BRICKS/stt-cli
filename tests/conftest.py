"""Guards every test gets without asking: the suite never reaches the real launchd.

`stt mic enable`/`disable`/`status` and the daemon's autostart talk to launchd through
`launchctl`, and since #105 every `stt` command on macOS may start a detached worker that
does the same. On a developer's Mac a test that slipped past its fakes would install, start
or remove the real LaunchAgent — the one the person running the suite relies on. So every
process a test starts goes through `_Guarded`, which refuses `launchctl` and the autostart
worker outright and records the attempt, and the test fails at teardown even when the code
under test swallowed the refusal.

The post-command autostart hook (`commands/mic.py::autostart_after`, called by `cli.main`)
is also inert unless a test asks for it by requesting the `autostart_hook` fixture: tests
that run `cli.main` for some other command must not depend on whether the person running
them has decided about the daemon.

A transcription may start the detached dictionary-suggestion check (`suggest._spawn_worker`,
#88). No test may: it would outlive the test, reading whichever `STT_HOME` it inherited. A
test that exercises that hook passes its own `spawn=`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Iterator

import pytest

from stt_cli import suggest
from stt_cli.commands import mic
from stt_cli.live.mic_service import AUTOSTART_TOKEN


class LaunchdReachedInTest(AssertionError):
    """A test started `launchctl`, or the autostart worker that runs it."""


def _reaches_launchd(args: object) -> bool:
    # A string is a whole command line (`shell=True`, or one word with no arguments), so its
    # first word is split out before its basename is compared.
    if isinstance(args, (str, bytes, os.PathLike)):
        words = shlex.split(os.fsdecode(args))
    else:
        words = [os.fsdecode(word) for word in args]  # type: ignore[attr-defined]
    return bool(words) and (os.path.basename(words[0]) == "launchctl" or AUTOSTART_TOKEN in words)


@pytest.fixture(autouse=True)
def _never_a_detached_suggestion_check(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    started: list[bool] = []
    monkeypatch.setattr(suggest, "_spawn_worker", lambda: started.append(True))
    yield
    assert not started, "a test started the detached suggestion check; pass `spawn=` instead"


@pytest.fixture(autouse=True)
def _never_the_real_launchd(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[list[str]]]:
    reached: list[list[str]] = []

    class _Guarded(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, args: object, *rest: object, **options: object) -> None:
            if _reaches_launchd(args):
                reached.append(
                    [str(word) for word in args] if isinstance(args, list) else [str(args)]
                )
                raise LaunchdReachedInTest(f"a test tried to run {args!r}")
            super().__init__(args, *rest, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", _Guarded)
    if "autostart_hook" not in request.fixturenames:
        monkeypatch.setattr(mic, "autostart_after", lambda argv: None)
    yield reached
    assert not reached, f"the suite must never reach the real launchd: {reached}"


@pytest.fixture(autouse=True)
def _never_the_real_helper_app(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer's Mac may have `~/Applications/stt.app` installed, and with it every
    burst's notification and the daemon's launchd argv change. The suite sees an empty
    Applications folder of its own instead, and a build — a Swift compile, a keychain — is
    refused: a test that wants a helper installs a fake one there."""
    from stt_cli.live import helper

    apps = tmp_path_factory.mktemp("Applications")
    monkeypatch.setattr(helper, "install_dir", lambda: apps)

    def refused() -> helper.Built:
        raise helper.HelperBuildError(what="tests never build stt.app", why="", how="")

    monkeypatch.setattr(helper, "build", refused)


@pytest.fixture
def autostart_hook() -> None:
    """Keep `cli.main`'s real post-command autostart hook for this test (see the module
    docstring); `_never_the_real_launchd` still refuses what it would start."""
