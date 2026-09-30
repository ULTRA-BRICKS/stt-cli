"""Dictionary suggestions (#88): when a check runs, what it reads, what it says, what it never does.

Every test runs on a fake clock (``NOW``, and ``now=`` on the functions that take it), a fake
notifier that records what would have been shown, and either a fake scorer or a fake `laya`
package driven through the real worker script. Nothing loads a model or reaches the network.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stt_cli import installed, notify, suggest
from stt_cli.archive import Archive
from stt_cli.config import Settings
from stt_cli.models import EngineInfo, MediaInfo, Segment, Transcript

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC).timestamp()
DAY = suggest.DAY


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_HOME", str(tmp_path / "home"))


class Notifier:
    def __init__(self, *, accepts: bool = True) -> None:
        self.shown: list[tuple[str, str]] = []
        self.accepts = accepts

    def __call__(self, title: str, body: str) -> bool:
        self.shown.append((title, body))
        return self.accepts


class Scorer:
    """Scores a piece by keyword: a piece naming a topic's keyword gets ``probability`` for it."""

    def __init__(self, keywords: dict[str, tuple[str, float]], *, ready: bool = True) -> None:
        self.keywords = keywords
        self.is_ready = ready
        self.groups: list[list[dict[str, str]]] = []
        self.pieces: list[list[str]] = []

    def ready(self) -> bool:
        return self.is_ready

    def score(self, pieces: list[str], groups: list[dict[str, str]]) -> list[dict[str, float]]:
        self.pieces.append(pieces)
        self.groups.append(groups)
        offered = {option for group in groups for option in group} - {suggest.OTHER}
        answers = []
        for piece in pieces:
            probabilities = dict.fromkeys(offered, 0.01)
            for word, (tag, probability) in self.keywords.items():
                if word in piece and tag in offered:
                    probabilities[tag] = probability
            answers.append(probabilities)
        return answers


def _seed(count: int, *, text: str, language: str = "en", days_ago: float = 1, start: int = 0):
    """``count`` archived transcripts, each of its own recording, made ``days_ago``."""
    with Archive() as store:
        for index in range(start, start + count):
            created = datetime.fromtimestamp(NOW - days_ago * DAY, UTC)
            sha = hashlib.sha256(str(index).encode()).hexdigest()
            media = MediaInfo(path=f"/rec/{index}.m4a", sha256=sha, size_bytes=1, duration=60)
            transcript = Transcript(
                media=media,
                engine=EngineInfo(backend="whispercpp", model="large-v3-turbo"),
                language=language,
                segments=[Segment(0, 5, text)],
            )
            store.save(transcript, "f" * 16, created_at=created.isoformat(timespec="seconds"))


def _a_month_of(*, matching: int, total: int, language: str = "en", word: str = "doctor") -> None:
    _seed(1, text="the very first recording", days_ago=45, start=10_000)
    _seed(matching, text=f"the {word} said the dose was fine", language=language)
    _seed(total - matching, text="we talked about the weekend", start=matching)


MEDICAL = {"doctor": ("medical-general", 0.9)}


# ── the switch and the clock ──────────────────────────────────────────────────
def test_switched_off_nothing_starts_and_nothing_is_written() -> None:
    started: list[bool] = []
    suggest.after_transcription(
        Settings(dict_suggest=False), now=NOW, spawn=lambda: started.append(True)
    )
    assert started == []
    assert not suggest.path().exists()


def test_on_by_default_a_check_starts_at_most_once_a_day() -> None:
    started: list[float] = []

    def at(moment: float) -> None:
        suggest.after_transcription(Settings(), now=moment, spawn=lambda: started.append(moment))

    at(NOW)
    at(NOW + 23 * 3600)
    at(NOW + 25 * 3600)
    assert started == [NOW, NOW + 25 * 3600]


def test_a_week_passes_between_two_notifications() -> None:
    suggest.State(notified=NOW).write()
    assert not suggest.State.read().due(NOW + 6 * DAY)
    assert suggest.State.read().due(NOW + 7 * DAY)


