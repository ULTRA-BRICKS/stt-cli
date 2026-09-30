"""tap — watch the real keyboard, so live dictation knows when to stop rewriting.

THE PROBLEM THIS SOLVES
    Live dictation improves what it already typed by deleting it and typing it again. That
    is safe exactly as long as the characters to the left of the caret are still the ones it
    put there. The moment the user types a word themselves, or clicks somewhere else, those
    backspaces would eat something that was never ours.

    So the session needs to know the instant a human touches the keyboard. A listen-only
    event tap is the only way macOS offers to find that out, and it costs nothing: the tap
    observes, it never modifies or swallows an event.

TELLING OUR OWN KEYSTROKES APART
    Everything `quartz.type_text` sends comes straight back through this tap — it is a real
    system-wide keyboard event, which is the whole point of it. Each one carries the marker
    stamped into `kCGEventSourceUserData`, and this module ignores anything wearing it. A
    marker that matched by accident would make stt deaf to the user, so it is a random 63-bit
    number chosen per session rather than a constant.

WHAT THIS TAP IS NOT ALLOWED TO BECOME
    A system-wide key-down tap is a keylogger with a different name. The defence that
    actually holds is that a key code never leaves this file for anything but a comparison:
    the session is told THAT a key was pressed and, for the hotkeys it listens for (a
    press-style, a hold-style and a stop binding may all be configured at once), WHICH ONE —
    each supplied by the caller, compared here (`Holds`) and reported only as
    pressed/released, the same rule the original single hotkey (Escape) always followed.
    Nothing here writes a key code to a log, a file, or the archive, and nothing may start
    doing so. There is no reason live dictation needs the content of what the user typed by
    hand, and every reason it must not have it.

RELEASES THAT DO NOT COME AS A KEY-UP
    A hold binding ends when it is let go, and a key-up is not the only way to let go. The
    hand can release a chord's modifier first while the key itself stays down, and `fn`
    alone has no key at all. Both arrive as flagsChanged events, which the tap asks for
    whenever a hotkey is configured; `Holds` turns them into the same pressed/released edges
    a key produces.
"""

from __future__ import annotations

import ctypes
import secrets
import threading
from collections.abc import Callable, Sequence

from .hotkey import RELEVANT_FLAGS, Combo
from .quartz import _Frameworks, frameworks

_SESSION_TAP = 1
_HEAD_INSERT = 0
_LISTEN_ONLY = 1
_KEY_DOWN = 10
# Only added to the tap's event mask when a hotkey is configured (see `_run`) — a plain
# correction-interrupt watcher never asks for key-up events at all, so its behaviour is
# byte-for-byte what it was before hotkeys existed.
_KEY_UP = 11
# A modifier went down or up. Asked for alongside key-up and for the same reason: to see a
# hotkey's release when it does not arrive as a key-up (see RELEASES THAT DO NOT COME AS A
# KEY-UP) — and to see `fn` alone at all, since a modifier on its own never sends a key-down.
_FLAGS_CHANGED = 12
# Every button that can move the focus, not just the left one. A right-click opens a menu
# somewhere else and an extra button is bound to whatever its owner chose; both put the caret
# in a different window, and only the left one was being watched — so a correction after one
# of those went wherever the click had landed.
_MOUSE_DOWN = (1, 3, 25)
_KEYCODE_FIELD = 9
# `kCGKeyboardEventAutorepeat`: nonzero on every key-down the OS synthesizes while a key
# stays physically held, after the first one. Read only for the configured hotkey (see
# `_handle`) — a repeat is "still holding", not a second press, and letting it through as
# one made a toggle-mode hold self-cancel within the OS's own repeat delay, and made a
# hold-mode hold nothing (each repeat is itself a "press", never a release, so it never
# looked like anything ended).
_AUTOREPEAT_FIELD = 8
_SOURCE_USER_DATA = 42
# A tap that stops responding is switched off by the system rather than removed. Both
# reasons are reported through the callback as a pseudo-event, and both are recoverable by
# switching it back on — a tap left disabled is silently blind for the rest of the session.
_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
_DISABLED_BY_USER_INPUT = 0xFFFFFFFF

