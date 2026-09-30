"""suggest — after a month of use, name the curated dictionary a user's own dictation keeps needing.

WHAT IT DOES (#88, `docs/specs/dictionaries.md` section 9)
    Once a day at most, after a file transcription has written and printed its result, a
    detached child process looks at the archive. When the user has been transcribing for at
    least a month (`MIN_AGE_DAYS`) and made enough transcripts lately (`MIN_TRANSCRIPTS` in
    the trailing `WINDOW_DAYS`), each of those transcripts is split into a few pieces and
    every piece is sorted into the topic tags of the catalog dictionaries not installed yet,
    plus "other", by laya: a local classifier that answers a `choice` question with a
    probability per option (`suggest_worker.py`). A transcript matches a topic when its best
    piece gives the topic at least `MATCH_PROBABILITY`; a topic qualifies when at least
    `MIN_MATCHES` transcripts, and at least `MIN_SHARE` of them, match it. The best qualifying
    topic's dictionary in the matching transcripts' language is named in ONE notification,
    with the reason and the exact `stt dict install <id>`.

WHAT IT NEVER DOES
    Install. Nothing on this path calls `installed.install`; the user reads the command and
    runs it. Nor does it reach the network: candidates come from the catalog bundled with stt,
    the classifier runs offline on the CPU, and the transcripts never leave the machine. The
    one download — laya itself and its two checkpoints — is `stt dict suggest --setup`, asked
    for and watched, never done in the background.

HOW OFTEN
    A check starts at most once per `CHECK_INTERVAL`, a notification reaches the user at most
    once per `NOTIFY_INTERVAL`, a dictionary is suggested at most once ever, and a user who
    has not set the classifier up is told how to, once. `dict_suggest` off (`stt config set
    dict_suggest false`) stops all of it: no process is started and nothing is read.

WHY A CHILD PROCESS
    Classifying a month of transcripts takes minutes on a CPU. It must never delay the command
    that triggered it or affect its transcript, so the command only stamps the state and
    starts `stt dict __suggest` in a session of its own, as `stt mic`'s autostart worker is
    started (#105).
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from . import config, portable
from ._errors import EXIT_OK, UsageError

if TYPE_CHECKING:
    from .archive import Archive, RunRecord
    from .catalog import Distributable

DAY = 86_400.0
# Eligibility: the spec's "month of use", on the archive's own `created_at`.
MIN_AGE_DAYS = 30
WINDOW_DAYS = 30
MIN_TRANSCRIPTS = 10
# The newest transcripts of the window that are classified, and the pieces each one gives.
# Bounded so a busy month costs minutes, not hours, of a background CPU.
MAX_TRANSCRIPTS = 60
MAX_PIECES = 3
# About 250 tokens of English: inside what laya's English checkpoint reads (512 tokens, the
# options included) and well inside the multilingual one's 1,024.
PIECE_CHARS = 1_000
# The spec's three thresholds (section 9), to be tuned on real archives.
MATCH_PROBABILITY = 0.6
MIN_MATCHES = 5
MIN_SHARE = 0.25
# laya's accuracy falls off past about 20 options per question; "other" is one of them.
MAX_OPTIONS = 20
OTHER = "other"
# Terms shown after each option's label; laya shares one budget of about 250 tokens between
# all the options of a question, so a longer list is cut off anyway.
EXAMPLE_TERMS = 3
OTHER_LABEL = "everyday talk, or any subject not listed here"
QUESTION = "Which subject is this dictated text about?"
# Rate limits.
CHECK_INTERVAL = DAY
NOTIFY_INTERVAL = 7 * DAY
# The hidden `stt dict` word the detached check runs as; never typed by a person.
WORKER_TOKEN = "__suggest"
# The classifier, pinned as task-cli pins it: `suggest_worker.py` reads laya's result
# envelope, which is not a versioned public contract yet.
LAYA = "laya>=0.3.20,<0.4"
# torch, transformers and laya in a uv environment, plus two checkpoints (843 MB and 644 MB).
SETUP_GIB = 3.0
PROBE_TIMEOUT = 300.0
SCORE_TIMEOUT = 1800.0

TITLE = "stt"
OPT_OUT = "Turn off: stt config set dict_suggest false"
SETUP_OFFER = (
    "stt can suggest dictionaries that match your last month of transcripts, entirely on this "
    f"computer. Set it up (about {SETUP_GIB:.0f} GB): stt dict suggest --setup. {OPT_OUT}"
)


# ── the record ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class State:
    """``<app_home>/dicts/suggestions.json``: when checks ran, and what was already said."""

    requested: float = 0.0  # a command last started a check; the daily limit counts from here
    finished: float = 0.0  # a check last finished
    notified: float = 0.0  # a notification last reached the user; the weekly limit
    suggested: tuple[str, ...] = ()  # dictionaries already named, never named again
    offered_setup: bool = False  # the one "set the classifier up" notification was shown
    set_up: bool = False  # `stt dict suggest --setup` finished
    outcome: str = ""  # what the last check concluded, for `stt dict suggest`

    @classmethod
    def read(cls) -> State:
        """The stored record, or a fresh one when it is missing, unreadable or malformed."""
        try:
            raw = json.loads(path().read_text(encoding="utf-8"))
            return cls(
                requested=float(raw.get("requested", 0.0)),
                finished=float(raw.get("finished", 0.0)),
                notified=float(raw.get("notified", 0.0)),
                suggested=tuple(str(item) for item in raw.get("suggested", [])),
                offered_setup=bool(raw.get("offered_setup", False)),
                set_up=bool(raw.get("set_up", False)),
                outcome=str(raw.get("outcome", "")),
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def write(self) -> None:
        from .archive import write_atomic

        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(target, json.dumps(asdict(self), sort_keys=True, ensure_ascii=False) + "\n")

    def due(self, now: float) -> bool:
        """May a check start at ``now``? A stamp in the future (the clock was set back) holds
        nothing off, as `stt mic`'s autostart backoff treats one."""
        if self.requested <= now < self.requested + CHECK_INTERVAL:
            return False
        return not self.notified <= now < self.notified + NOTIFY_INTERVAL

    def wanted(self) -> bool:
        """Is there anything a check could still say? Not once the setup offer went unanswered."""
        return self.set_up or not self.offered_setup


