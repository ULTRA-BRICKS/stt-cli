"""dictation — assembling `stt mic` out of the parts, and taking it apart again afterwards.

Everything here is wiring: permissions, then two models, then the keyboard watcher, then the
microphone, then the loop in `session.py`, and then all of it closed down in the reverse
order however the run ends. The decisions are elsewhere; the ordering is the point of this
file, because half of these things hold something the user notices being held — a recording
light, a system-wide event tap, two copies of a model in memory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config, dictionary, model_gate, phrases, portable
from .._errors import EXIT_OK, EngineError, MissingDependencyError, UsageError
from ..backends import whispercpp
from . import capture, hotkey, permissions, quartz, status, tap
from .gate import Gate
from .server import Transcriber, Voice, WhisperServer
from .session import IDLE_MINUTES, Progress, Session
from .typist import Typist

SERVER_HINT = (
    "`stt mic` needs the whisper-server binary from a whisper.cpp build — `brew install "
    "whisper-cpp`, or clone https://github.com/ggml-org/whisper.cpp and "
    "`cmake -B build && cmake --build build -j`"
)


@contextlib.contextmanager
def _the_only_session() -> Iterator[None]:
    """Refuse to start a second dictation while one is already running.

    Two sessions type into the same window at the same time, and each one deletes what it
    believes it wrote — which is now interleaved with the other's. The result is not two
    transcripts, it is one line of debris, and neither session can tell that anything is
    wrong. An exclusive lock is the whole fix; the file is a marker, its contents are of no
    interest, and the lock goes away with the process however the process ends.
    """
    config.ensure_dirs()
    # Appended to rather than truncated, purely so that failing to get the lock does not
    # first reach into the file the process that HAS it is holding open.
    marker = config.app_home() / "mic.lock"
    handle = marker.open("a")
    try:
        try:
            portable.lock(handle, blocking=False)
        except OSError as exc:
            raise UsageError(
                what="dictation is already running",
                why="another `stt mic` holds the microphone and the keyboard",
                how="finish that one first — Ctrl-C in the terminal it was started from, "
                "its own hotkey (press again, or release it, depending how it's "
                "configured), or the stop notification's button",
            ) from exc
        yield
    finally:
        handle.close()


async def dictate(args: argparse.Namespace) -> int:
    """`stt mic`, from the permission check to the last word typed.

    `--list-devices` and `--check` are answered by the command before this is reached, so
    everything here is a real session. The device listing used to live here as well, which is
    how `--check --list-devices` came to run a six-second listening test instead of printing
    a list: two places answered the same flag and the wrong one got there first.
    """
    quartz.require_accessibility()
    settings = config.load_settings()
    device = await capture.resolve(args.device)
    # The lock before the models, not after. Loading `large-v3-turbo` takes ten seconds and a
    # couple of gigabytes, and a second `stt mic` used to pay both of those before being told
    # that the first one already had the keyboard.
    with _the_only_session():
        async with _models(args, settings) as models:
            said = await _dictate_until_stopped(args, device, models, settings)
    if said:
        print(said)
    return EXIT_OK


@dataclass
class Models:
    """The two loaded models, and the language they were told to expect."""

    settled: Transcriber
    draft: Transcriber | None


@contextlib.asynccontextmanager
async def _models(args: argparse.Namespace, settings: config.Settings) -> AsyncIterator[Models]:
    """Load both models, each under a slot of its own, and unload them however the block ends."""
    # The configured checkout, not just PATH. Someone whose whisper.cpp lives somewhere they
    # told stt about could transcribe files perfectly well and be told by `stt mic` that
    # whisper-server does not exist — the two commands disagreeing about an installation the
    # user had already configured once.
    binary = whispercpp.server_binary(settings.whispercpp_root)
    if binary is None:
        raise MissingDependencyError(
            what="whisper-server was not found",
            why="live dictation keeps a model loaded, which whisper-cli cannot do",
            how=SERVER_HINT,
        )
    language = args.language or settings.language or "auto"
    engine = whispercpp.WhisperCppBackend(settings.whispercpp_root)
    wanted = [_settled_model(args, settings)]
    drafting = args.draft and _the_draft_is_installed(
        engine, args.draft_model, lambda said: status.note(said, quiet=args.quiet)
    )
    if drafting:
        wanted.append(args.draft_model)
    # Both model files resolved before either server starts. Otherwise a missing model is
    # discovered after `large-v3-turbo` has already spent ten seconds and two gigabytes
    # loading, and the run that was never going to work took the longest to say so.
    files = [engine.model_path(name) for name in wanted]
    loads = await _model_slots(
        list(zip(wanted, files, strict=True)), lambda said: status.note(said, quiet=args.quiet)
    )
    # The outer block releases the slots, the inner one stops the servers inside it, so a
    # slot goes only after its model has left memory — and on every way out, including a
    # load that failed.
    try:
        # Said after the files are found, not before: "loading models" followed immediately by
        # "that model has no whisper.cpp build" reads like something went wrong during a load
        # that never started.
        # In the order they are loaded, which is the order they finish in. The note used to
        # reverse them, so the model somebody was waiting on was named second.
        status.note("loading " + " and ".join(load.name for load in loads), quiet=args.quiet)
        threads = settings.threads if args.threads is None else args.threads
        started = await _load_together(binary, loads, language, threads)
        try:
            yield Models(settled=started[0], draft=started[1] if len(started) > 1 else None)
        finally:
            await asyncio.gather(*(server.stop() for server in started))
    finally:
        for load in loads:
            model_gate.release(load.slot)


@dataclass(frozen=True, slots=True)
class _Load:
    """One model about to be loaded: its name, its file, and the slot it loads under."""

    name: str
    file: Path
    slot: model_gate.Slot


async def _model_slots(models: list[tuple[str, Path]], note: Callable[[str], None]) -> list[_Load]:
    """The models that will load, each with its machine-wide slot (`model_gate`), settled first.

    The settled model waits for its slot like any other model. The draft takes one only if
    one is free right now, and is left out if not: waiting for it would be waiting while
    holding the settled model's slot, and when that is the slot being waited for — a machine
    allowed one model of this size, RIG_GPU_SLOTS=1, a wrapper's single slot — the session
    would wait half an hour on itself and then not dictate at all. The draft is a
    convenience (see `_the_draft_is_installed`); dictating without it is the better trade.

    Taken here, before either server starts, rather than by each server as it starts: two
    loads racing for one slot could hand it to the draft and leave the settled model waiting.
    """
    (settled, settled_file), *drafts = models
    slot = await model_gate.acquire(settled_file.stat().st_size, what=settled)
    loads = [_Load(settled, settled_file, slot)]
    try:
        for name, file in drafts:
            spare = model_gate.attempt(file.stat().st_size)
            if spare is None:
                note(f"no free speech-model slot for {name}, so no fast first guess this time")
                break
            loads.append(_Load(name, file, spare))
    except BaseException:
        for load in loads:
            model_gate.release(load.slot)
        raise
    return loads


async def _load_together(
    binary: Path, loads: list[_Load], language: str, threads: int
) -> list[WhisperServer]:
    """Both models at once, and neither left running if the other cannot start.

    They used to load one after the other, so the wait was the SUM: `large-v3-turbo` takes
    about ten seconds and `base` follows it, for no reason but the order of a loop. They are
    separate processes reading separate files, so overlapping them makes the wait the longer
    of the two instead — which matters because this wait is the whole of what somebody
    experiences before dictation will listen to them.

    The failure path is why this is not a one-line `gather`. If one model fails while the
    other is still loading, the one that succeeded is a process holding a gigabyte open with
    nobody left to use it, so every server that did start is stopped before the failure is
    allowed out.
    """
    loading = [
        asyncio.create_task(_load(binary, load.file, load.slot, language, threads))
        for load in loads
    ]
    done = await asyncio.gather(*loading, return_exceptions=True)
    started = [server for server in done if isinstance(server, WhisperServer)]
    failed = [outcome for outcome in done if isinstance(outcome, BaseException)]
    if failed:
        await asyncio.gather(*(server.stop() for server in started))
        raise failed[0]
    return started


def _the_draft_is_installed(
    engine: whispercpp.WhisperCppBackend, model: str, note: Callable[[str], None]
) -> bool:
    """Is the fast model on disk? If not, say so and carry on without it.

    `stt setup` downloads the one model it was asked for, so somebody who accepted the
    default has `large-v3-turbo` and no `base` — and the first `stt mic` they ever ran failed
    before opening the microphone, over a model they had never been told they needed. The
    draft pass is a convenience; refusing to dictate at all because the convenience is
    missing is the wrong trade. A missing ACCURATE model is still fatal, because without it
    there is nothing to dictate with.
    """
    try:
        engine.model_path(model)
    except EngineError:
        # Only a model that EXISTS and is not downloaded. A name that is not a model at all,
        # or one with no whisper.cpp build, raises something else and is left to reach the
        # user: telling somebody to `stt models pull definitely-not-a-model` is worse than
        # telling them nothing, because it reads like the answer.
        note(f"no {model} model, so no fast first guess — `stt models pull {model}` adds one")
        return False
    return True


def _settled_model(args: argparse.Namespace, settings: config.Settings) -> str:
    return str(args.model or settings.model)


async def _load(
    binary: Path, model: Path, slot: model_gate.Slot, language: str, threads: int
) -> WhisperServer:
    """One `whisper-server`, holding one model file open for the rest of the session.

    `threads` is the user's configured value, and zero keeps the meaning it has everywhere
    else in stt: let the engine decide. It used to be turned into four — so somebody who had
    set eight got four, and somebody who asked for the engine default got four as well, with
    two servers between them oversubscribing the machine.
    """
    voice = Voice(model=model, language=language, threads=threads)
    return await WhisperServer.start(binary, voice, slot)


async def _dictate_until_stopped(
    args: argparse.Namespace,
    device: capture.Device,
    models: Models,
    settings: config.Settings,
) -> str:
    """Open the keyboard watcher and the microphone, and run until one of them stops."""
    line = status.Line(enabled=not args.quiet)
    marker = tap.new_marker()
    report, idle = _reporting(line)
    dictating = Session(
        typist=Typist(keys=_Keys(marker=marker)),
        settled=models.settled,
        draft=models.draft,
        # The same two settings the file pipeline reads. Somebody who turned the dictionary
        # off to keep the spelling they dictated was having it corrected anyway, and having
        # their glossary sent to the models on top.
        terms=dictionary.load() if settings.dictionary else dictionary.Dictionary(),
        bias=bool(settings.dictionary and settings.dict_bias),
        # The user's own always-drop patterns, which file transcription has always read and
        # live dictation was not. Somebody who wrote down a phrase they never want to see
        # was watching stt type it into their window.
        refuse=phrases.compile_all(phrases.load_user_patterns(config.app_home()))[0],
        idle_after=_idle_seconds(args),
        report=report,
        gate=gate_for(settings),
    )
    watcher = _watch(dictating, marker)
    try:
        status.note(
            f"listening on {device.name} — {status.stop_hint('Ctrl-C')}",
            quiet=args.quiet,
        )
        mic = asyncio.create_task(
            _through_the_microphone(
                device,
                dictating,
                line,
                idle,
                quiet=args.quiet,
                dictionary_enabled=settings.dictionary,
            )
        )
        notif = asyncio.create_task(
            status.stop_notification("listening — click Stop to finish", quiet=args.quiet)
        )
        try:
            return await _race_burst_signals(dictating, mic, notif)
        finally:
            await status.close_stop_notification(quiet=args.quiet)
    finally:
        watcher.stop()
        line.clear()
        await status.closed(said=dictating.transcript, quiet=args.quiet)


def gate_for(settings: config.Settings) -> Gate:
    """The speech detector, at this machine's threshold rather than the built-in one.

    Zero means nobody has chosen, which is not the same as somebody choosing zero — that
    would open the gate on every frame and hand the room to the model continuously.
    """
    # Anything not above zero means nobody chose, INCLUDING a negative one: a hand-edited
    # `"mic_threshold": -5` is not a quieter threshold, it is a value below every level a
    # microphone can produce, and honouring it would open the gate on the room continuously.
    # Asked here rather than in the config layer because "can this be negative" is a question
    # about the setting, and the shared coercion path knows nothing about microphones.
    if settings.mic_threshold > 0:
        return Gate(minimum=settings.mic_threshold)
    return Gate()


# Twice a second — the conventional blink rate of a text-input caret, chosen so the idle
# invitation reads as "waiting for you" the same way any other text field on the machine
# does, without being fast enough to be distracting.
_BLINK_SECONDS = 0.5


@dataclass
class _Idle:
    """What `_blink_while_idle` needs to know about the CURRENT `Progress`, kept up to date
    by `_reporting`'s wrapper — not a second subscription, just one more thing the one
    `Session.report` callback already does on every call. `hint` is set once by
    `_through_the_microphone` right after it computes it, not by `report` — the hint never
    changes mid-burst, only whether it should currently be SHOWN does."""

    listening: bool = True
    text: str = ""
    hint: str | None = None


def _reporting(line: status.Line) -> tuple[Callable[[Progress], None], _Idle]:
    """Build the `Session.report` callback AND the `_Idle` state `_blink_while_idle` reads,
    so both call sites (the ad-hoc path and the daemon's `_burst`) wire the same tracking
    once instead of by hand twice."""
    idle = _Idle()

    def report(progress: Progress) -> None:
        idle.listening = progress.listening
        idle.text = progress.text
        line.show(_paint(progress, idle.hint))

    return report, idle


async def _blink_while_idle(line: status.Line, idle: _Idle, hint: str) -> None:
    """Redraw the idle invitation on a fixed interval, alternating the cursor — the one
    place `stt mic` genuinely needs a periodic redraw instead of an event-driven one:
    nothing else reports anything while nobody has spoken yet (`Session._begin` is the next
    report, and only speech triggers it), so the line would otherwise sit frozen from the
    moment the microphone opens until the first word. Reads `idle` fresh every tick instead
    of a snapshot: the instant a real `Progress` with text arrives this simply stops
    drawing over it on its own, and resumes between sentences within the same burst — no
    cancellation race with `Session.report` to get right, only a read of a plain attribute.
    Cancelled by the caller when the burst ends; ``return``s on its own once `listening`
    goes false first, as a second line of defence.
    """
    on = True
    while True:
        await asyncio.sleep(_BLINK_SECONDS)
        if not idle.listening:
            return
        if idle.text:
            continue
        on = not on
        line.show(status.render(listening=True, text="", settled=False, cursor=on, hint=hint))


async def _through_the_microphone(
    device: capture.Device,
    dictating: Session,
    line: status.Line,
    idle: _Idle,
    *,
    quiet: bool,
    dictionary_enabled: bool,
    interruptible: bool = True,
) -> str:
    """The microphone stays open for exactly as long as this block runs.

    ``interruptible=False`` skips installing the per-burst SIGINT handler: the daemon (see
    `_idle_loop`) installs ONE handler for its whole process lifetime instead, and a second
    `add_signal_handler` here would silently replace it, then `_interruptible`'s own
    `remove_signal_handler` on exit would delete it outright — leaving a later Ctrl-C/
    `stt mic stop` unhandled after the first burst. The ad-hoc single-session path keeps the
    default: there the burst IS the whole process, so installing it here is correct.
    """
    async with capture.listening(device) as audio:
        ctx = _interruptible(dictating) if interruptible else contextlib.nullcontext()
        with ctx:
            # Computed once per burst, not per redraw: `example_hint()` reads the user's
            # dictionary from disk, and the blink below repaints twice a second. Skipped
            # entirely when the line is disabled (quiet, or no real terminal) — there is
            # nothing for either to draw.
            hint = (
                status.example_hint(dictionary_enabled=dictionary_enabled) if line.enabled else None
            )
            idle.hint = hint
            line.show(status.render(listening=True, text="", settled=False, hint=hint))
            # Announced beside the session, not in front of it. The sound and the banner
            # are each a subprocess, and `afplay` alone takes a second and a half to play a
            # noise a fifth of that long — waited on, that is a second and a half of somebody
            # talking into a microphone that is open but not being read yet.
            saying = asyncio.create_task(status.opened(device.name, quiet=quiet))
            blink = asyncio.create_task(_blink_while_idle(line, idle, hint)) if hint else None
            try:
                return await dictating.run(audio)
            finally:
                if blink is not None:
                    blink.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await blink
                await saying


async def _race_burst_signals(
    dictating: Session, mic: asyncio.Task[str], *signals: asyncio.Task[bool]
) -> str:
    """Run ``mic`` (the open-microphone loop) to completion, calling ``dictating.stop()``
    the moment any of ``signals`` resolves ``True`` — a GENUINE request to stop, not merely
    "finished": `status.stop_notification` can finish ``False`` (dismissed, closed, or
    `terminal-notifier` not installed), none of which mean stop, so a false result is
    dropped and whatever is left keeps racing rather than ending the burst early. Every
    signal task still pending once the race is decided is cancelled and awaited so nothing
    outlives the burst that started it — an outstanding notification subprocess left running
    past its burst would show a Stop button that does nothing (or worse, stops the NEXT
    one). ``mic`` itself is never cancelled; it is always awaited to its own real end, so a
    stop signal always gets the same graceful wind-down (the sentence in flight settles, the
    transcript is returned) that the microphone loop's own natural endings get.
    """
    pending: set[asyncio.Task[Any]] = {mic, *signals}
    while True:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        if mic in done:
            break
        if any(finished.result() for finished in done):
            if not mic.done():
                dictating.stop()
            break
        # Every task in `done` resolved False — dropped, not a stop request. `pending` (from
        # `asyncio.wait`) still holds `mic` and whatever signals have not resolved yet.
    for task in pending:
        if task is not mic:
            task.cancel()
    for task in pending:
        if task is not mic:
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return await mic


@contextlib.contextmanager
def _interruptible(dictating: Session) -> Iterator[None]:
    """Make Ctrl-C mean "finish", not "throw away what I just dictated".

    Left alone, SIGINT raises `KeyboardInterrupt` out of `asyncio.run` — the microphone is
    closed and the models are unloaded, correctly, and the sentences already spoken are
    discarded along with them. Handled on the loop it just stops the session, which lets the
    last sentence settle and the transcript print. Both ways out then behave the same, which
    matters because Ctrl-C is what people reach for in a terminal and Escape is what the
    terminal cannot see when the focus is somewhere else.
    """
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, dictating.stop)
    except (NotImplementedError, RuntimeError):  # pragma: no cover - not the main thread
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGINT)


def _idle_seconds(args: argparse.Namespace) -> float:
    """How long the session sits through silence before deciding it was forgotten."""
    minutes = IDLE_MINUTES if args.idle_minutes is None else args.idle_minutes
    return max(0.0, float(minutes)) * 60


def _paint(progress: Progress, hint: str | None = None) -> str:
    """`Session.report`'s frame, through `status.render` — the idle frame (no draft yet,
    still listening) carries `hint` too, the same as the burst-opening frame and every
    blink tick: without this, the settle-then-pause `Progress` `Session._settle_each` sends
    (see its own comment) painted the idle head with no hint at all, and the example phrase
    visibly blinked OUT the instant a sentence settled before the next blink tick redrew it
    back in. Caught in review before merge."""
    if progress.listening and not progress.text:
        return status.render(listening=True, text="", settled=False, hint=hint)
    return status.render(listening=progress.listening, text=progress.text, settled=progress.settled)


class _Keys:
    """The typist's keyboard: real synthesized keystrokes, stamped so the tap ignores them."""

    def __init__(self, marker: int) -> None:
        self.marker = marker

    def type_text(self, text: str) -> None:
        quartz.type_text(text, marker=self.marker)

    def press_backspace(self, times: int) -> None:
        quartz.press_backspace(times, marker=self.marker)


