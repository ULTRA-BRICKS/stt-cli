import random

from stt_cli.benchmark import distance, errors, normalize, summarize


def oracle(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        new = [i]
        for j, y in enumerate(b, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (x != y)))
        row = new
    return row[-1]


def test_distance_against_independent_reference():
    rng = random.Random(37)
    for _ in range(1000):
        a = "".join(rng.choices("абвё abc", k=rng.randrange(40)))
        b = "".join(rng.choices("абвё abc", k=rng.randrange(40)))
        assert distance(a, b) == oracle(a, b)


def test_equal_normalization_and_no_english_number_privilege():
    assert errors("Привет, мир!", "привет Мир.")["wer"] == 0
    assert normalize("ＦＯＯ—Bar") == "foo bar"
    assert errors("24", "twenty four")["wer"] != 0


def test_empty_reference_is_a_silence_test_not_zero_wer():
    result = errors("", "invented subtitle")
    assert result["wer"] is None
    assert result["cer"] is None
    assert result["silence_insertions"] == 2


def test_weighted_quality_and_failures_are_not_averaged_away():
    def row(case, reference, hypothesis, repeat=1):
        return {
            "case": case,
            "phase": "measured",
            "ok": True,
            "wall_sec": 1,
            "duration_sec": 10,
            "repeat": repeat,
            "hypothesis": hypothesis,
            "errors": errors(reference, hypothesis),
        }

    rows = [
        row("long", "one two three four", "one two three five"),
        row("short", "one", "wrong"),
        row("short", "one", "wrong", 2),
        {"case": "failed", "phase": "measured", "ok": False},
    ]
    result = summarize(rows)
    assert result["wer"] == 2 / 5
    assert result["failures"] == 1
    assert result["quality_cases"] == 2
    assert result["missing_quality_cases"] == 1
    assert result["rtf"] == 0.1 and result["speedup"] == 10
    assert summarize([])["wer"] is None
