"""permissions — the daemon asks macOS for its own grants, then waits for the click.

WHY THE DAEMON ASKS, NOT THE TERMINAL
    macOS files Accessibility and Microphone under the process it holds responsible for
    the request. A command typed in a terminal is attributed to the terminal application;
    the daemon `stt mic enable` hands to launchd is attributed to the interpreter launchd
    execs (a Homebrew ``python3.14``, typically). So a prompt raised from a terminal asks
    for the WRONG principal (#80, #98). The only process that can raise the right prompt is
    the daemon itself — which is what this module is for.

WHAT STAYS THE USER'S
    No API grants either permission (settled in #57, ``docs/accessibility.md``). The floor
    is one click per permission: the Accessibility toggle, and Allow on the Microphone
    request. Everything else — raising the prompt, opening the right pane of System
    Settings, noticing the grant, carrying on — happens here, with no command to rerun.
    (Accessibility is noticed from a fresh child process and picked up by the daemon
    replacing itself with a fresh copy: a process never sees its own grant change.)

NEVER SPAM
    A daemon that is not trusted used to exit, and launchd restarted it every ten seconds
    (7273 runs on one machine). This module keeps the process alive instead, and it asks
    each principal at most once: a ledger under ``STT_HOME`` records "asked", keyed by the
    real interpreter path, so a restart, a reboot or a crash never shows the prompt again.
    ``stt mic --fix`` clears the ledger (:func:`rearm`) — the one explicit way to ask twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from .. import config

Kind = Literal["accessibility", "microphone"]

# `AVAuthorizationStatus`. Only `AUTHORIZED` lets the capture hear anything; a denied
# microphone delivers pure silence rather than an error, which is why this is checked.
MIC_UNDETERMINED = 0
MIC_RESTRICTED = 1
MIC_DENIED = 2
MIC_AUTHORIZED = 3

# How often the waiting daemon re-reads its trust while someone is likely looking for the
# toggle — for a while after each prompt, or after `stt mic --fix` makes it ask again — and
# how often after that. Each read of Accessibility is a fresh child interpreter (~40 ms,
# see `accessibility_granted_now`), so a daemon nobody grants must not keep spawning one
# every two seconds for days.
POLL_SECONDS = 2.0
IDLE_POLL_SECONDS = 10.0
FAST_POLL_WINDOW_SECONDS = 300.0

_SETTINGS = "x-apple.systempreferences:com.apple.preference.security"
_PANES: dict[Kind, str] = {
    "accessibility": f"{_SETTINGS}?Privacy_Accessibility",
    "microphone": f"{_SETTINGS}?Privacy_Microphone",
}
_LEDGER = "mic-permission-prompts.json"
_STATE = "mic-daemon-state.json"

DaemonState = Literal["waiting-accessibility", "waiting-microphone", "starting", "ready"]
_STATES: tuple[DaemonState, ...] = get_args(DaemonState)


# ---------------------------------------------------------------------------------------
# Who macOS will hold responsible, in words the Settings list uses
# ---------------------------------------------------------------------------------------


def under_launchd(label: str | None) -> bool:
    """Is this process the launchd job ``label`` — the daemon `stt mic enable` installed?

    launchd puts the job's label in ``XPC_SERVICE_NAME`` for every process it spawns (seen
    in ``launchctl print``: ``XPC_SERVICE_NAME => com.agenttools.stt.mic``). A process a
    terminal started — `stt mic run`, or `start`'s detached child — inherits the terminal's
    responsibility instead, so its principal is the terminal, not the interpreter.
    """
    return label is not None and os.environ.get("XPC_SERVICE_NAME") == label


def principal_name() -> str:
    """The entry the user has to turn on for the launchd daemon, as System Settings lists
    it: the interpreter launchd execs, resolved like `principal_key` — a venv's `python` is
    a symlink, and macOS lists the binary it points to."""
    return f"'{Path(principal_key()).name}'"


def principal_key() -> str:
    """What the ledger keys "already asked" on: the REAL interpreter path. `brew upgrade`
    moves it (a new Cellar directory), and macOS treats the moved binary as a new
    principal with no grant — so it must be asked again, and a new key does exactly that.
    Only the launchd daemon asks (`dictation._permissions`), so a terminal's answer can
    never use up the daemon's one prompt."""
    return os.path.realpath(sys.executable)