def test_a_clock_set_back_holds_nothing_off() -> None:
    assert suggest.State(requested=NOW + 3 * DAY, notified=NOW + 3 * DAY).due(NOW)


def test_a_worker_that_could_not_start_is_not_counted_as_a_check() -> None:
    def refuse() -> None:
        raise OSError("fork refused")

    suggest.after_transcription(Settings(), now=NOW, spawn=refuse)
    started: list[bool] = []
    suggest.after_transcription(Settings(), now=NOW + 60, spawn=lambda: started.append(True))
    assert started == [True]


def test_an_unanswered_setup_offer_stops_further_checks() -> None:
    suggest.State(offered_setup=True).write()
    started: list[bool] = []
    suggest.after_transcription(Settings(), now=NOW, spawn=lambda: started.append(True))
    assert started == []


# ── eligibility: a month of use, on the archive's own dates ───────────────────
def test_a_month_has_to_pass_since_the_first_transcript() -> None:
    _seed(1, text="first", days_ago=29, start=500)
    _seed(20, text="the doctor said the dose was fine")
    shown, scorer = Notifier(), Scorer(MEDICAL)
    state = suggest.check(suggest.State(), now=NOW, scorer=scorer, notifier=shown)
    assert state.outcome == "29 of 30 days since the first transcript"
    assert shown.shown == [] and scorer.pieces == []


def test_ten_recordings_in_the_last_month_are_needed_and_a_rerun_is_not_a_second_one() -> None:
    _seed(1, text="first", days_ago=45, start=500)
    _seed(9, text="the doctor said the dose was fine")
    # The same nine recordings transcribed again with other settings: still nine.
    with Archive() as store:
        for record in store.since("2000-01-01T00:00:00+00:00"):
            store.save(store.load(record.run_id), "e" * 16, created_at=record.created_at)
    _seed(3, text="old news", days_ago=40, start=600)
    state = suggest.check(suggest.State(), now=NOW, scorer=Scorer(MEDICAL), notifier=Notifier())
    assert state.outcome == "9 of 10 transcripts in the last 30 days"


# ── one notification, never an install ───────────────────────────────────────
def test_a_qualifying_topic_is_named_once_with_the_reason_and_the_command(monkeypatch) -> None:
    def no_install(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the suggestion path installed something")

    monkeypatch.setattr(installed, "install", no_install)
    _a_month_of(matching=7, total=23)
    shown = Notifier()
    state = suggest.check(suggest.State(), now=NOW, scorer=Scorer(MEDICAL), notifier=shown)
    assert shown.shown == [
        (
            "stt",
            'Dictionary "General medicine: clinical terms, eponyms and abbreviations" matches 7 '
            "of your last 23 transcripts (tag medical-general). Install: stt dict install "
            "medical-general. Turn off: stt config set dict_suggest false",
        )
    ]
    assert state.suggested == ("medical-general",) and state.notified == NOW
    assert installed.installed_ids() == []


def test_a_dictionary_already_suggested_or_installed_is_never_named_again() -> None:
    _a_month_of(matching=12, total=12, language="ru")
    shown = Notifier()
    state = suggest.State(suggested=("medical-general-ru",))
    after = suggest.check(state, now=NOW, scorer=Scorer(MEDICAL), notifier=shown)
    # Only the English one is left for the topic, and these transcripts are Russian.
    assert shown.shown == []
    assert after.outcome == "no topic qualified in 12 transcripts"


def test_the_dictionary_in_the_transcripts_language_is_the_one_named() -> None:
    _a_month_of(matching=12, total=12, language="ru")
    shown = Notifier()
    suggest.check(suggest.State(), now=NOW, scorer=Scorer(MEDICAL), notifier=shown)
    assert "stt dict install medical-general-ru." in shown.shown[0][1]


def test_a_notification_that_was_not_shown_is_tried_again_a_week_later() -> None:
    """Not recorded as said — and not retried the next day either: a machine with no way to
    show one would otherwise classify a month of transcripts daily for nothing."""
    _a_month_of(matching=12, total=12)
    state = suggest.check(
        suggest.State(), now=NOW, scorer=Scorer(MEDICAL), notifier=Notifier(accepts=False)
    )
    assert state.suggested == ()
    assert not state.due(NOW + 2 * DAY) and state.due(NOW + 7 * DAY)


# ── the thresholds ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("matching", "total", "probability", "qualifies"),
    [
        (5, 20, 0.9, True),  # five, and exactly a quarter
        (4, 12, 0.9, False),  # fewer than five
        (5, 21, 0.9, False),  # five, but under a quarter of the window
        (6, 20, 0.6, True),  # the per-transcript bar is inclusive
        (6, 20, 0.59, False),
    ],
)
def test_a_topic_qualifies_on_five_matches_and_a_quarter_of_the_window(
    matching: int, total: int, probability: float, qualifies: bool
) -> None:
    dictated = [suggest.Dictated("en", "the doctor came")] * matching
    dictated += [suggest.Dictated("en", "the weekend")] * (total - matching)
    scorer = Scorer({"doctor": ("medical-general", probability)})
    found = suggest.decide(dictated, suggest.candidates(()), scorer)
    assert (found is not None) is qualifies