class _Stopper:
    """Turns the watcher's stream of key presses into the one thing the session cares about
    on ANY key: "stop correcting what you typed". The key code goes no further than this
    object — see the note in `tap.py` about what this must never become.

    An earlier version also ended the WHOLE session on two Escapes pressed within half a
    second — removed (ticket #79). It was the only stop mechanism the foreground/ad-hoc
    path had, and it was a real hazard: Escape is pressed constantly for reasons that have
    nothing to do with dictation (closing a dropdown, cancelling a browser action, backing
    out of a menu), so two of them landing in that window by pure coincidence silently ended
    a session with no warning, and the SAME Escape could also cancel whatever unrelated
    thing the user was doing elsewhere (Escape resetting a half-filled browser form is the
    scenario that got this removed). Ending a burst is now always one of: the hotkey firing
    again (press-style), the hotkey being released (hold-style), or the stop notification's
    button — see `_race_burst_signals` and `_await_release` — never a key this object sees.
    """

    def __init__(self, dictating: Session) -> None:
        self._session = dictating

    def __call__(self, code: int) -> None:
        del code  # every key means the same thing here; see the class docstring
        self._session.interrupt()


def _watch(dictating: Session, marker: int) -> tap.Watcher:
    """Start the keyboard watcher, marshalling its thread's events onto the event loop."""
    stopper = _Stopper(dictating)

    def arrived(code: int) -> None:
        """Act on the key press HERE, on the tap's own thread, and not a moment later.

        This used to hand the press to the event loop with `call_soon_threadsafe`, which
        reads as the careful thing to do and was the wrong thing. The loop is what performs
        the synthetic edits, so a click arriving while it was already about to type left the
        edit ahead of the interrupt in the queue: text going into the window the user had
        just clicked into. "Ownership ends the instant the user clicks" has to mean this
        instant, not the next time the loop looks at its queue.

        Doing it here is safe because of what it does: `_Stopper` sets two attributes and adds
        an integer to a set, each of which is a single bytecode step. It touches no loop
        machinery, so there is nothing to be woken up and nothing to raise if the loop has
        already gone away — which was the other reason the indirection was there.
        """
        stopper(code)

    watcher = tap.Watcher(marker=marker, on_key=arrived)
    watcher.start()
    if watcher.failure:
        # Stopped before the error goes up, because nothing above will ever see this object
        # again — the `finally` that normally stops it only runs once it has been returned.
        watcher.stop()
        raise EngineError(
            what="stt cannot tell when you type",
            why=watcher.failure,
            how="without it stt could delete text it did not write, so it will not start",
        )
    return watcher