def path() -> Path:
    return config.app_home() / "dicts" / "suggestions.json"


def update(change: Callable[[State], State | None]) -> State:
    """Read, change and write the record under its lock, so no writer loses another's.

    Three processes write it — a finishing transcription, the detached check and `stt dict
    suggest --setup` — and each writes the whole file. Without the lock a transcription that
    read the record before a check stored its suggestion would write the old record back, and
    the same dictionary would be suggested again. ``change`` returning ``None`` writes nothing.
    """
    lock = path().with_name("suggestions.state.lock")
    with _lock(lock, blocking=True) as held:
        if not held:
            raise UsageError(
                what=f"could not lock {lock}",
                why=f"{lock} is not a regular file, or its lock cannot be taken",
                how="move it out of the way",
            )
        current = State.read()
        changed = change(current)
        if changed is None or changed == current:
            return current
        changed.write()
        return changed


# ── what a check works on ─────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Dictated:
    """One archived transcript, as the check sees it."""

    language: str | None
    text: str


@dataclass(frozen=True, slots=True)
class Topic:
    """A topic tag, and the catalog dictionaries carrying it that could still be suggested."""

    tag: str
    label: str  # what laya reads for this option: the tag's label and a few of its terms
    dictionaries: tuple[Distributable, ...]


@dataclass(frozen=True, slots=True)
class Suggestion:
    dictionary: str
    title: str
    tag: str
    matched: int
    window: int

    def message(self) -> str:
        return (
            f'Dictionary "{self.title}" matches {self.matched} of your last {self.window} '
            f"transcripts (tag {self.tag}). Install: stt dict install {self.dictionary}. {OPT_OUT}"
        )


class Scorer(Protocol):
    def ready(self) -> bool:
        """Can the classifier answer right now, without downloading anything?"""
        ...

    def score(self, pieces: list[str], groups: list[dict[str, str]]) -> list[dict[str, float]]:
        """Each piece's probability per option, every group being one `choice` question."""
        ...


Notifier = Callable[[str, str], bool]


# ── the command's half ────────────────────────────────────────────────────────
def after_transcription(
    settings: config.Settings,
    *,
    now: float | None = None,
    spawn: Callable[[], None] | None = None,
) -> None:
    """Called by the transcription command once it has written and printed its result.

    Costs one small file read when nothing is due. Never raises and never waits: the command
    has already answered, and the check runs in `worker`, which this does not wait for.
    """
    if not settings.dict_suggest:
        return
    try:
        moment = time.time() if now is None else now
        before: list[State] = []

        def stamp(state: State) -> State | None:
            if not state.wanted() or not state.due(moment):
                return None
            before.append(state)
            return replace(state, requested=moment)

        # Stamped before the child starts, so a transcription finishing meanwhile finds this
        # check in flight. A child that could not even start is no check: the stamp goes back.
        update(stamp)
        if not before:
            return
        try:
            (spawn or _spawn_worker)()
        except Exception:
            previous = before[0].requested
            update(
                lambda state: (
                    replace(state, requested=previous) if state.requested == moment else None
                )
            )
    except Exception:
        return