def test_a_transcript_matches_on_its_best_piece() -> None:
    """One topical minute in an hour of small talk still makes the recording a match."""
    long = " ".join(["weekend"] * 400) + " doctor " + " ".join(["weekend"] * 400)
    dictated = [suggest.Dictated("en", long)] * 5
    scorer = Scorer(MEDICAL)
    found = suggest.decide(dictated, suggest.candidates(()), scorer)
    assert found is not None and found.matched == 5
    assert len(scorer.pieces[0]) == 5 * suggest.MAX_PIECES


def test_no_question_offers_laya_more_than_twenty_options() -> None:
    topics = [
        suggest.Topic(f"topic-{index:02}", f"Topic {index}", suggest.candidates(())[0].dictionaries)
        for index in range(45)
    ]
    scorer = Scorer({})
    suggest.decide([suggest.Dictated("en", "anything")], topics, scorer)
    groups = scorer.groups[0]
    assert all(len(group) <= suggest.MAX_OPTIONS for group in groups)
    assert all(group[suggest.OTHER] == suggest.OTHER_LABEL for group in groups)
    offered = [tag for group in groups for tag in group if tag != suggest.OTHER]
    assert sorted(offered) == sorted(topic.tag for topic in topics)


def test_a_long_transcript_is_sampled_from_its_start_middle_and_end() -> None:
    words = [f"w{index}" for index in range(3000)]
    pieces = suggest.split(" ".join(words))
    assert len(pieces) == suggest.MAX_PIECES
    assert all(len(piece) <= suggest.PIECE_CHARS for piece in pieces)
    assert pieces[0].startswith("w0 ") and pieces[-1].endswith("w2999")


# ── laya: the real worker script, over a fake laya package ────────────────────
_FAKE_LAYA = """
class Router:
    def __init__(self, models, device, max_loaded):
        assert device == "cpu", device
        self.models = models

    def predict_batch(self, requests, batch_size=None):
        out = []
        for request in requests:
            answers = {}
            for name, question in request["questions"].items():
                keys = list(question["criteria"])
                hit = "medical-general" if "doctor" in request["state"] else "other"
                p = {k: (0.8 if k == hit else 0.2 / (len(keys) - 1)) for k in keys}
                answers[name] = {"choice": hit, "probabilities": p, "confidence": 0.8}
            out.append({"answers": answers, "routing": {"model": "english"}})
        return out
"""

_FAKE_HUB = """
import os

def snapshot_download(repo, allow_patterns, local_files_only):
    assert os.environ.get("HF_HUB_OFFLINE") == "1" and local_files_only
    return os.environ["FAKE_SNAPSHOT"]
"""


@pytest.fixture
def fake_laya(tmp_path: Path) -> dict[str, str]:
    packages = tmp_path / "site"
    for name, source in (("laya", _FAKE_LAYA), ("huggingface_hub", _FAKE_HUB)):
        (packages / name).mkdir(parents=True)
        (packages / name / "__init__.py").write_text(source)
    snapshot = tmp_path / "snapshot"
    for sub in ("", "multilingual/"):
        (snapshot / sub).mkdir(parents=True, exist_ok=True)
        (snapshot / sub / "rl_agent_config.json").write_text("{}")
        (snapshot / sub / "model.safetensors").write_text("")
    return {"PYTHONPATH": str(packages), "FAKE_SNAPSHOT": str(snapshot)}