# ---------------------------------------------------------------------------------------
# The microphone half — AVFoundation through ctypes, the same way quartz.py reaches Quartz
# ---------------------------------------------------------------------------------------


class _Block(ctypes.Structure):
    """A global Objective-C block literal: the completion handler
    ``requestAccessForMediaType:completionHandler:`` insists on. Global (flag bit 28), so
    copying it is a no-op and it never needs freeing; it lives for the process."""


class _BlockDescriptor(ctypes.Structure):
    _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]


_INVOKE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool)
_Block._fields_ = [
    ("isa", ctypes.c_void_p),
    ("flags", ctypes.c_int),
    ("reserved", ctypes.c_int),
    ("invoke", _INVOKE),
    ("descriptor", ctypes.POINTER(_BlockDescriptor)),
]
_BLOCK_IS_GLOBAL = 1 << 28


@dataclass
class _AVFoundation:
    objc: Any
    device_class: int
    audio: int
    status_sel: int
    request_sel: int
    # Kept referenced for the process lifetime: AVFoundation calls the handler on a thread
    # of its own, possibly after the call that passed it returned.
    handler: _Block | None = None


_AV: _AVFoundation | None = None


def _avfoundation() -> _AVFoundation:
    global _AV
    if _AV is None:
        ctypes.CDLL("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        core.CFStringCreateWithCString.restype = ctypes.c_void_p
        core.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        # `AVMediaTypeAudio` is the string "soun"; a CFString is toll-free an NSString.
        audio = core.CFStringCreateWithCString(None, b"soun", 0x08000100)  # UTF-8
        device_class = objc.objc_getClass(b"AVCaptureDevice")
        if not device_class or not audio:
            raise OSError("AVCaptureDevice is not available")
        _AV = _AVFoundation(
            objc=objc,
            device_class=device_class,
            audio=audio,
            status_sel=objc.sel_registerName(b"authorizationStatusForMediaType:"),
            request_sel=objc.sel_registerName(b"requestAccessForMediaType:completionHandler:"),
        )
    return _AV


def microphone_status() -> int | None:
    """``AVCaptureDevice authorizationStatusForMediaType:`` for audio, as THIS process's
    principal sees it — or ``None`` where it cannot be asked (not macOS)."""
    try:
        av = _avfoundation()
    except OSError:
        return None
    send = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
        ("objc_msgSend", av.objc)
    )
    return int(send(av.device_class, av.status_sel, av.audio))


def request_microphone() -> None:
    """Raise macOS's own Microphone request for this process's principal. Answers nothing:
    the answer arrives when the user clicks, and :func:`microphone_status` is re-read."""
    try:
        av = _avfoundation()
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        isa = ctypes.addressof(ctypes.c_void_p.in_dll(libsystem, "_NSConcreteGlobalBlock"))
    except (OSError, ValueError):
        return
    if av.handler is None:
        descriptor = _BlockDescriptor(0, ctypes.sizeof(_Block))
        av.handler = _Block(
            isa,
            _BLOCK_IS_GLOBAL,
            0,
            _INVOKE(lambda _block, _granted: None),
            ctypes.pointer(descriptor),
        )
    send = ctypes.CFUNCTYPE(
        None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )(("objc_msgSend", av.objc))
    send(av.device_class, av.request_sel, av.audio, ctypes.addressof(av.handler))


def microphone_granted() -> bool:
    """True unless macOS says this principal may NOT hear: a machine where the question
    cannot be asked at all gets True, so an unanswerable check never blocks the daemon."""
    found = microphone_status()
    return found is None or found == MIC_AUTHORIZED