def _spawn_worker() -> None:
    """`stt dict __suggest` in a session of its own, detached from this terminal, nothing
    read back, with `PYTHONSAFEPATH` so the directory the command was typed in cannot supply
    a `stt_cli` of its own (see `commands/mic.py::_spawn_autostart_worker`)."""
    import subprocess

    from .live import mic_service

    subprocess.Popen(
        [*mic_service.stt_entry(), "dict", WORKER_TOKEN],
        env={**os.environ, "PYTHONSAFEPATH": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ── the child's half ──────────────────────────────────────────────────────────
def worker(*, clock: Callable[[], float] = time.time) -> int:
    """The detached check: at most one at a time, and only for a request not yet served."""
    with _lock(path().with_suffix(".lock"), blocking=False) as alone:
        if not alone:
            return EXIT_OK
        start = State.read()
        if start.finished >= start.requested:
            return EXIT_OK
        # A background job; the machine's owner is using it for something else.
        _lower_priority()
        try:
            # Asked again here, not only by the command that started this: turning the
            # setting off between the two must still mean nothing is read.
            if not config.load_settings().dict_suggest:
                result = replace(start, outcome="switched off, nothing was read")
            else:
                result = check(start, now=clock(), scorer=LayaScorer(), notifier=_post)
        except Exception as failure:
            result = replace(start, outcome=f"failed: {type(failure).__name__}: {failure}")
        # Never before the request it served, even if the clock was set back meanwhile.
        finished = max(clock(), start.requested)
        update(lambda current: _merged(current, result, finished))
    return EXIT_OK


def _merged(current: State, result: State, finished: float) -> State:
    """The check's findings laid over the record as it is NOW, which `stt dict suggest
    --setup` or a transcription may have changed while the check ran."""
    return replace(
        current,
        finished=finished,
        notified=max(current.notified, result.notified),
        suggested=(
            *current.suggested,
            *(d for d in result.suggested if d not in current.suggested),
        ),
        offered_setup=current.offered_setup or result.offered_setup,
        outcome=result.outcome,
    )


def check(state: State, *, now: float, scorer: Scorer, notifier: Notifier) -> State:
    """One check. Returns the record to store; posts at most one notification."""
    from .archive import Archive

    with Archive() as store:
        records = _window(store, now)
        if isinstance(records, str):
            return replace(state, outcome=records)
        if not scorer.ready():
            if state.offered_setup:
                return replace(state, outcome="the classifier is not set up")
            if notifier(TITLE, SETUP_OFFER):
                return replace(state, offered_setup=True, notified=now, outcome="offered the setup")
            return _unshown(state, now, "the setup offer")
        topics = candidates(state.suggested)
        if not topics:
            return replace(state, outcome="every dictionary is installed or was suggested")
        dictated = [
            Dictated(record.language, text)
            for record in records
            if (text := _text_of(store, record.run_id))
        ]
    found = decide(dictated, topics, scorer)
    if found is None:
        return replace(state, outcome=f"no topic qualified in {len(dictated)} transcripts")
    if not notifier(TITLE, found.message()):
        return _unshown(state, now, f"the suggestion of {found.dictionary}")
    return replace(
        state,
        notified=now,
        suggested=(*state.suggested, found.dictionary),
        outcome=f"suggested {found.dictionary}: {found.matched} of {found.window} transcripts",
    )


def _unshown(state: State, now: float, what: str) -> State:
    """A notification the platform did not take: not recorded as said, so it is tried again —
    but a week later, like a shown one, since a machine with no way to show one would
    otherwise classify a month of transcripts every day for nothing."""
    return replace(state, notified=now, outcome=f"{what} could not be shown")


def _text_of(store: Archive, run_id: str) -> str:
    """A transcript's words, or nothing when its file went missing since it was indexed."""
    from ._errors import SttError

    try:
        return store.load(run_id).text()
    except (SttError, OSError, ValueError, KeyError):
        return ""


def _window(store: Archive, now: float) -> list[RunRecord] | str:
    """The transcripts a check reads, newest first, or why there are not enough yet."""
    from .archive import epoch

    first = store.earliest()
    if first is None:
        return "no transcripts yet"
    age = (now - epoch(first)) / DAY
    if age < MIN_AGE_DAYS:
        return f"{age:.0f} of {MIN_AGE_DAYS} days since the first transcript"
    cutoff = datetime.fromtimestamp(now - WINDOW_DAYS * DAY, UTC).isoformat(timespec="seconds")
    records = [record for record in store.since(cutoff) if record.words > 0]
    if len(records) < MIN_TRANSCRIPTS:
        return f"{len(records)} of {MIN_TRANSCRIPTS} transcripts in the last {WINDOW_DAYS} days"
    return records[:MAX_TRANSCRIPTS]


def candidates(suggested: Sequence[str]) -> list[Topic]:
    """Topic tags of the bundled catalog's dictionaries that are neither installed nor
    already suggested. The bundled catalog, not the website: this runs offline."""
    from . import catalog, installed

    tags = catalog.read_tags()
    excluded = {*installed.installed_ids(), *suggested}
    grouped: dict[str, list[Distributable]] = {}
    for dictionary in catalog.bundled():
        if dictionary.id in excluded:
            continue
        for tag in dictionary.tags:
            known = tags.get(tag)
            if known is not None and known.kind == "topic":
                grouped.setdefault(tag, []).append(dictionary)
    return [
        Topic(tag, _described(tags[tag].label, grouped[tag]), tuple(grouped[tag]))
        for tag in sorted(grouped)
    ]


def _described(label: str, dictionaries: list[Distributable]) -> str:
    """The option text laya reads: the label, then `EXAMPLE_TERMS` of the topic's terms.

    Measured on laya's multilingual checkpoint (2026-09-30): with the bare label "Russian IT
    anglicisms", Russian small talk about a weekend scored 0.69 for that topic — the word
    "Russian" was the match, not the subject. With three of its terms after the label it
    scored 0.51, and two other pieces of small talk 0.14, while dictated medicine kept 0.99.
    """
    examples = [entry.term.term for one in dictionaries for entry in one.entries]
    shown = ", ".join(examples[:EXAMPLE_TERMS])
    return f"{label}, like {shown}" if shown else label


def decide(dictated: list[Dictated], topics: list[Topic], scorer: Scorer) -> Suggestion | None:
    """The one dictionary to suggest, or ``None`` when no topic qualifies."""
    pieces: list[str] = []
    owners: list[int] = []
    for index, one in enumerate(dictated):
        for piece in split(one.text):
            pieces.append(piece)
            owners.append(index)
    if not pieces:
        return None
    step = MAX_OPTIONS - 1
    groups = [
        {**{topic.tag: topic.label for topic in topics[at : at + step]}, OTHER: OTHER_LABEL}
        for at in range(0, len(topics), step)
    ]
    scores = scorer.score(pieces, groups)
    if len(scores) != len(pieces):
        raise ValueError(f"the classifier answered {len(scores)} pieces of {len(pieces)}")
    best: list[dict[str, float]] = [{} for _ in dictated]
    for owner, probabilities in zip(owners, scores, strict=True):
        for tag, probability in probabilities.items():
            best[owner][tag] = max(best[owner].get(tag, 0.0), probability)
    window = len(dictated)
    ranked: list[tuple[int, float, str, Distributable]] = []
    for topic in topics:
        matched = [
            (one, seen[topic.tag])
            for one, seen in zip(dictated, best, strict=True)
            if seen.get(topic.tag, 0.0) >= MATCH_PROBABILITY
        ]
        if len(matched) < MIN_MATCHES or len(matched) < MIN_SHARE * window:
            continue
        chosen = _in_their_language(topic, [one for one, _ in matched])
        if chosen is not None:
            strength = sum(probability for _, probability in matched) / len(matched)
            ranked.append((len(matched), strength, topic.tag, chosen))
    if not ranked:
        return None
    # Most transcripts first, then the more confident; the tag breaks a tie deterministically.
    count, _, tag, entry = min(ranked, key=lambda row: (-row[0], -row[1], row[2]))
    return Suggestion(entry.id, entry.title, tag, count, window)


def _in_their_language(topic: Topic, matched: list[Dictated]) -> Distributable | None:
    """The topic's dictionary in the language the matching transcripts were dictated in.

    A topic can have one dictionary per language (`medical-general`, `medical-general-ru`),
    and suggesting the Russian one to somebody who dictates medicine in English helps nobody.
    A transcript whose language was not recorded counts for every dictionary.
    """
    fits = [
        (sum(one.language is None or one.language in entry.languages for one in matched), entry)
        for entry in topic.dictionaries
    ]
    best = max((count for count, _ in fits), default=0)
    if best == 0:
        return None
    return min((entry for count, entry in fits if count == best), key=lambda entry: entry.id)


def split(text: str) -> list[str]:
    """At most `MAX_PIECES` pieces of about `PIECE_CHARS`, spread over the whole text.

    Pieces, because laya reads a few hundred tokens reliably and a transcript can be an hour
    long; spread, because the subject of a recording is not always in its first minute.
    """
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for word in text.split():
        if current and size + len(word) + 1 > PIECE_CHARS:
            pieces.append(" ".join(current))
            current, size = [], 0
        current.append(word)
        size += len(word) + 1
    if current:
        pieces.append(" ".join(current))
    if len(pieces) <= MAX_PIECES:
        return pieces
    last = len(pieces) - 1
    return [pieces[round(i * last / (MAX_PIECES - 1))] for i in range(MAX_PIECES)]


def _post(title: str, body: str) -> bool:
    from . import notify

    return notify.post(title, body)


def _lower_priority() -> None:
    # Windows has no `nice`, and a refusal changes nothing about the check.
    with contextlib.suppress(AttributeError, OSError):
        os.nice(10)


@contextmanager
def _lock(file: Path, *, blocking: bool) -> Iterator[bool]:
    """Yields whether this process holds the lock on ``file``. Non-blocking, a held lock
    yields False at once. Opened non-blocking and asked what it is, as `installed._locked`
    does, so a named pipe sitting where the lock goes cannot hang the process."""
    file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(file, os.O_CREAT | os.O_RDWR | portable.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            yield False
            return
        try:
            portable.lock(descriptor, blocking=blocking)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            portable.unlock(descriptor)
    finally:
        os.close(descriptor)


# ── the classifier ────────────────────────────────────────────────────────────
class LayaScorer:
    """laya, in whichever Python has it: ours when the `suggest` extra is installed, else a
    `uv run --with laya` environment — the route `diarize.py` takes for pyannote. Always
    offline: an environment or a checkpoint that is not already on disk is "not ready", and
    fetching it is `stt dict suggest --setup`'s job."""

    def __init__(self, route: list[str] | None = None, *, env: dict[str, str] | None = None):
        self._route = route
        self._env = env or {}

    def ready(self) -> bool:
        try:
            answered = self._ask(["--probe"], stdin=None, timeout=PROBE_TIMEOUT)
        except Exception:
            return False
        return answered.get("ready") is True

    def score(self, pieces: list[str], groups: list[dict[str, str]]) -> list[dict[str, float]]:
        questions = {
            f"topic_{index}": {"type": "choice", "instructions": QUESTION, "criteria": criteria}
            for index, criteria in enumerate(groups)
        }
        request = json.dumps({"pieces": pieces, "questions": questions}, ensure_ascii=False)
        answered = self._ask([], stdin=request, timeout=SCORE_TIMEOUT)
        if "answers" not in answered:
            raise RuntimeError(f"laya failed: {answered.get('error', answered)!r:.300}")
        # One dict per piece, the topics of every question together. Each question is its own
        # softmax, so with more than one group (over 19 topics) the probabilities of two
        # groups are not calibrated against each other; the catalog has 18 topic tags today.
        merged: list[dict[str, float]] = []
        for per_piece in answered["answers"]:
            probabilities: dict[str, float] = {}
            for name in questions:
                for option, probability in per_piece[name].items():
                    if option != OTHER:
                        probabilities[option] = float(probability)
            merged.append(probabilities)
        return merged

    def _ask(self, flags: list[str], *, stdin: str | None, timeout: float) -> dict[str, Any]:
        import asyncio

        from . import proc
        from .jsonio import last_object

        route = self._route if self._route is not None else runner(offline=True)
        if route is None:
            return {"error": "neither laya nor uv is installed"}
        env = {"PYTHONSAFEPATH": "1", **self._env}
        result = asyncio.run(
            proc.run([*route, worker_path(), *flags], timeout=timeout, env=env, stdin_text=stdin)
        )
        return last_object(result.stdout) or {"error": result.stderr.strip()[-300:]}


def runner(*, offline: bool) -> list[str] | None:
    """The interpreter that runs `suggest_worker.py`: ours when it can import laya, else uv's
    (`--offline` for a check, online for the setup). ``None``: no laya here and no uv."""
    import importlib.util

    from . import proc

    try:
        if importlib.util.find_spec("laya") is not None:
            return [sys.executable]
    except (ImportError, ValueError):
        pass
    uv = proc.which("uv")
    if uv is None:
        return None
    # `--no-project`: never the pyproject.toml of whatever directory the command ran in.
    return [
        uv, "run", "--quiet", "--no-project", *(["--offline"] if offline else []),
        "--with", LAYA, "python",
    ]  # fmt: skip


def worker_path() -> str:
    """The worker script's path, resolved without importing it — it must not be imported."""
    return str(Path(__file__).with_name("suggest_worker.py"))
