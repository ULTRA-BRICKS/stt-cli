"""config — where stt-cli keeps its state, and the knobs a run is configured with.

TWO LAYERS, ONE SHAPE
    :class:`Settings` is the single struct the pipeline reads. It is built by loading the
    user's ``config.json`` over the built-in defaults, then applying command-line flags
    over that. A flag always wins; an absent flag never clobbers a stored preference.

WHERE THINGS LIVE
    Everything durable goes under one root — by default the macOS application-support
    directory, overridable with ``STT_HOME`` (tests and throwaway runs use that). Nothing
    is ever written to ``/tmp``: the whole point of the archive is that a transcript you
    paid GPU time for is still there next month. Only genuinely scratch intermediates go
    to a temporary directory, and they are deleted in the same run that made them.
"""

from __future__ import annotations

import json
import math
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import portable
from .dictionary import DEFAULT_SIMILARITY
from .jsonio import ABSENT, read_json

# A settings file is a dozen keys. The bound is here for the same reason the dictionary's is:
# this file is read by every run, and anything can be sitting at that path.
MAX_CONFIG_BYTES = 1024 * 1024

APP_NAME = "stt-cli"

# `mic_daemon_autostart`'s third state, beside `True`/`False`/`None` — see the field's own
# doc comment. Recorded by `commands/mic.py::_maybe_autostart_daemon` when `enable()`
# succeeded and the OS accepted the autostart install (`autostart_active()`), but the
# daemon it just installed has NOT been verified genuinely alive yet (`mic_service.
# probe_launchd`, #54) — most often a permission-flavored crash-loop (`EXIT_PERMISSION`):
# the ad-hoc dictation that triggered this proved ACCESSIBILITY/MICROPHONE work for THAT
# process's own identity, never for the separately-spawned launchd daemon (#54 exists
# because those two can genuinely differ). Deliberately not `True` (that would durably
# claim a daemon that may be crash-looping forever is confirmed working) and not `False`
# (nothing here was ever DECLINED — a real user `stt mic disable` is the only thing that
# means that). Treated the SAME as `None` by the trigger's own gate — "eligible to try
# again on the next successful ad-hoc dictation" — so it is retried automatically, not
# stuck, while still being distinguishable in the stored record from "nothing was ever
# attempted here at all". See #87.
MIC_AUTOSTART_RETRY = "retry"

# Formats the renderers can produce. `all` expands to this list minus the JSON-ish debug
# ones a human never asks for by name.
FORMATS = ("txt", "md", "json", "srt", "vtt", "csv", "tsv", "speakers", "summary")
DEFAULT_ALL = ("txt", "md", "json", "srt", "vtt")

TIMESTAMP_MODES = ("none", "relative", "absolute")