# ---------------------------------------------------------------------------------------
# The ledger: "this principal was already asked" — the whole anti-spam guarantee
# ---------------------------------------------------------------------------------------


def _ledger_path() -> Path:
    return config.app_home() / _LEDGER


def _read_json(path: Path) -> dict[str, Any]:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _write_json(path: Path, content: dict[str, Any]) -> None:
    from ..archive import write_atomic

    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(content, indent=2, sort_keys=True) + "\n")


def claim_prompt(kind: Kind, principal: str) -> bool:
    """True once per (``kind``, ``principal``) until :func:`rearm`: the caller may raise the
    prompt now, and nobody after it may. Recorded BEFORE the prompt is shown, so a crash
    while it is on screen still counts as "asked". Unlocked: a `--fix` re-arm racing this
    write can at worst let one extra prompt through, which is what the re-arm asked for."""
    ledger = _read_json(_ledger_path())
    key = f"{kind}:{principal}"
    if key in ledger:
        return False
    ledger[key] = int(time.time())
    _write_json(_ledger_path(), ledger)
    return True


def rearm() -> None:
    """Forget every "already asked" — `stt mic --fix`, the explicit request to ask again."""
    with contextlib.suppress(FileNotFoundError):
        _ledger_path().unlink()


# ---------------------------------------------------------------------------------------
# The daemon's own state, for `stt mic status`, the banner and `--fix` to read
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateRecord:
    pid: int
    state: DaemonState
    principal: str


def _state_path() -> Path:
    return config.app_home() / _STATE


def write_state(state: DaemonState, principal: str) -> None:
    _write_json(_state_path(), {"pid": os.getpid(), "state": state, "principal": principal})


def mark_ready() -> None:
    """The daemon is listening for its hotkey — keeping the principal the wait recorded."""
    found = read_state()
    if found is not None and found.pid == os.getpid():
        write_state("ready", found.principal)


def clear_state() -> None:
    """Remove the record on the way out — only our own, never a successor's."""
    found = read_state()
    if found is not None and found.pid == os.getpid():
        with contextlib.suppress(FileNotFoundError):
            _state_path().unlink()


def read_state() -> StateRecord | None:
    raw = _read_json(_state_path())
    pid, principal = raw.get("pid"), raw.get("principal")
    state = next((known for known in _STATES if known == raw.get("state")), None)
    if not isinstance(pid, int) or state is None:
        return None
    return StateRecord(pid=pid, state=state, principal=str(principal or "stt"))


def state_for(pid: int | None) -> StateRecord | None:
    """The record, but only if it describes the live process ``pid`` — a record left by a
    process that has since died says nothing about the one running now."""
    found = read_state()
    return found if found is not None and pid is not None and found.pid == pid else None


def waiting_text(state: DaemonState, principal: str) -> str | None:
    """The one sentence every surface prints for a waiting daemon, or None if it is not."""
    if state == "waiting-accessibility":
        return (
            f"waiting for Accessibility — turn on {principal} in System Settings > "
            "Privacy & Security > Accessibility"
        )
    if state == "waiting-microphone":
        return (
            f"waiting for Microphone — click Allow for {principal} (or turn it on in "
            "System Settings > Privacy & Security > Microphone)"
        )
    return None


# ---------------------------------------------------------------------------------------
# The wait itself
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grant:
    """One permission: how to read it, and how to make macOS ask for it.

    ``recheck`` is for a permission whose in-process answer is frozen at its first read: the
    wait polls that instead of ``granted``, and restarts the process once it turns, so
    everything after the wait runs in a process that sees the grant itself."""

    kind: Kind
    state: DaemonState
    granted: Callable[[], bool]
    ask: Callable[[], None]
    recheck: Callable[[], bool] | None = None


