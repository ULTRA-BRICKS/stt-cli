"""doctor — say what is installed, what is missing, and exactly how to fix each gap.

A speech pipeline has a lot of moving parts that live outside the package: ffmpeg, an
engine, model files, an LLM CLI, optionally pyannote. When one of them is absent the
failure surfaces halfway through a long run, in whatever language that component speaks.
This command asks all of them up front and reports in one place, so a broken setup is a
thirty-second check rather than a discovery made forty minutes into a transcription.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable

from .. import backends, config, llm, proc, resources
from .._errors import EXIT_MISSING_DEP, EXIT_OK

NAME = "doctor"
SUMMARY = "check ffmpeg, engines, models, LLM tools and disk space"


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="stt doctor", description=SUMMARY)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="after reporting, attempt auto-fixable gaps (the native macOS Accessibility "
        "prompt, and building stt.app for the notification's Stop button) and report the result",
    )
    args = parser.parse_args(argv)

    required_ok = _media_section()
    engine_ok = _engine_section()
    _optional_section()
    _storage_section()

    if args.fix:
        _fix_section()

    if required_ok and engine_ok:
        print("\nEverything stt needs is present.")
        return EXIT_OK
    print("\nSomething required is missing — see the fix lines above, or run `stt setup`.")
    return EXIT_MISSING_DEP


def _fix_section() -> None:
    """`--fix`: attempt the auto-fixable gaps found above, then report where they landed.

    The only fixable gap today is Accessibility, and "fix" is generous: the OS lets code
    RAISE its own consent flow (`quartz.ask_for_trust`) but never lets code finish it —
    completing it stays the user's, done in their own time. This waits a few seconds for
    whoever is at the machine to do that, then reports whichever state it settled in; it
    never blocks indefinitely on an action that may not come.
    """
    print("\nfix")
    _fix_terminal_accessibility()
    if sys.platform == "darwin":
        from ..live import helper

        found = helper.ensure(say=False)
        if found is not None:
            print(f"  {_mark(True)} mic notification Stop button: {helper.app_path()}")
        else:
            print(
                f"  {_mark(False)} mic notification Stop button: {helper.APP_NAME} could not "
                f"be built — `stt mic helper` says why"
            )
        print(
            "  the background dictation daemon is a separate principal to macOS and asks for "
            "its own — `stt mic --fix` turns it on and makes it ask"
        )


def _fix_terminal_accessibility() -> None:
    from ..live import quartz

    if quartz.trusted():
        print(f"  {_mark(True)} Accessibility: already granted, nothing to fix")
        return
    if sys.platform != "darwin":
        # `ask_for_trust`/`trusted` both fail soft to False off macOS (no frameworks to
        # load), indistinguishable from "prompt shown, not yet granted" — so without this
        # gate the report below would tell a Linux user to follow a dialog that can never
        # appear, then poll it for several silent seconds before saying so.
        print(f"  {_mark(False)} Accessibility: not applicable (macOS only)")
        return
    print(
        "  a native macOS Accessibility prompt is about to appear — follow it to grant "
        "access to your terminal application"
    )
    quartz.ask_for_trust()
    granted = _wait_for_trust(quartz.trusted)
    print(f"  {_mark(granted)} Accessibility: {'granted' if granted else 'still NOT granted'}")
    if not granted:
        print("      if you granted it just now, stt may need a new terminal to see it:")
        print(f"      {quartz.ACCESSIBILITY_HINT}")


def _wait_for_trust(
    is_trusted: Callable[[], bool], *, attempts: int = 10, interval: float = 1.0
) -> bool:
    """Poll `is_trusted` for a few seconds, sleeping only BETWEEN checks.

    A single immediate check would read the prompt's OWN pre-action state almost every
    time — completing it is a human action on their own schedule, not an instant callback.
    `is_trusted` is injected (rather than this calling `quartz.trusted` itself) so a test
    can prove the retry happens without waiting on a real clock or a real dialog.
    """
    for attempt in range(attempts):
        if is_trusted():
            return True
        if attempt < attempts - 1:
            time.sleep(interval)
    return False


def _media_section() -> bool:
    print("media (required)")
    ok = True
    for binary, hint in (("ffmpeg", "brew install ffmpeg"), ("ffprobe", "brew install ffmpeg")):
        found = proc.which(binary)
        print(f"  {_mark(bool(found))} {binary:<10} {found or 'not found'}")
        if not found:
            print(f"      fix: {hint}")
            ok = False
    return ok


def _engine_section() -> bool:
    print("\nspeech engines (at least one required)")
    any_ok = False
    for name, status in backends.survey():
        print(f"  {_mark(status.ok)} {name:<10} {status.detail}")
        if not status.ok and status.how_to_install:
            print(f"      fix: {status.how_to_install}")
        any_ok = any_ok or status.ok
    if any_ok:
        _model_section()
    return any_ok


def _model_section() -> None:
    from ..registry import MODELS

    print("\n  models on disk")
    backend = backends.whispercpp_backend()
    if not backend.availability().ok:
        print("    (whisper.cpp not installed; the mlx engine downloads models on demand)")
        return
    for spec in MODELS:
        if "whispercpp" not in spec.engine_ids:
            continue
        try:
            path = backend.model_path(spec.name)
            print(f"    ✓ {spec.name:<18} {path}")
        except Exception:
            print(f"    · {spec.name:<18} not downloaded (`stt models pull {spec.name}`)")
    silero = backend.silero_model()
    print(f"    {_mark(bool(silero))} {'silero VAD':<18} {silero or _MISSING_VAD}")


def _optional_section() -> None:
    print("\noptional features")
    tools = llm.available()
    names = ", ".join(t.name for t in tools) or "none"
    print(f"  {_mark(bool(tools))} LLM correction / --summary: {names}")
    if not tools:
        print(
            f"      fix: install one of {', '.join(llm.ORDER)} to enable "
            "`stt transcribe --fix`/`--summary`"
        )

    import asyncio

    from .. import diarize

    installed = asyncio.run(diarize.ready())
    print(f"  {_mark(installed)} speaker diarization: {'ready' if installed else 'not ready'}")
    if not installed:
        print(f"      fix: {diarize.INSTALL_HINT}")
    _dictation_line()
    _notification_stop_line()


def _notification_stop_line() -> None:
    """Whether `stt mic`'s notification carries a Stop button (#79, #99) — stt's own
    `stt.app`, or else terminal-notifier — reported like any other optional feature rather
    than discovered only when a burst starts and no button appears. The hotkey stops a burst
    either way, and the notification is posted either way; only the button depends on this.
    """
    if sys.platform != "darwin":
        return
    from ..live import helper, status

    state = helper.state()
    button = status.stop_button()  # what a burst will actually use
    if state == "current":
        permission = helper.notification_permission() or "unknown"
        allowed, _, style = permission.partition(" ")
        where = f"{helper.app_path()} (notifications: {allowed}, style {style or 'unknown'})"
        if button is not None and not button.via_helper:
            where = f"terminal-notifier, since {where}"
        print(f"  {_mark(button is not None)} mic notification Stop button: {where}")
        if allowed == "denied":
            print("      fix: System Settings > Notifications > stt > Allow notifications")
        elif style == "banner":
            print(
                "      note: banners slide away after a few seconds; System Settings > "
                "Notifications > stt > Alerts keeps the Stop button on screen"
            )
        return
    missing = (
        f"{helper.APP_NAME} not built" if state == "missing" else f"{helper.APP_NAME} out of date"
    )
    if button is not None:
        print(f"  {_mark(True)} mic notification Stop button: terminal-notifier ({missing})")
    else:
        print(f"  {_mark(False)} mic notification Stop button: {missing}")
    print(
        f"      fix: `{status.INSTALL_HINT}` or `stt doctor --fix` builds it (the hotkey "
        "always stops a dictation without it)"
    )


def _dictation_line() -> None:
    """Whether `stt mic` would work, without starting it.

    Its two requirements fail in ways that are hard to read at the moment they fail: a
    missing `whisper-server` looks like whisper.cpp being absent when `whisper-cli` is right
    there, and a missing Accessibility grant looks like a keyboard that types nothing at all.
    Both are worth answering here rather than by trying it and wondering.
    """
    from .. import config

    # The configured checkout, the way `stt mic` asks. Without it, somebody whose whisper.cpp
    # lives where they told stt about it was shown "NOT found" and an install instruction for
    # something they had already installed.
    #
    # Reading the config is allowed to FAIL here. `stt doctor` is the command somebody runs
    # because their setup is broken, and a hand-edited config.json is one of the ways it can
    # be; letting that diagnosed error out stopped the report at this line, so the storage
    # section and the summary never printed for the one command whose job is to say what is
    # wrong. A config that cannot be read is reported as a line, not as the end of the report.
    from .._errors import SttError
    from ..backends import whispercpp
    from ..live import quartz

    try:
        root = config.load_settings().whispercpp_root
    except SttError as unreadable:
        print(f"  {_mark(False)} live dictation (`stt mic`): config unreadable")
        print(f"      fix: {unreadable.how}")
        return
    server = whispercpp.server_binary(root)
    granted = quartz.trusted()
    print(
        f"  {_mark(bool(server) and granted)} live dictation (`stt mic`): "
        f"whisper-server {'found' if server else 'NOT found'}, "
        f"Accessibility {'granted' if granted else 'NOT granted'}"
    )
    if not server:
        # The whisper-server-specific text, not the generic whisper.cpp one. They are two
        # hints for one failure, and the generic one never names `whisper-server` — so the
        # report told somebody to install something they already had, without mentioning
        # that the packaged build may not carry the binary `stt mic` needs.
        from ..live.dictation import SERVER_HINT

        print(f"      fix: {SERVER_HINT}")
    if not granted:
        print(f"      fix: {quartz.ACCESSIBILITY_HINT}")


def _storage_section() -> None:
    from ..archive import Archive

    res = resources.probe(config.app_home())
    print("\nstorage")
    print(f"  archive: {config.app_home()}")
    print(f"  machine: {res.human()}")
    with Archive() as store:
        runs, audio, transcripts = store.usage()
    mib = 1024 * 1024
    print(
        f"  stored:  {runs} run(s), {audio / mib:.0f} MiB audio, "
        f"{transcripts / mib:.1f} MiB transcripts"
    )


_MISSING_VAD = "not downloaded (`stt models pull-vad`)"


def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"