def app_home() -> Path:
    """The one durable root for models, archive and config."""
    override = os.environ.get("STT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def archive_dir() -> Path:
    return app_home() / "archive"


def media_dir() -> Path:
    return archive_dir() / "media"


def runs_dir() -> Path:
    return archive_dir() / "runs"


def models_dir() -> Path:
    return app_home() / "models"


CONFIG_FILENAME = "config.json"


def config_path() -> Path:
    return app_home() / CONFIG_FILENAME


def index_path() -> Path:
    return archive_dir() / "index.sqlite"


def ensure_dirs() -> None:
    for path in (app_home(), archive_dir(), media_dir(), runs_dir(), models_dir()):
        path.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class Settings:
    """Everything one transcription run needs to know."""

    # engine
    backend: str = "auto"
    model: str = "large-v3-turbo"
    language: str | None = None
    threads: int = 0  # 0 -> let the engine choose
    whispercpp_root: str | None = None

    # How much of its own previous output the decoder is fed back as context. Carrying it
    # keeps casing, punctuation and proper nouns consistent across the model's 30-second
    # windows; it is also exactly what lets a repetition loop feed itself, because a garbage
    # phrase in the prompt makes the same garbage the likeliest continuation. `off` is the
    # safe default and `context_compare` is how you get the quality back without the risk.
    context: str = "off"  # off | short | full
    context_compare: str = "off"
    # Did somebody actually CHOOSE the mode above, or is it just the built-in default?
    # `--fix` turns the comparison on when nobody chose, because an LLM asked to correct a
    # transcript with the disagreements hidden from it is guessing. It must not override a
    # person who typed `--context-compare off`, which is why the two are not the same fact.
    # Never configurable and never in the cache key: what gets keyed is the mode it settles.
    context_compare_chosen: bool = False

    # live dictation
    # How loud a frame must be before `stt mic` treats it as speech, whatever the room is
    # doing. Zero means the detector's own default. Raise it in a room where stray noises get
    # typed; lower it if a quiet voice goes unheard. `stt mic --check` reports both numbers.
    mic_threshold: float = 0.0
    # The global chord that starts/stops a background `stt mic` daemon (`live/hotkey.py`),
    # e.g. "cmd+shift+space". None means nobody has chosen one, which `hotkey.resolve` reads
    # as "use the built-in default" — the same None-means-unset convention `language` and
    # `timezone` already use, so a fresh install still states a real default in `--help`
    # without this file hard-coding what that default is.
    hotkey: str | None = None
    # "toggle" (press once to start, again to stop) or "hold" (dictate only while held).
    # None resolves through `hotkey.resolve_mode` the same way `hotkey` resolves through
    # `hotkey.resolve` — one place owns the actual default word.
    hotkey_mode: str | None = None
    # A DECISION RECORD, not a direct control — `stt config set mic_daemon_autostart true`
    # stores the record without touching the daemon itself (use `stt mic enable`/`disable`
    # for that); this only ever affects what `commands/mic.py::_maybe_autostart_daemon`
    # decides on a LATER successful dictation, which is why the TUI/help text below says so
    # explicitly rather than implying a live toggle. FOUR states, not three:
    #   None   — nobody has decided yet (the same None-means-undecided convention as
    #            `hotkey`/`hotkey_mode` above); `_maybe_autostart_daemon` reads this as
    #            "eligible to turn the background daemon on the first time an ad-hoc
    #            dictation actually completes on this machine" — that success is proof
    #            Accessibility and the microphone are really granted to THAT process, the
    #            same permission surface the daemon needs.
    #   MIC_AUTOSTART_RETRY ("retry") — that trigger fired, `enable()` succeeded and the OS
    #            accepted the autostart install, but the SEPARATELY-SPAWNED launchd daemon
    #            was not then verified genuinely alive (`mic_service.probe_launchd`, #54):
    #            an ad-hoc process's own TCC identity is not proof of the daemon's — see
    #            `MIC_AUTOSTART_RETRY`'s own comment. Treated the SAME as `None` by the
    #            trigger's gate (tries again next time), but distinguishable in the record
    #            from "never even attempted".
    #   True   — confirmed on: the daemon was verified genuinely alive after auto-enable,
    #            or an explicit `stt mic enable` was run (that one is trusted without a
    #            liveness probe — a human just watched it and can watch `stt mic status`).
    #   False  — an explicit `stt mic disable` opted out, and it must never be silently
    #            turned back on.
    # Unsetting it (`stt config unset mic_daemon_autostart`) re-arms the automatic decision
    # from scratch (back to `None`). See #87.
    mic_daemon_autostart: bool | str | None = None

    # voice activity detection
    vad: str = "auto"  # auto | silero | ffmpeg | none
    vad_threshold: float = 0.5
    vad_min_silence_ms: int = 400
    vad_speech_pad_ms: int = 200
    vad_min_speech_ms: int = 250

    # cleaning
    clean: bool = True
    strict_clean: bool = False
    max_repeats: int = 3
    confidence_floor: float = 0.55

    # terminology (see dictionary.py: prompt biasing, exact fixes, phonetic flags)
    dictionary: bool = True
    dict_bias: bool = True  # feed the glossary to the speech model, not just to the LLM
    # One source for the number: `dictionary.apply`/`screen` take it as their default
    # argument, so a value written down twice would let a direct caller and the
    # pipeline disagree about what the threshold is.
    dict_similarity: float = DEFAULT_SIMILARITY
    # Filled in by the pipeline from the dictionary's content, never by the user: it exists
    # so that editing a term invalidates exactly the cached runs it would have changed.
    dict_digest: str = ""
    # Also filled in by the pipeline, from the ENGINES: a sorted, comma-joined list of what
    # the installed engines could not actually do for this run ("whispercpp cannot pin the
    # glossary"). Such a run decodes differently from one where they could, so it must not
    # share that run's identity — otherwise upgrading the engine changes nothing and the
    # compromised transcript is served forever. Empty means nothing fell short.
    engine_limits: str = ""

    # variants
    variants: int = 0  # extra decodings per low-confidence segment
    variant_models: list[str] = field(default_factory=list)
    show_variants: bool = False

    # correction / summary
    fix: bool = False
    fix_with: str = "auto"  # auto | codex | claude | opencode | <binary>
    summary: bool = False
    summary_style: str = "structured"

    # diarization
    diarize: bool = False
    speakers: int | None = None

    # output
    show_flags: bool = False
    text_variant: str = "fixed"  # fixed | raw | both
    formats: list[str] = field(default_factory=lambda: ["txt"])
    timestamps: str = "none"
    timezone: str | None = None
    output: str | None = None
    # Per-run only: an explicit recording start time for absolute timestamps. It describes
    # one particular file rather than a preference, so it is never stored in config.json.
    recorded_at: datetime | None = None

    # archive
    cache: bool = True
    keep_media: bool = True

    def merged(self, **overrides: Any) -> Settings:
        """Return a copy with the non-``None`` overrides applied (flags beat config)."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)


# Only these keys may come from the on-disk config. An unknown key is a typo the user
# should hear about rather than a silently ignored preference.
def configurable() -> set[str]:
    """The settings a person may read and write. `config list`/`get` ask this too.

    Listing every dataclass field advertised `dict_digest` and `engine_limits` — the run's
    own identity, computed by the pipeline — as if they were preferences, and `config set`
    then refused them as unknown. One contract, asked by all three commands.
    """
    return set(_CONFIGURABLE)


_CONFIGURABLE = {
    f
    for f in Settings.__dataclass_fields__
    if f not in {"output", "recorded_at", "dict_digest", "engine_limits", "context_compare_chosen"}
}


def _settings_object(path: Path) -> dict[str, Any]:
    """The contents of `config.json`, or the reason it cannot be one.

    Both the reader and the writer go through here, and that is the point. When only the
    reader checked the shape, `stt config list` reported a `config.json` holding `[]` as
    broken while `stt config set` quietly replaced it with a fresh object — the same file,
    diagnosed by one command and destroyed by the other.
    """
    from ._errors import UsageError

    raw = read_json(
        path,
        how="fix the JSON, save it as UTF-8, or delete it for the defaults",
        limit=MAX_CONFIG_BYTES,
        too_big="a settings file is a handful of keys, not a document",
    )
    if raw is ABSENT:
        return {}
    if not isinstance(raw, dict):
        raise UsageError(
            what=f"{path} is not a settings file",
            why='expected an object of setting names and values, like {"language": "ru"}',
            how="write {} into it, or delete it to fall back to the defaults",
        )
    return raw


def stored() -> dict[str, Any]:
    """The settings actually written in `config.json`, so a caller can say which are stored."""
    return _settings_object(config_path())


def load_settings() -> Settings:
    """Built-in defaults with the user's ``config.json`` laid over them."""
    from ._errors import UsageError

    path = config_path()
    settings = Settings()
    raw = _settings_object(path)
    if not raw:
        return settings
    unknown = sorted(set(raw) - _CONFIGURABLE)
    if unknown:
        raise UsageError(
            what=f"unknown key(s) in {path}: {', '.join(unknown)}",
            why="stt only understands the settings listed by `stt config list`",
            how="remove the key, or check `stt config list` for its real name",
        )
    _validate(raw)
    settled = replace(settings, **raw)
    if "context_compare" in raw:
        settled = replace(settled, context_compare_chosen=True)
    return settled


# The declared type of each setting, taken from the dataclass annotation rather than from
# whatever value happens to be there. Reading the type off the CURRENT value cannot work for
# a field whose default is None (`language`, `timezone`): there is nothing to read, so
# `stt config set language 1` would happily store the integer 1.
_TYPES: dict[str, str] = {
    name: str(field.type) for name, field in Settings.__dataclass_fields__.items()
}

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def coerce(key: str, raw: str) -> Any:
    """Turn a command-line string into the type ``key`` is declared as, or refuse clearly."""
    from ._errors import UsageError

    declared = _TYPES.get(key, "str")
    if "bool" in declared:
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise UsageError(
            what=f"{key} is a true/false setting",
            why=f"{raw!r} is not a boolean",
            how=f"use `stt config set {key} true` or `... false`",
        )
    if "list" in declared:
        return [part.strip() for part in raw.split(",") if part.strip()]
    for name, caster in (("int", int), ("float", float)):
        if name in declared:
            try:
                number = caster(raw)
            except ValueError as exc:
                raise UsageError(
                    what=f"{key} is a {name} setting",
                    why=f"{raw!r} is not a {name}",
                    how=f"pass a number, e.g. `stt config set {key} 2`",
                ) from exc
            _refuse_a_number_that_is_not_one(key, number)
            return number
    return raw


def _refuse_a_number_that_is_not_one(key: str, number: float) -> None:
    """`float("inf")` and `float("nan")` are numbers to Python and to nobody else.

    The same hole was found and closed once already, in `--idle-minutes`: `nan` compares
    false against everything and `inf` compares false against everything finite, so either
    one switches off whatever it is a threshold for — silently, since nothing raises. Here it
    would be `mic_threshold`: `stt config set mic_threshold inf` leaves the microphone open,
    the detector never opening, nothing typed, and `--check` reporting a perfectly loud voice
    as too quiet. Closing it in one place rather than per setting, because the next threshold
    somebody adds will have exactly the same hole.

    Only finiteness. Whether a NEGATIVE value makes sense is a question about the particular
    setting — `-1` is nonsense for a threshold and nonsense for a repetition count, but this
    function knows nothing about either, and an earlier version answered it here with a
    sentence about microphone levels that `stt config set max_repeats -1` then printed.
    """
    from ._errors import UsageError

    # An `int` is finite by construction, and asking `math.isfinite` about a very large one
    # RAISES: it converts to float first, so `stt config set threads <309 digits>` answered a
    # promised usage error with an `OverflowError` traceback. Centralising the check created
    # a way to crash that neither of the copies it replaced had.
    if isinstance(number, int):
        return
    if not math.isfinite(number):
        raise UsageError(
            what=f"{key} must be an ordinary number",
            why=f"{number!r} compares false against every real measurement, so it would "
            "switch off whatever it is a threshold for without saying so",
            how=f"pass a finite number, e.g. `stt config set {key} 1500`",
        )


def _a_real_number(value: Any) -> bool:
    """A float a measurement can be compared against — so not a bool, and not `Infinity`.

    JSON has no infinity, but Python's `json` reads the bare words `Infinity` and `NaN`
    anyway, so a hand-edited config can carry one into a threshold. This is the same question
    `_refuse_a_number_that_is_not_one` asks of a value typed at the command line, and the two
    paths have to agree: one of them accepting what the other refuses is how a setting ends
    up rejected when set and honoured when hand-edited.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


# The declared type of a field can be `bool | str | None` (only `mic_daemon_autostart`,
# today) when a plain `bool | None` tri-state is not enough — see that field's own doc
# comment. `_validate` cannot infer which STRINGS such a field accepts from the type alone
# (unlike an ordinary `str` field, which accepts any string), so each one names its own
# closed set here; a hand-edited value outside that set is rejected exactly like a wrong
# TYPE would be, not silently accepted as some fourth, undocumented state.
_STRING_ENUM_VALUES: dict[str, frozenset[str]] = {
    "mic_daemon_autostart": frozenset({MIC_AUTOSTART_RETRY}),
}

# A `bool | str` field with no entry above would fail CLOSED, not loudly: `_stored_value_ok`
# falls back to `_STRING_ENUM_VALUES.get(key, ())`, an EMPTY set, so every string for that
# field would be rejected as a type error rather than validated against whatever the field
# actually allows — the bug would look like the field itself was broken, not like a missing
# registration here. Checked at IMPORT time, against the dataclass's own declared types
# (not a hand-maintained list of field names that could drift from it independently), so a
# future `bool | str` field ships with this satisfied or the module fails to import at all.
_BOOL_STR_FIELDS = frozenset(
    name for name, declared in _TYPES.items() if "bool" in declared and "str" in declared
)
assert _STRING_ENUM_VALUES.keys() >= _BOOL_STR_FIELDS, (
    "_STRING_ENUM_VALUES is missing an entry for: "
    f"{sorted(_BOOL_STR_FIELDS - _STRING_ENUM_VALUES.keys())}"
)


def _stored_value_ok(key: str, value: Any, declared: str) -> bool:
    """Whether `value` can genuinely be `key`'s declared type — pulled out of `_validate`
    itself because a five-way nested ternary reads like a puzzle, not a type check. `bool`
    is checked before `int` (not the other way around) because `bool` IS an `int` in
    Python: `isinstance(True, int)` is true, so an `int`-only field would silently accept
    `True`/`False` unless the `bool` branch runs first and the `int` branch explicitly
    excludes it.
    """
    if "bool" in declared and "str" in declared:
        return isinstance(value, bool) or (
            isinstance(value, str) and value in _STRING_ENUM_VALUES.get(key, ())
        )
    if "bool" in declared:
        return isinstance(value, bool)
    if "list" in declared:
        return isinstance(value, list)
    if "int" in declared:
        return isinstance(value, int) and not isinstance(value, bool)
    if "float" in declared:
        return _a_real_number(value)
    return value is None or isinstance(value, str)


def _validate(raw: dict[str, Any]) -> None:
    """Reject a stored value whose type cannot work, before it reaches the pipeline.

    A hand-edited (or older) config.json can hold ``"variants": "abc"``. Left alone that
    string flows into a numeric comparison and into the cache fingerprint, so every run is
    subtly wrong in a way whose cause is nowhere near the symptom.

    ``None`` is accepted up front whenever the field's OWN annotation says so (``"... |
    None"``), whatever the other half of that union is. ``mic_daemon_autostart: bool | str
    | None`` documents ``None`` as a real, meaningful third state — nobody has decided yet,
    see its own comment above — and the per-type checks below only ever special-cased
    ``None`` for the ``str`` branch, so a hand-edited ``{"mic_daemon_autostart": null}``
    (restoring exactly the tri-state the field's own default already starts at) was
    rejected with a "wrong type" error that contradicted the field's documented contract,
    locking `stt config` itself out until the key was deleted by hand. The SAME field's
    fourth state (``MIC_AUTOSTART_RETRY``) is a ``str``, not a ``bool`` — declared
    ``"bool | str | None"``, checked against its own closed set in
    ``_STRING_ENUM_VALUES`` rather than accepting an arbitrary string, which the bare
    ``"str" in declared`` branch below would otherwise do for ANY field that mixed ``bool``
    and ``str``. The actual per-type check lives in `_stored_value_ok`, decomposed out of
    here because a five-way nested ternary reads like a puzzle, not a type check.
    """
    from ._errors import UsageError

    for key, value in raw.items():
        declared = _TYPES.get(key, "str")
        if value is None and "None" in declared:
            continue
        ok = _stored_value_ok(key, value, declared)
        if not ok:
            # A non-finite float is not a type error, and saying "expected float, found
            # float (nan)" contradicts itself. It is a real number that is not a real
            # measurement, and the sentence has to say which of the two went wrong.
            if isinstance(value, float) and not _a_real_number(value):
                what = f"{key} in {config_path()} is not a number anything can be compared to"
                why = (
                    f"{value!r} compares false against every real measurement, so it would "
                    "switch off whatever it is a threshold for without saying so"
                )
            else:
                what = f"{key} in {config_path()} has the wrong type"
                why = f"expected {declared}, found {type(value).__name__} ({value!r})"
            raise UsageError(
                what=what,
                why=why,
                how=f"fix it, or run `stt config set {key} <value>` to write a valid one",
            )


def _resolved_target() -> Path:
    """The real, refused-if-strange path a settings-file write locks and operates on.

    ``config.json`` may be a symlink into a dotfiles checkout — an ordinary thing to do —
    so the link is resolved once here and everything after (the lock, the read, the write)
    happens on the resolved target. Resolving it a second time later would leave a gap for
    the link to be repointed in between, turning "write my config" into "rename a file over
    whatever that points at now"; and locking beside the link rather than beside the target
    would let two ``STT_HOME``s pointing at one file take two different locks that never
    exclude each other. Shared by `_settings_transaction` (an ordinary read-modify-write)
    and `locked_for_daemon_decision` (a lock held with no read/write of its own) so the
    symlink/refuse policy lives in exactly one place.
    """
    ensure_dirs()
    target = Path(os.path.realpath(config_path()))
    _refuse_a_strange_target(target)
    return target


@contextmanager
def _settings_transaction(key: str) -> Iterator[tuple[dict[str, Any], Path, Path]]:
    """Resolve, validate and lock ``config.json`` for one read-modify-write.

    ``save_setting`` and ``unset_setting`` used to repeat this in full: the whole of it is
    one transaction over ONE file, and both halves of that sentence had to be argued for.

    One transaction: reading the whole file, changing one key and writing the whole file back
    is three operations, and two terminals doing it at once both read the old object — then
    whichever renames last stores a version that never saw the other's key, gone with no
    error anywhere. The lock spans the read and the write, the way `dictionary.editing` does.

    Yields ``(raw, named, target)``: the loaded dict to mutate in place, the (possibly
    symlinked) configured path, and its resolved target — the two paths `write_atomic`
    needs. Whether anything is actually written is the caller's call, not this one:
    `unset_setting` skips the write entirely when the key was never stored.
    """
    from ._errors import unknown_item

    if key not in _CONFIGURABLE:
        raise unknown_item("setting", key, sorted(_CONFIGURABLE))
    target = _resolved_target()
    with _editing_the_settings(target):
        # Through the same guarded loader as everything else: a corrupt config must produce
        # the diagnosed error, not a traceback out of json or the codec — and not, as an
        # earlier version of this line did through `as_dict`, a silent `{}` written back
        # over whatever was really in the file.
        yield _settings_object(target), config_path(), target


def save_setting(key: str, value: Any) -> None:
    """Persist one preference into ``config.json``, leaving the rest untouched."""
    # Imported here, not at the top: `archive` imports `config` back, the way
    # `dictionary.save` does it for the same reason.
    from .archive import write_atomic

    with _settings_transaction(key) as (raw, named, target):
        raw[key] = value
        write_atomic(target, _serialized(raw, named))


def save_setting_if_unset(key: str, value: Any, *, unset: tuple[Any, ...] = (None,)) -> bool:
    """Persist ``value`` for ``key`` ONLY if nothing has DECIDED a value there yet —
    the atomic version of "read, see it is still unset, then write", for a decision that
    must never silently clobber a concurrent explicit choice.

    A separate `load_settings()` read followed by a separate `save_setting()` write (the
    shape `commands/mic.py::_maybe_autostart_daemon` used before this) leaves a real gap
    between the two: a concurrent writer can land in between them and get overwritten. This
    does the check and the write inside ONE `_settings_transaction` lock acquisition, so
    there is no gap left to land in. Returns whether it actually wrote — ``False`` means
    someone else's decision was already there and this left it alone.

    "Already there" means ``raw.get(key) not in unset``, not merely ``key in raw``: a
    hand-edited ``{"mic_daemon_autostart": null}`` is a valid, explicit "undecided" (see
    `_validate` and the field's own doc comment — the SAME None-means-unset convention the
    caller's own gate (`load_settings().mic_daemon_autostart is not None`) already tests
    by value, not by presence. Testing presence here disagreed with that gate: the gate let
    a decision proceed (value was `None`), the real daemon control ran, and THIS function
    then silently refused to persist it (the key was merely present) — no confirmation
    note, and the record stayed `null` forever, so every later dictation re-entered and
    re-probed the daemon's status. Caught in review before merge.

    ``unset`` defaults to just ``(None,)`` but ``_maybe_autostart_daemon`` (#87) passes
    ``(None, MIC_AUTOSTART_RETRY)`` for `mic_daemon_autostart`: `MIC_AUTOSTART_RETRY` ITSELF
    counts as "not yet decided" — it records "tried, the daemon was not verified alive",
    not a real decision either way — so a LATER successful dictation must still be able to
    overwrite it (with a confirmed `True`, or with another `MIC_AUTOSTART_RETRY` while the
    daemon keeps failing to come up), the same as it could overwrite a bare `None`. Without
    broadening this check, the first crash-looping attempt would permanently wedge the
    field at `MIC_AUTOSTART_RETRY` — retryable in name only, since nothing would ever be
    allowed to write over it again.
    """
    from .archive import write_atomic

    with _settings_transaction(key) as (raw, named, target):
        if raw.get(key) not in unset:
            return False
        raw[key] = value
        write_atomic(target, _serialized(raw, named))
        return True


def unset_setting(key: str) -> None:
    """Remove one preference from ``config.json``, so it goes back to answering with the
    built-in default — the reset half of `save_setting`, sharing the same one-file,
    one-transaction lock so a concurrent `set` and `unset` cannot interleave.

    Removing a key that was never stored is not an error: "make sure this is not
    overridden" is a sensible thing to ask for whether or not anybody had overridden it.
    """
    from .archive import write_atomic

    with _settings_transaction(key) as (raw, named, target):
        if key not in raw:
            return
        del raw[key]
        write_atomic(target, _serialized(raw, named))


_held_locks = threading.local()


@contextmanager
def _editing_the_settings(target: Path) -> Iterator[None]:
    """An exclusive lock over one read-modify-write of the settings file.

    Beside the RESOLVED target, so that two homes whose `config.json` links to the same file
    take the same lock. And opened the way every other file here is opened — non-blocking,
    with the descriptor asked what it is — because a lock file is a file like any other, and
    a named pipe sitting where it goes would have hung `stt config set` forever on the very
    line that was added to make the command safe.

    Reentrant within one thread: `locked_for_daemon_decision` holds this same lock around a
    whole check-then-control-then-persist sequence (`commands/mic.py::_maybe_autostart_daemon`
    / `_lifecycle`, #87), and the persist step at the end of that sequence is an ordinary
    `save_setting`/`save_setting_if_unset` call, which reaches here a SECOND time. Plain
    `flock` has no idea the two calls share an owner — it locks the OPEN FILE DESCRIPTION,
    not the process, so a second `os.open` of the same path blocks on the first's lock even
    from the very same thread — so the outer call is tracked here and an inner one skips
    locking again instead of deadlocking against itself.
    """
    from ._errors import UsageError

    held = getattr(_held_locks, "targets", None)
    if held is None:
        held = set()
        _held_locks.targets = held
    if target in held:
        yield
        return
    lock = target.with_name(target.name + ".lock")
    try:
        # Owner only. The file holds nothing — it exists to be flocked — but there is no
        # reason for anything else on the machine to be able to open it, and a mode that
        # says "world readable" invites the question of what is in it.
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | portable.O_NONBLOCK, 0o600)
    except OSError as exc:
        raise UsageError(
            what=f"could not lock {target}",
            why=str(exc),
            how=f"remove {lock} if something else is sitting in its place",
        ) from exc
    # The raw descriptor, not a file object: wrapping a FIFO in one raises before the check
    # that was supposed to catch the FIFO.
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UsageError(
                what=f"could not lock {target}",
                why=f"{lock} is not a regular file",
                how="move it out of the way",
            )
        portable.lock(descriptor)
        held.add(target)
        try:
            yield
        finally:
            held.discard(target)
            portable.unlock(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def locked_for_daemon_decision() -> Iterator[None]:
    """One lock, held for the WHOLE check-then-control-then-persist sequence around
    `mic_daemon_autostart` — see `commands/mic.py::_maybe_autostart_daemon` and `_lifecycle`
    (#87).

    The per-write lock an ordinary `save_setting` call takes is not enough by itself: it
    only serializes the CONFIG record, while the actual OS-level daemon control
    (`manager.enable()` for the automatic path, `dispatch()` for an explicit `stt mic
    enable`/`disable`) used to happen outside any lock at all. That left a real gap: a
    concurrent explicit `disable` could turn launchd off in the window between the automatic
    path's own status check and its `enable()` call, re-installing autostart the user had
    just removed — `save_setting_if_unset` alone stopped that from being recorded as a lie,
    but the daemon itself stayed re-enabled despite the config correctly reading `False`
    afterward. Holding this lock around the daemon-control call AND the config write, on
    BOTH the automatic and the explicit path, closes that: the two decisions can no longer
    interleave, only run one fully before the other is allowed to start, so the config
    record and the real daemon state can never disagree about which decision won.
    """
    with _editing_the_settings(_resolved_target()):
        yield


def _refuse_a_strange_target(path: Path) -> None:
    """Say no to writing over something that is not a settings file.

    The reader treats anything that is not a regular file as "there is no config", which is
    right for reading and leaves the writer standing in front of whatever IS there. Opening a
    FIFO for writing blocks until somebody reads it — `stt config set` hanging with no
    message — and a directory raises out of the middle of the write. Neither is a thing to
    do to a path the user pointed `STT_HOME` at.

    Following symlinks, deliberately, because the reader does: `os.open` resolves the link
    and asks the descriptor what it is. Checking the link itself instead made a `config.json`
    symlinked into a dotfiles checkout readable by every command and writable by none.
    """
    from ._errors import UsageError

    try:
        found = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        raise UsageError(
            what=f"could not write {path}",
            why=str(exc),
            how="check the path is writable, or point STT_HOME somewhere else",
        ) from exc
    if not stat.S_ISREG(found.st_mode):
        raise UsageError(
            what=f"{path} is not a settings file",
            why="something that is not a regular file is sitting where config.json goes",
            how="move it out of the way, or point STT_HOME somewhere else",
        )


def _serialized(raw: dict[str, Any], path: Path) -> str:
    """The file, refused here if the reader would refuse it.

    A hand-edited `config.json` just under the limit loads fine, and `stt config set` then
    re-serializes it with indentation and one more key — over the line. The command reported
    success and every `stt` invocation after it failed before doing any work, on a file
    nothing would read. The same guard the dictionary has, for the same reason: the failure
    belongs at the write that causes it, not at every read that follows.
    """
    from ._errors import UsageError
    from .jsonio import in_units

    blob = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    if len(blob.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise UsageError(
            what=f"{path} would be too large to read back",
            why=f"it serializes to more than {in_units(MAX_CONFIG_BYTES)}",
            how="remove what you do not need from it, or delete it for the defaults",
        )
    return blob