# ---------------------------------------------------------------------------------------
# The daemon: idle with the microphone closed until a hotkey fires, dictate, repeat.
# `stt mic run`/`start` reach this instead of `dictate`; the ad-hoc path above is unchanged.
# ---------------------------------------------------------------------------------------


async def dictate_daemon(args: argparse.Namespace) -> None:
    """The daemon behind `stt mic run`/`start`: idle until a hotkey, one burst per press.

    Both models load exactly once, here, at daemon start — the 30-45 second wait a cold
    ad-hoc `stt mic` pays happens a single time instead of before every dictation. From then
    on the daemon sits with the microphone CLOSED (nothing is captured while idle — the same
    "a microphone left open is a room being recorded" rule the ad-hoc path keeps) until
    EITHER configured hotkey fires (both `hotkey_press` and `hotkey_hold` can be set and
    live at once — see `hotkey.py`); each activation opens the microphone for one burst,
    reusing the exact loop `dictate` uses for its single session, and returns to idle when
    that SAME binding says so again (another press, for a press-armed burst) or is released
    (a hold-armed one) — never by exiting. Only `stt mic stop` (SIGTERM) or Ctrl-C ends the
    process, and a burst already in flight is allowed to finish (idle-timeout still applies
    to it) before it does.
    """
    await _permissions()
    try:
        settings = config.load_settings().merged(
            hotkey_press=args.hotkey_press, hotkey_hold=args.hotkey_hold
        )
        press, hold = _resolved_hotkeys(settings)
        device = await capture.resolve(args.device)
        with _the_only_session():
            async with _models(args, settings) as models:
                await _idle_loop(args, device, models, settings, press, hold)
    finally:
        permissions.clear_state()