def test_laya_results_become_per_topic_probabilities_other_left_out(fake_laya) -> None:
    scorer = suggest.LayaScorer([sys.executable], env=fake_laya)
    assert scorer.ready()
    scored = scorer.score(
        ["the doctor said", "the weekend"],
        [{"medical-general": "General medicine", "gaming": "Gaming", "other": "else"}],
    )
    assert scored == [
        {"medical-general": 0.8, "gaming": 0.1},
        {"medical-general": 0.1, "gaming": 0.1},
    ]


def test_a_month_scored_by_laya_ends_in_one_notification(fake_laya) -> None:
    _a_month_of(matching=6, total=20)
    shown = Notifier()
    scorer = suggest.LayaScorer([sys.executable], env=fake_laya)
    state = suggest.check(suggest.State(), now=NOW, scorer=scorer, notifier=shown)
    assert state.suggested == ("medical-general",), state.outcome
    assert "matches 6 of your last 20 transcripts" in shown.shown[0][1]


def test_without_laya_nothing_raises_and_the_setup_is_offered_once(tmp_path: Path) -> None:
    broken = tmp_path / "site" / "laya"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text('raise ImportError("laya is not installed")\n')
    scorer = suggest.LayaScorer([sys.executable], env={"PYTHONPATH": str(broken.parent)})
    assert scorer.ready() is False
    _a_month_of(matching=12, total=12)
    shown = Notifier()
    first = suggest.check(suggest.State(), now=NOW, scorer=scorer, notifier=shown)
    second = suggest.check(first, now=NOW + 8 * DAY, scorer=scorer, notifier=shown)
    assert shown.shown == [("stt", suggest.SETUP_OFFER)]
    assert first.offered_setup and not first.suggested
    assert second.outcome == "the classifier is not set up"


# ── the detached worker ───────────────────────────────────────────────────────
def test_the_worker_serves_a_request_once_and_records_a_failure(monkeypatch) -> None:
    class Broken:
        def ready(self) -> bool:
            raise RuntimeError("laya fell over")

    monkeypatch.setattr(suggest, "LayaScorer", Broken)
    monkeypatch.setattr(suggest, "_lower_priority", lambda: None)
    _a_month_of(matching=12, total=12)
    suggest.State(requested=NOW).write()
    assert suggest.worker(clock=lambda: NOW + 60) == 0
    served = suggest.State.read()
    assert served.finished >= served.requested and "laya fell over" in served.outcome
    replace(served, outcome="untouched").write()
    suggest.worker(clock=lambda: NOW + 120)  # nothing requested since: nothing done
    assert suggest.State.read().outcome == "untouched"


def test_switched_off_after_the_check_was_started_nothing_is_read(monkeypatch) -> None:
    from stt_cli import config

    class Untouchable:
        def ready(self) -> bool:
            raise AssertionError("the classifier was asked after the setting was turned off")

    monkeypatch.setattr(suggest, "LayaScorer", Untouchable)
    monkeypatch.setattr(suggest, "_lower_priority", lambda: None)
    _a_month_of(matching=12, total=12)
    suggest.after_transcription(Settings(), now=NOW, spawn=lambda: None)
    config.save_setting("dict_suggest", False)
    suggest.worker(clock=lambda: NOW + 60)
    assert suggest.State.read().outcome == "switched off, nothing was read"