ESCAPE = 53
# The fn / Globe key's own codes (`kVK_Function`, and the Globe code newer Apple keyboards
# report). A key-down from either is fn itself, never the navigation key that turns a fn
# hold into fn+arrow — so it must not cancel a fn-alone hold.
_FN_KEYS = frozenset({63, 179})

_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p
)


def new_marker() -> int:
    """A per-session id for our own synthetic events. Never zero: zero is the default."""
    return secrets.randbits(62) + 1


def _event_mask(hotkeys: Sequence[Combo]) -> int:
    """The `CGEventTapCreate` mask for a watcher: key-down and every caret-moving button
    always; key-up and flagsChanged ONLY when at least one hotkey is configured (needed
    solely to notice its release, and to see `fn` alone — see `_KEY_UP`/`_FLAGS_CHANGED`).
    Pure and free of `_run`'s ctypes plumbing so a test can check the actual bits a
    hotkey-less watcher asks for, not just assert the mask constant differs from itself.
    """
    mask = 1 << _KEY_DOWN
    for button in _MOUSE_DOWN:
        mask |= 1 << button
    if hotkeys:
        mask |= 1 << _KEY_UP | 1 << _FLAGS_CHANGED
    return mask


Edge = tuple[Combo, bool]


class Holds:
    """Which configured hotkeys are physically down, worked out from the tap's raw events
    alone and turned into ``(combo, pressed)`` edges. Pure — no Quartz, no thread — so every
    edge can be tested; `Watcher._handle` feeds it on the tap's own thread.

    A chord with a key goes down on its key-down (never an autorepeat one: a repeat is still
    holding, not a second press) and comes up on its key's key-up, whatever the modifiers say
    by then (`Combo.released`) — OR the moment one of its own modifiers is let go, since a
    hold must not survive its chord: cmd released first with "d" still down would otherwise
    auto-repeat "d" into the document with the microphone open. A later autorepeat of the
    same chord, cmd pressed again, does not re-arm it.

    `fn` alone (`Combo.lone_modifier`) goes down when fn is the ONLY modifier held and comes
    up when fn is released. It counts only while nothing joins it: any key-down, or any other
    modifier, means this was fn+arrow / fn+delete navigation or the start of a chord, so a
    release edge is sent at once (the cancel) and nothing more until fn itself comes up —
    fn going down again is what arms the next one.
    """

    def __init__(self, hotkeys: Sequence[Combo]) -> None:
        self._chords = tuple(combo for combo in hotkeys if not combo.lone_modifier)
        self._lone = tuple(combo for combo in hotkeys if combo.lone_modifier)
        self._chords_down: set[Combo] = set()
        # A lone-modifier hotkey that is down -> whether it still counts (False once
        # cancelled: it stays down, reporting nothing, until the modifier itself comes up).
        self._lone_down: dict[Combo, bool] = {}

    def key_down(self, keycode: int, flags: int, *, autorepeat: bool) -> list[Edge]:
        edges: list[Edge] = []
        if keycode not in _FN_KEYS:
            for combo, counting in self._lone_down.items():
                if counting:
                    self._lone_down[combo] = False
                    edges.append((combo, False))
        for combo in self._chords:
            if combo.matches(keycode, flags):
                if not autorepeat:
                    self._chords_down.add(combo)
                    edges.append((combo, True))
                break
        return edges

    def key_up(self, keycode: int) -> list[Edge]:
        for combo in self._chords:
            if combo.released(keycode):
                self._chords_down.discard(combo)
                # Configured hotkeys never share a keycode — see `hotkey.refuse_shared_key`.
                return [(combo, False)]
        return []

    def flags_changed(self, flags: int) -> list[Edge]:
        held = flags & RELEVANT_FLAGS
        edges: list[Edge] = []
        for combo in tuple(self._chords_down):
            if held & combo.modifiers != combo.modifiers:
                self._chords_down.discard(combo)
                edges.append((combo, False))
        for combo in self._lone:
            counting = self._lone_down.get(combo)
            if counting is None:
                if held == combo.modifiers:
                    self._lone_down[combo] = True
                    edges.append((combo, True))
            elif held & combo.modifiers != combo.modifiers:
                del self._lone_down[combo]
                if counting:
                    edges.append((combo, False))
            elif held != combo.modifiers and counting:
                self._lone_down[combo] = False
                edges.append((combo, False))
        return edges