async def _permissions() -> None:
    """Accessibility and the microphone, asked for by THIS process and waited for — for the
    launchd daemon only (#98). It is the one process macOS attributes the request to the
    right principal for (see `permissions.py`), nobody is at a terminal to read a refusal,
    and exiting instead of waiting is what had launchd restart it every ten seconds
    forever. Started from a terminal — `stt mic run`, `start`'s detached child — the
    terminal is the principal and someone (or a script, or an ssh session nobody can click
    for) is waiting on an answer, so the immediate refusal naming the terminal stands, as
    it does off macOS."""
    if sys.platform != "darwin" or not permissions.under_launchd(_daemon_label()):
        quartz.require_accessibility()
        return
    await permissions.await_grants(
        permissions.default_grants(),
        principal=permissions.principal_name(),
        key=permissions.principal_key(),
        note=lambda line: status.note(line, quiet=False),
    )


def _daemon_label() -> str | None:
    """The launchd label `stt mic enable` installs, or None without the service lib — in
    which case nothing could have installed a launchd job to be running under."""
    try:
        from .mic_service import mic_service

        return str(mic_service().label)
    except ImportError:
        return None


def _resolved_hotkeys(settings: config.Settings) -> tuple[hotkey.Combo | None, hotkey.Combo | None]:
    """`hotkey.resolve_press`/`resolve_hold`, but a bad spec — or both bindings sharing a
    KEYCODE — becomes a diagnosed `UsageError` instead of a raw exception or a silently
    unendable hold burst. `--hotkey-press`/`--hotkey-hold` are already validated at the
    argparse layer (see `commands/mic.py`), and the same collision is already refused at
    `stt config set` time (see `hotkey.validate`), so this exists for the path neither can
    reach: a hand-edited `config.json` whose stored `hotkey_press`/`hotkey_hold` reached one
    of those states anyway. Uncaught, that crash is worse than an ordinary one — `stt mic
    enable` bakes the daemon's argv into a launchd `LaunchAgent` with `KeepAlive`, so a
    crash on every start is a restart loop, not a one-time failure.
    """
    try:
        press = hotkey.resolve_press(settings.hotkey_press)
        hold = hotkey.resolve_hold(settings.hotkey_hold)
    except hotkey.HotkeyError as broken:
        raise UsageError(
            what="the configured hotkey is invalid",
            why=str(broken),
            how="fix it with `stt config set hotkey_press ...` / "
            "`stt config set hotkey_hold ...`, or `stt config unset` either to go back to "
            "its default",
        ) from broken
    if press is None and hold is None:
        raise UsageError(
            what="no hotkey is configured",
            why="hotkey_press and hotkey_hold are both disabled, so the daemon would idle "
            "forever with nothing that could ever open the microphone",
            how="`stt config unset hotkey_press` to restore the default (cmd+shift+space), "
            "or `stt config set hotkey_press ...`/`hotkey_hold ...` to choose one yourself",
        )
    if press is not None and hold is not None and hotkey.same_keycode(press, hold):
        raise UsageError(
            what="hotkey_press and hotkey_hold use the same key",
            why=f"{press.spec!r} and {hold.spec!r} both use the same physical key — a key "
            "release only reports which key was let go, not which modifiers were held with "
            "it (see `hotkey.same_keycode`), so releasing either could be misattributed to "
            "the other binding and leave the microphone open indefinitely",
            how="pick two different keys for hotkey_press and hotkey_hold, or `stt config "
            "set hotkey_hold none` (or hotkey_press) to disable the one you do not need",
        )
    return press, hold