def test_a_setup_finished_during_a_check_is_kept_with_its_suggestion(monkeypatch) -> None:
    """The check holds the record it started from for minutes; `stt dict suggest --setup`
    may write in between, and the check's final write must not put the old record back."""

    class SetUpMeanwhile(Scorer):
        def ready(self) -> bool:
            suggest.update(lambda state: replace(state, set_up=True))
            return True

    monkeypatch.setattr(suggest, "LayaScorer", lambda: SetUpMeanwhile(MEDICAL))
    monkeypatch.setattr(suggest, "_post", Notifier())
    monkeypatch.setattr(suggest, "_lower_priority", lambda: None)
    _a_month_of(matching=12, total=12)
    suggest.State(requested=NOW).write()
    suggest.worker(clock=lambda: NOW + 60)
    stored = suggest.State.read()
    assert stored.set_up and stored.suggested == ("medical-general",)


def test_a_transcription_asks_for_a_check_after_its_output_is_printed(monkeypatch, capsys) -> None:
    from stt_cli import pipeline
    from stt_cli.commands import transcribe

    asked: list[str] = []

    async def one_result(*_args: object) -> list[object]:
        return [object()]

    def emit(*_args: object) -> int:
        print("the transcript")
        return 0

    monkeypatch.setattr(pipeline, "gather", one_result)
    monkeypatch.setattr(transcribe, "_emit", emit)
    monkeypatch.setattr(
        suggest, "after_transcription", lambda settings: asked.append(capsys.readouterr().out)
    )
    assert transcribe.run(["memo.m4a"]) == 0
    assert asked == ["the transcript\n"]


def test_the_worker_does_not_turn_the_dictation_daemon_on(monkeypatch, autostart_hook) -> None:
    """`cli.main` runs `stt mic`'s first-run autostart after every command; the check a
    transcription starts is stt talking to itself and must not count as a first run."""
    from stt_cli import cli
    from stt_cli.commands import mic

    reached: list[list[str]] = []
    monkeypatch.setattr(mic, "autostart_after", reached.append)
    monkeypatch.setattr(suggest, "worker", lambda: 0)
    assert cli.main(["dict", suggest.WORKER_TOKEN]) == 0
    assert reached == []


# ── the notification commands ─────────────────────────────────────────────────
def test_macos_gets_one_osascript_banner_whose_text_cannot_escape_its_string() -> None:
    found = notify.command("stt", 'say "hi"\\ now', platform="darwin")
    assert found is not None
    assert found.argv == [
        "osascript",
        "-e",
        'display notification "say \\"hi\\"\\\\ now" with title "stt"',
    ]


def test_linux_prefers_notify_send_then_gdbus(monkeypatch) -> None:
    present = {"notify-send", "gdbus"}
    monkeypatch.setattr(
        notify.shutil, "which", lambda name: f"/usr/bin/{name}" if name in present else None
    )
    found = notify.command("stt", "-dash body", platform="linux")
    assert found is not None and found.argv == [
        "/usr/bin/notify-send", "--app-name=stt", "--", "stt", "-dash body",
    ]  # fmt: skip
    present.discard("notify-send")
    found = notify.command("stt", 'a "b"', platform="linux")
    assert found is not None
    assert found.argv[-5:] == ['"stt"', '"a \\"b\\""', "[]", "{}", "-1"]
    present.clear()
    assert notify.command("stt", "x", platform="linux") is None


def test_windows_text_travels_in_the_environment_never_in_the_script() -> None:
    import base64

    found = notify.command("stt", "</text><evil/>", platform="win32")
    assert found is not None and found.argv[0] == "powershell.exe"
    script = base64.b64decode(found.argv[-1]).decode("utf-16-le")
    assert "$env:STT_NOTIFY_BODY" in script and "evil" not in script
    assert found.env == {"STT_NOTIFY_TITLE": "stt", "STT_NOTIFY_BODY": "</text><evil/>"}


def test_a_platform_without_a_notifier_posts_nothing(monkeypatch) -> None:
    monkeypatch.setattr(notify, "command", lambda *_args, **_kwargs: None)
    assert notify.post("stt", "hello") is False


def test_the_record_survives_a_round_trip_and_a_broken_file() -> None:
    state = suggest.State(requested=1.0, suggested=("a", "b"), offered_setup=True, outcome="x")
    state.write()
    assert suggest.State.read() == state
    suggest.path().write_text("[not a record", encoding="utf-8")
    assert suggest.State.read() == suggest.State()