class Watcher:
    """Reports the user's own key presses and clicks on a background run loop.

    `on_key(keycode)` is called ON THE TAP'S OWN THREAD and must do its work there. It must
    NOT hand the press to the event loop: that was the first implementation, via
    `call_soon_threadsafe`, and it was wrong in a way that took a real failure to see — the
    press waited its turn in the loop's queue behind an edit that was already scheduled, so
    the edit was typed into the window the user had just clicked away from. See `arrived` in
    `dictation.py` for the whole account. The cost is that the callback runs somewhere no
    loop machinery may be touched, which is why it only ever sets flags.
    """

    def __init__(
        self,
        *,
        marker: int,
        on_key: Callable[[int], None],
        hotkeys: Sequence[Combo] = (),
        on_hotkey: Callable[[Combo, bool], None] | None = None,
    ) -> None:
        self._marker = marker
        self._on_key = on_key
        self._hotkeys = tuple(hotkeys)
        self._holds = Holds(self._hotkeys)
        self._on_hotkey = on_hotkey
        self._loop: int | None = None
        self._ready = threading.Event()
        self._failed: str | None = None
        self._thread: threading.Thread | None = None
        self._trampoline = _CALLBACK(self._handle)
        self._port: int | None = None
        # Set when the caller has already given up on this watcher; checked by `_run` before
        # it commits the thread to a run loop.
        self._unwanted = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="stt-mic-tap", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0) and self._failed is None:
            # Not reaching the end of `_run` is as much a failure as being refused by it, and
            # it used to look like success: `failure` stayed None, the caller carried on, and
            # dictation began with nothing watching for the user's own keystrokes. Synthetic
            # corrections would then delete text stt had not typed, which is the one thing
            # the whole design promises never to do.
            self._failed = "the event tap did not start within five seconds"
            # And told to stop. The thread is only late, not dead: it can reach
            # `CFRunLoopRun` a moment after this and then watch every key the user presses
            # for the rest of the process's life, with nobody holding a reference to switch
            # it off. A system-wide watcher nothing owns is the last thing to leave behind.
            self._unwanted = True

    @property
    def failure(self) -> str | None:
        """Why the tap is not watching, or None. Set before `start` returns."""
        return self._failed

    def stop(self) -> None:
        api = frameworks()
        if self._loop:
            api.core.CFRunLoopStop(ctypes.c_void_p(self._loop))
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _handle(self, proxy: int, kind: int, event: int, _info: int) -> int:
        """The tap callback. Listen-only, so the event is always returned untouched.

        The tap CANNOT swallow the hotkey's own keystroke — `_LISTEN_ONLY` means macOS still
        delivers it to whatever has focus, exactly like every other key. A modifier
        combination is not guaranteed to be silent there either: Option+letter is a dead key
        or an accented character on the US layout (Option+D is "∂"), and some Cocoa text
        views bind Control+letter to Emacs-style caret motion. Either way the window the
        hotkey was pressed over may have just received a character stt did not type, or
        moved where its own next backspace would land — so a hotkey key-down is treated as
        "the user touched the keyboard" (`on_key`) exactly like any other key, in ADDITION to
        firing `on_hotkey`, never instead of it. An earlier version skipped `on_key` here to
        avoid interrupting the in-flight correction on every activation; that traded a real
        text-corruption risk for a cosmetic one, which is the wrong side of that trade.
        """
        del proxy
        if kind in (_DISABLED_BY_TIMEOUT, _DISABLED_BY_USER_INPUT):
            if self._port:
                frameworks().services.CGEventTapEnable(ctypes.c_void_p(self._port), True)
            return event
        api = frameworks()
        stamp = api.services.CGEventGetIntegerValueField(ctypes.c_void_p(event), _SOURCE_USER_DATA)
        if stamp == self._marker:
            return event
        code = int(api.services.CGEventGetIntegerValueField(ctypes.c_void_p(event), _KEYCODE_FIELD))
        holds, on_hotkey = self._holds, self._on_hotkey
        if kind == _FLAGS_CHANGED:
            # Only reachable when `_run` put flagsChanged in the mask (a hotkey is configured).
            # A modifier going up or down is never "the user typed something", so it never
            # reaches `on_key`; it exists to notice a hold's release and `fn` alone.
            if on_hotkey is not None:
                flags = int(api.services.CGEventGetFlags(ctypes.c_void_p(event)))
                for combo, pressed in holds.flags_changed(flags):
                    on_hotkey(combo, pressed)
            return event
        if kind == _KEY_UP:
            # Only reachable when `_run` put key-up in the mask, which it does only when at
            # least one hotkey is configured (see `_run`) — every watcher with none never
            # receives this kind at all. It exists solely to notice a hotkey's own release
            # and must never reach `on_key`: a release was never "the user typed something",
            # and treating it as one would fire the correction-interrupt twice per
            # activation.
            if on_hotkey is not None:
                for combo, pressed in holds.key_up(code):
                    on_hotkey(combo, pressed)
            return event
        if kind == _KEY_DOWN and on_hotkey is not None:
            flags = int(api.services.CGEventGetFlags(ctypes.c_void_p(event)))
            autorepeat = bool(
                api.services.CGEventGetIntegerValueField(ctypes.c_void_p(event), _AUTOREPEAT_FIELD)
            )
            for combo, pressed in holds.key_down(code, flags, autorepeat=autorepeat):
                on_hotkey(combo, pressed)
            # Deliberately falls through to `on_key` below — see the docstring.
        self._on_key(code if kind == _KEY_DOWN else -1)
        return event

    def _run(self) -> None:
        api = _declare(frameworks())
        mask = _event_mask(self._hotkeys)
        port = api.services.CGEventTapCreate(
            _SESSION_TAP, _HEAD_INSERT, _LISTEN_ONLY, mask, self._trampoline, None
        )
        if not port:
            self._failed = "the event tap was refused (Accessibility not granted)"
            self._ready.set()
            return
        self._port = port
        source = api.core.CFMachPortCreateRunLoopSource(None, ctypes.c_void_p(port), 0)
        if not source:
            # Nothing here can survive a null source: handing it to `CFRunLoopAddSource` is a
            # crash inside CoreFoundation, in a thread with no traceback to show for it.
            self._failed = "the event tap could not be attached to a run loop"
            api.core.CFRelease(ctypes.c_void_p(port))
            self._port = None
            self._ready.set()
            return
        loop = api.core.CFRunLoopGetCurrent()
        modes = ctypes.c_void_p.in_dll(api.core, "kCFRunLoopCommonModes")
        api.core.CFRunLoopAddSource(ctypes.c_void_p(loop), ctypes.c_void_p(source), modes)
        api.services.CGEventTapEnable(ctypes.c_void_p(port), True)
        self._loop = loop
        self._ready.set()
        if self._unwanted:
            # Late, and no longer wanted. Enter no run loop; unwind instead.
            api.core.CFRelease(ctypes.c_void_p(source))
            api.core.CFRelease(ctypes.c_void_p(port))
            self._port = None
            return
        api.core.CFRunLoopRun()
        api.core.CFRelease(ctypes.c_void_p(source))
        api.core.CFRelease(ctypes.c_void_p(port))


def _declare(api: _Frameworks) -> _Frameworks:
    """Argument and return types for the calls only the tap makes."""
    services, core = api.services, api.core
    services.CGEventTapCreate.restype = ctypes.c_void_p
    services.CGEventTapCreate.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint64,
        _CALLBACK,
        ctypes.c_void_p,
    ]
    services.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    services.CGEventGetIntegerValueField.restype = ctypes.c_int64
    services.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    services.CGEventGetFlags.restype = ctypes.c_uint64
    services.CGEventGetFlags.argtypes = [ctypes.c_void_p]
    core.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
    core.CFMachPortCreateRunLoopSource.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_long,
    ]
    core.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    core.CFRunLoopAddSource.argtypes = [ctypes.c_void_p] * 3
    core.CFRunLoopStop.argtypes = [ctypes.c_void_p]
    return api