class _HotkeyBus:
    """Marshals the tap thread's hotkey presses onto the event loop as burst start/stop
    edges, tagged with WHICH configured binding (`hotkey_press` or `hotkey_hold`) fired.

    `_Stopper`/`arrived` above run same-thread on purpose — a correction has to win a race
    against an edit already queued on the loop. A hotkey activation is a different kind of
    event: it starts or stops a whole *session* (opens the microphone, creates tasks), which
    only the loop can do, so handing it across with `call_soon_threadsafe` is the right tool
    here, not a repeat of the mistake `arrived`'s docstring warns about.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.events: asyncio.Queue[tuple[hotkey.Combo, bool]] = asyncio.Queue()

    def __call__(self, combo: hotkey.Combo, pressed: bool) -> None:
        self._loop.call_soon_threadsafe(self.events.put_nowait, (combo, pressed))


class _Active:
    """The one burst currently open, if any — so a signal or the tap can reach it.

    `session` and `stopper` are only ever meaningful together: `stopper` is a `_Stopper`
    wrapping the exact `Session` `session` points at, one built from the other in the same
    breath. Deliberately not a `@dataclass`: that annotation style generates a positional
    constructor, which would let two unrelated values be paired into one `_Active` with no
    relationship between them — exactly the unpaired state this class exists to make
    unreachable. `open` (which also asserts at most one burst is active at a time) and
    `close` are the ONLY way either field changes — outside this class, `active.stopper =
    ...`/`active.session = ...` raises `AttributeError` at runtime (a property with no
    setter), not just a mypy error.
    """

    def __init__(self) -> None:
        self._stopper: _Stopper | None = None
        self._session: Session | None = None

    def __repr__(self) -> str:
        return f"_Active(open={self._session is not None})"

    @property
    def stopper(self) -> _Stopper | None:
        return self._stopper

    @property
    def session(self) -> Session | None:
        return self._session

    def open(self, session: Session) -> None:
        assert self._session is None, "a burst opened while another was still active"
        # Built before either field is published: `_Stopper(session)` cannot raise today,
        # but if it ever grew work that could, raising between the two assignments below
        # would leave `_session` set and `_stopper` None — the exact unpaired state this
        # class exists to make unreachable.
        stopper = _Stopper(session)
        self._session = session
        self._stopper = stopper

    def close(self) -> None:
        # `stopper` first: `arrived` (the tap thread) reads only `stopper`, so clearing it
        # first shrinks that cross-thread reader's window as early as possible. `session`
        # second: `_stop_everything` (the loop thread, no race to shrink) only reads
        # `session`, so there is nothing to gain — and something to lose — clearing it first.
        self._stopper = None
        self._session = None


@dataclass
class _BurstContext:
    """Everything one burst needs, fixed for the whole daemon's lifetime.

    `_burst` used to take these eight as separate positional parameters — a call site that
    means nothing without the signature open beside it, and a shape where adding a ninth is
    an eight-argument diff. `_idle_loop` builds exactly one of these, once, and every burst
    the daemon ever runs shares it.
    """

    args: argparse.Namespace
    device: capture.Device
    models: Models
    settings: config.Settings
    marker: int
    active: _Active
    bus: _HotkeyBus
    press: hotkey.Combo | None
    hold: hotkey.Combo | None


async def _idle_loop(
    args: argparse.Namespace,
    device: capture.Device,
    models: Models,
    settings: config.Settings,
    press: hotkey.Combo | None,
    hold: hotkey.Combo | None,
) -> None:
    """Idle with the microphone closed until either configured hotkey fires, dictate, and
    repeat. At least one of ``press``/``hold`` is non-``None`` — `_resolved_hotkeys` refuses
    to return both `None`."""
    loop = asyncio.get_running_loop()
    marker = tap.new_marker()
    shutdown = asyncio.Event()
    bus = _HotkeyBus(loop)
    active = _Active()
    ctx = _BurstContext(
        args=args,
        device=device,
        models=models,
        settings=settings,
        marker=marker,
        active=active,
        bus=bus,
        press=press,
        hold=hold,
    )

    def _stop_everything() -> None:
        shutdown.set()
        if active.session is not None:
            active.session.stop()

    # ONE handler for the daemon's whole lifetime, not per burst — see the `interruptible`
    # note on `_through_the_microphone`. `stt mic stop` sends SIGTERM; Ctrl-C (SIGINT) is the
    # same "finish, don't discard" request `_interruptible` gives the ad-hoc path.
    handled = [
        sig for sig in (signal.SIGTERM, signal.SIGINT) if _try_handle(loop, sig, _stop_everything)
    ]
    hotkeys = [combo for combo in (press, hold) if combo is not None]
    watcher = _watch_daemon(marker, hotkeys, bus, active)
    try:
        permissions.mark_ready()
        status.note(
            f"stt mic daemon ready — {_ready_hint(press, hold)}; `stt mic stop` to quit"
            f"{_notification_absence_note()}",
            quiet=args.quiet,
        )
        while not shutdown.is_set():
            edge = await _next_hotkey_edge(bus, shutdown)
            if edge is None or shutdown.is_set():
                # The second check is belt-and-braces over `_next_hotkey_edge`'s own
                # shutdown-wins-the-race guard: a burst must never start once stop has been
                # asked for, on ANY path that could reach here — see that function.
                continue
            combo, pressed = edge
            if not pressed:
                continue  # a release while idle: nothing was armed, so nothing to end
            # `is`, not `==`: `Combo.__eq__` also compares `spec` (see `same_chord`'s own
            # docstring for why that matters elsewhere) and TWO valid, distinct configured
            # combos could in principle compare structurally equal in some future change to
            # how they are constructed — `combo` here is always the very same object `hold`
            # already is (the tap hands back exactly the object it was given, see
            # `tap.Watcher._handle`), so identity is the actual invariant this classification
            # relies on, not mere structural equality. Found by review.
            kind: hotkey.BindingKind = "hold" if combo is hold else "press"
            # The burst's own transcript is deliberately NEVER logged here. `dictate`'s
            # ad-hoc path prints it to stdout for the human watching that one terminal;
            # `start`/`enable` run headless with stdout/stderr redirected to a PERSISTENT
            # logfile (`agenttools_service`'s detached spawner), so doing the same thing
            # here would silently write every word ever dictated — passwords included — to
            # a plaintext file with no rotation, regardless of `--quiet`. The burst already
            # typed the words where they were meant to go; the log only needs to know it
            # ran, which `status.opened`/`status.closed` (inside `_burst`) already say.
            try:
                await _burst(ctx, combo, kind)
            except Exception as failed:
                # A burst failing (the mic unplugged mid-recording, a decode error) must not
                # take the whole daemon down with it — this docstring promises only SIGTERM/
                # Ctrl-C end the process. Left uncaught, `stt mic enable`'s launchd unit
                # (`KeepAlive`) would restart the whole process — reloading both models —
                # for a failure that only needed to end one burst and return to idle.
                status.note(f"a burst ended in an error: {failed}", quiet=args.quiet)
    finally:
        for sig in handled:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        watcher.stop()


def _try_handle(
    loop: asyncio.AbstractEventLoop, sig: signal.Signals, handler: Callable[[], None]
) -> bool:
    try:
        loop.add_signal_handler(sig, handler)
    except (NotImplementedError, RuntimeError):  # pragma: no cover - not the main thread
        return False
    return True


async def _next_hotkey_edge(
    bus: _HotkeyBus, shutdown: asyncio.Event
) -> tuple[hotkey.Combo, bool] | None:
    """The next hotkey edge while idle — ``(combo, pressed)`` — or ``None`` if shutdown won
    the race first. A release edge (``pressed=False``) is returned rather than swallowed
    here; `_idle_loop` is what decides a release means "keep idling" (only a press starts a
    burst), the same way it always has.

    Shutdown ALWAYS wins a simultaneous completion, checked first and unconditionally — a
    security review found that a hotkey press already sitting in the queue when `stt mic
    stop`/Ctrl-C arrives could resolve `get_task` and `shutdown_task` in the very same
    `asyncio.wait`, and the original code checked `get_task` regardless of whether shutdown
    ALSO completed. That started a burst — opened the microphone — after the user had just
    asked the daemon to stop, the exact "a microphone left open is a room being recorded"
    failure this whole design exists to prevent.
    """
    get_task = asyncio.create_task(bus.events.get())
    shutdown_task = asyncio.create_task(shutdown.wait())
    try:
        done, _pending = await asyncio.wait(
            {get_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if shutdown_task in done:
            return None
        return get_task.result() if get_task in done else None
    finally:
        for task in (get_task, shutdown_task):
            if not task.done():
                task.cancel()


async def _burst(ctx: _BurstContext, armed: hotkey.Combo, kind: hotkey.BindingKind) -> str:
    """One activation: open the microphone, dictate, close it, and hand control back to
    idle. ``armed`` is the specific chord that started THIS burst and ``kind`` is which
    binding it came from (`hotkey_press` arms "press", `hotkey_hold` arms "hold") — both
    fixed for the burst's whole lifetime, so a burst is never ended by the OTHER binding
    firing mid-burst (see `_await_release`).
    """
    line = status.Line(enabled=not ctx.args.quiet)
    report, idle = _reporting(line)
    dictating = Session(
        typist=Typist(keys=_Keys(marker=ctx.marker)),
        settled=ctx.models.settled,
        draft=ctx.models.draft,
        terms=dictionary.load() if ctx.settings.dictionary else dictionary.Dictionary(),
        bias=bool(ctx.settings.dictionary and ctx.settings.dict_bias),
        refuse=phrases.compile_all(phrases.load_user_patterns(config.app_home()))[0],
        idle_after=_idle_seconds(ctx.args),
        report=report,
        gate=gate_for(ctx.settings),
    )
    ctx.active.open(dictating)
    try:
        hint = _off_hint(kind, armed)
        status.note(f"listening on {ctx.device.name} — {hint}", quiet=ctx.args.quiet)
        mic = asyncio.create_task(
            _through_the_microphone(
                ctx.device,
                dictating,
                line,
                idle,
                quiet=ctx.args.quiet,
                dictionary_enabled=ctx.settings.dictionary,
                interruptible=False,
            )
        )
        off = asyncio.create_task(_await_release(ctx.bus, armed, kind))
        notif = asyncio.create_task(
            status.stop_notification(f"listening — {hint}", quiet=ctx.args.quiet)
        )
        try:
            return await _race_burst_signals(dictating, mic, off, notif)
        finally:
            await status.close_stop_notification(quiet=ctx.args.quiet)
    finally:
        ctx.active.close()
        line.clear()
        await status.closed(said=dictating.transcript, quiet=ctx.args.quiet)


async def _await_release(bus: _HotkeyBus, armed: hotkey.Combo, kind: hotkey.BindingKind) -> bool:
    """Wait for the ONE edge that ends a burst armed by ``armed``/``kind``: another press of
    the SAME chord for a press-armed burst, a release of it for a hold-armed one. Any other
    edge — the OTHER configured binding firing mid-burst, a release the daemon never asked
    about, a spurious extra press mid-hold — is consumed here and ignored, rather than left
    in the queue to misfire the NEXT burst. Always resolves ``True``: this coroutine only
    ever completes on the edge that means stop, matching the ``bool``-signals contract
    `_race_burst_signals` races it under.
    """
    while True:
        combo, pressed = await bus.events.get()
        if combo != armed:
            continue  # the OTHER binding (or a stray queued edge) — not this burst's to end
        if kind == "hold" and not pressed:
            return True
        if kind == "press" and pressed:
            return True


def _off_hint(kind: hotkey.BindingKind, combo: hotkey.Combo) -> str:
    action = f"release {combo.spec}" if kind == "hold" else f"press {combo.spec} again"
    return status.stop_hint(action)


def _notification_absence_note() -> str:
    """Appended to the daemon-ready line ONLY when `terminal-notifier` is missing. Alex's
    stated baseline expectation is a Stop button that is ALWAYS there (ticket #79), so its
    absence has to be a visible fact about THIS run, printed once before any burst — not
    something discovered only when a burst starts and no notification ever appears. Empty
    (no clause at all) when it IS available: a working optional feature needs no extra
    announcement beyond working, only a missing one does — `_off_hint`'s per-burst
    `status.stop_hint` already covers the "how to stop" line either way.
    """
    if status.notifier_available():
        return ""
    return f" (no notification Stop button — {status.INSTALL_HINT} adds one)"


def _ready_hint(press: hotkey.Combo | None, hold: hotkey.Combo | None) -> str:
    """What the daemon's startup line says is configured — one clause per active binding, so
    a person with only one of the two set never reads a hint for a binding that is not
    there."""
    clauses = []
    if press is not None:
        clauses.append(f"press {press.spec} to toggle dictation")
    if hold is not None:
        clauses.append(f"hold {hold.spec} to push-to-talk")
    return ", or ".join(clauses)


def _watch_daemon(
    marker: int, hotkeys: Sequence[hotkey.Combo], bus: _HotkeyBus, active: _Active
) -> tap.Watcher:
    """Start the ONE keyboard watcher the whole daemon shares across every burst, for
    whichever of `hotkey_press`/`hotkey_hold` are configured — one or both; ``hotkeys`` is
    never empty, since `_resolved_hotkeys` refuses to return both as `None`.

    Unlike the ad-hoc path's `_watch` (one `Watcher` per session), a daemon needs the tap
    running while idle too — that is how it ever sees the hotkey that starts the first burst.
    `on_key` (the correction-interrupt) is routed to whichever burst is currently open, if
    any; while idle there is nothing to interrupt, so a press is simply dropped.
    """

    def arrived(code: int) -> None:
        # Read ONCE into a local, not `if active.stopper is not None: active.stopper(code)`.
        # `arrived` runs on the tap's own background thread; `_burst`'s `finally` clears
        # `active.stopper` on the LOOP thread. Two separate reads of the same attribute let
        # the loop thread null it between the check and the call — a key pressed at the
        # exact instant a burst ends called `None(code)` inside the Quartz C callback,
        # which is about as bad a place for an unhandled `TypeError` as this file has.
        stopper = active.stopper
        if stopper is not None:
            stopper(code)

    watcher = tap.Watcher(marker=marker, on_key=arrived, hotkeys=hotkeys, on_hotkey=bus)
    watcher.start()
    if watcher.failure:
        watcher.stop()
        raise EngineError(
            what="stt cannot tell when you type",
            why=watcher.failure,
            how="without it stt could delete text it did not write, so it will not start",
        )
    return watcher