def accessibility_granted_now() -> bool:
    """`quartz.trusted()` as a brand-new process sees it.

    A process reads its Accessibility answer once and keeps it. #98's live run: the daemon
    polled `AXIsProcessTrusted()` every two seconds, TCC.db recorded its toggle turned on
    at 12:10:51, and at 12:14 it was still waiting — it never saw the grant, and only the
    process started after the 12:47 reboot did. A child inherits this process's responsible
    principal and runs the same interpreter, so its fresh answer is about the same grant.
    """
    root = Path(__file__).resolve().parents[2]
    probe = (
        f"import sys; sys.path.insert(0, {str(root)!r}); "
        "from stt_cli.live import quartz; sys.exit(0 if quartz.trusted() else 1)"
    )
    try:
        found = subprocess.run(
            [sys.executable, "-I", "-c", probe], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return found.returncode == 0


# Set on the way into the fresh image, so it is restarted at most once per launch.
_RESTARTED = "STT_MIC_RESTARTED_FOR_PERMISSIONS"


def restart_self() -> None:
    """Replace this process with a fresh run of its own command line: the same pid, so
    launchd, the pidfile and the state record still describe it, and nothing it cached
    about its permissions survives. Once per launch — a fresh image that still reads "not
    granted" in-process carries on with the fresh-process answer rather than restarting
    every two seconds."""
    if os.environ.get(_RESTARTED):
        return
    os.environ[_RESTARTED] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def open_pane(kind: Kind) -> None:
    """Open System Settings at ``kind``'s list. Fire-and-forget, like the Finder reveal in
    `commands/archive.py`: a person is about to act on it, nothing here waits on it."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["open", _PANES[kind]], capture_output=True, timeout=10, check=False)


def default_grants(*, opener: Callable[[Kind], None] = open_pane) -> list[Grant]:
    """Accessibility first — without it nothing is typed and no hotkey is heard — then the
    microphone. Each ``ask`` shows the user the ONE thing to click."""
    from . import quartz

    def ask_accessibility() -> None:
        # The native prompt is what adds this principal to the Accessibility list at all
        # (the toggle has to exist to be clicked); the pane is where the toggle is.
        quartz.ask_for_trust()
        opener("accessibility")

    def ask_microphone() -> None:
        # Not asked yet: macOS's own dialog has the Allow button — one click, no pane.
        # Already refused: macOS never asks twice, so the toggle in the pane is the way.
        if microphone_status() == MIC_UNDETERMINED:
            request_microphone()
        else:
            opener("microphone")

    return [
        Grant(
            "accessibility",
            "waiting-accessibility",
            quartz.trusted,
            ask_accessibility,
            recheck=accessibility_granted_now,
        ),
        # Re-read live: the #98 daemon saw its Allow click two seconds after it.
        Grant("microphone", "waiting-microphone", microphone_granted, ask_microphone),
    ]


async def await_grants(
    grants: list[Grant],
    *,
    principal: str,
    key: str,
    note: Callable[[str], None],
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    restart: Callable[[], None] = restart_self,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Return once every grant holds, asking for each at most once per principal.

    Stays alive meanwhile — the alternative, exiting, is what made launchd restart the
    daemon every ten seconds forever. The ledger is re-read on every tick, so `stt mic
    --fix` clearing it makes this very process ask again within one tick, no restart. It
    polls every two seconds for five minutes after the wait starts or a prompt is shown —
    when someone is looking for the toggle — and every ten seconds after that.
    """
    for grant in grants:
        if grant.granted():
            continue
        write_state(grant.state, principal)
        note(waiting_text(grant.state, principal) or f"waiting for {grant.kind}")
        poll = grant.recheck or grant.granted
        looking_since = clock()
        while not poll():
            if claim_prompt(grant.kind, key):
                grant.ask()
                looking_since = clock()
            looking = clock() - looking_since < FAST_POLL_WINDOW_SECONDS
            await sleep(POLL_SECONDS if looking else IDLE_POLL_SECONDS)
        note(f"{grant.kind} granted to {principal} — carrying on")
        if grant.recheck is not None:
            restart()
    write_state("starting", principal)
