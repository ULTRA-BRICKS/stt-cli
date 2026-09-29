"""Installed dictionaries: the sources they come from, the commands, and the effective glossary.

What these guard is the promise `stt dict install` makes: a whole topic's terms start being
corrected in every transcription, the user's own dictionary still wins every conflict, and a
transcript cached before the install is never served as if it had been made after it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path

import pytest

from stt_cli import archive, catalog, dictionary, installed, pipeline, sources
from stt_cli._errors import (
    EXIT_INTERNAL,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_UNKNOWN_ITEM,
    NetworkError,
    SttError,
    UsageError,
)
from stt_cli.cli import main
from stt_cli.config import Settings
from stt_cli.models import EngineInfo, MediaInfo, Segment, Transcript

FIXTURES = Path(__file__).parent / "fixtures" / "dictionaries"
QUIET = pipeline.Reporter(quiet=True)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("STT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _personal(*terms: dictionary.Term) -> None:
    dictionary.save(dictionary.Dictionary(terms=list(terms)))


def _fixture_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "catalog"
    shutil.copytree(FIXTURES, copy)
    return copy


def _distributable(ident: str, *terms: tuple[str, list[str]]) -> catalog.Distributable:
    document = {
        "id": ident,
        "version": 1,
        "title": ident,
        "tags": ["gaming"],
        "languages": ["en"],
        "license": "CC0-1.0",
        "source": "written for this test",
        "terms": [
            {"id": f"t{position}", "term": term, "aka": aka}
            for position, (term, aka) in enumerate(terms)
        ],
    }
    blob = json.dumps(document).encode("utf-8")
    return catalog.parse(blob, origin=ident, tags=catalog.read_tags())


def _corrected(text: str) -> str:
    """``text`` after the pipeline's own dictionary stage, with the glossary `_resolve` read."""
    settings, terms = pipeline._resolve(Settings(), QUIET)
    media = MediaInfo(path="rec.wav", sha256="a" * 64, size_bytes=1, duration=2.0)
    transcript = Transcript(media=media, engine=EngineInfo(backend="x", model="y"))
    transcript.segments = [Segment(start=0.0, end=2.0, text=text, confidence=0.9)]
    pipeline._apply_dictionary(transcript, settings, terms, QUIET)
    return transcript.segments[0].text


def _fingerprint() -> str:
    settled, _ = pipeline._resolve(Settings(), QUIET)
    return archive.fingerprint(settled)


class _Site:
    """A fake marketplace: serves the fixture catalog, and records what was asked for."""

    def __init__(self, directory: Path = FIXTURES) -> None:
        self.directory = directory
        self.asked: list[str] = []
        self.tamper = False

    def __call__(self, url: str, limit: int) -> bytes:
        self.asked.append(url)
        name = url.rsplit("/", 1)[1]
        if name == "dictionaries":
            return (self.directory / catalog.INDEX).read_bytes()
        blob = (self.directory / f"{name}.json").read_bytes()
        return blob.replace(b"ConLoca", b"C0nLoca") if self.tamper else blob


# ── the commands ──────────────────────────────────────────────────────────────


def test_search_lists_matching_bundled_dictionaries_and_marks_the_installed_ones(capsys) -> None:
    assert main(["dict", "search", "MEDIC"]) == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    rows = [line for line in lines if "medical-general" in line]
    assert len(rows) == 2
    assert all(not row.startswith("*") for row in rows)
    assert "General medicine" in rows[0] and "[medical-general" in rows[0]
    assert not any("gaming" in line for line in lines)

    assert main(["dict", "install", "medical-general"]) == EXIT_OK
    capsys.readouterr()
    assert main(["dict", "search", "medic"]) == EXIT_OK
    rows = [line for line in capsys.readouterr().out.splitlines() if "medical-general" in line]
    assert [row[0] for row in rows] == ["*", " "]


def test_search_matches_id_title_and_tag_but_never_term_text() -> None:
    index = sources.BundledSource(FIXTURES).index()

    def found(query: str) -> list[str]:
        return [entry.id for entry in index.entries if entry.matches(query)]

    assert found("tools") == ["fixture-tools"]  # the id
    assert found("GAME NAMES") == ["fixture-games"]  # the title, case-insensitively
    assert found("vendor") == ["fixture-tools"]  # a tag
    assert found("ConLoca") == []  # a term: the index holds none


def test_install_copies_the_bundled_file_byte_for_byte_and_installed_lists_it(capsys) -> None:
    assert main(["dict", "install", "medical-general"]) == EXIT_OK
    assert "installed medical-general v1" in capsys.readouterr().out

    target = installed.directory() / "medical-general.json"
    assert target.read_bytes() == (catalog.DIRECTORY / "medical-general.json").read_bytes()
    assert main(["dict", "installed"]) == EXIT_OK
    listing = capsys.readouterr().out
    assert "medical-general" in listing and "v1" in listing


def test_dict_list_prints_the_same_personal_terms_before_and_after_an_install(capsys) -> None:
    _personal(dictionary.Term(term="ConLoca", aka=["ConLog"]))
    assert main(["dict", "list"]) == EXIT_OK
    before = capsys.readouterr().out

    assert main(["dict", "install", "medical-general"]) == EXIT_OK
    capsys.readouterr()
    assert main(["dict", "list"]) == EXIT_OK
    assert capsys.readouterr().out == before


def test_reinstalling_says_whether_the_version_went_up_or_down(
    tmp_path, capsys, monkeypatch
) -> None:
    from stt_cli.commands import dict_cmd

    older = _fixture_copy(tmp_path)
    path = older / "fixture-tools.json"
    path.write_text(path.read_text("utf-8").replace('"version": 2', '"version": 1'), "utf-8")
    catalog.write_index(older)
    source = {"now": sources.BundledSource(older)}
    monkeypatch.setattr(dict_cmd, "_source", lambda args: source["now"])

    assert main(["dict", "install", "fixture-tools"]) == EXIT_OK
    source["now"] = sources.BundledSource(FIXTURES)
    assert main(["dict", "install", "fixture-tools"]) == EXIT_OK
    assert "upgraded fixture-tools from v1 to v2" in capsys.readouterr().out

    source["now"] = sources.BundledSource(older)
    assert main(["dict", "install", "fixture-tools"]) == EXIT_OK
    assert "replaced fixture-tools v2 with the older v1" in capsys.readouterr().out

    assert main(["dict", "install", "fixture-tools"]) == EXIT_OK
    assert "reinstalled fixture-tools v1 (3 terms), already the latest" in capsys.readouterr().out


def test_search_by_tag_ignores_case_and_says_when_nothing_matches(capsys) -> None:
    assert main(["dict", "search", "--tag", "DRUG-NAMES"]) == EXIT_OK
    rows = [line for line in capsys.readouterr().out.splitlines() if "[" in line]
    assert rows and all("drug-names" in row for row in rows)

    assert main(["dict", "search", "medic", "--tag", "drug-names"]) == EXIT_OK
    assert "no dictionary in" in capsys.readouterr().out  # the query and the tag must both hold

    assert main(["dict", "search", "zzzz-no-such-topic"]) == EXIT_OK
    assert "no dictionary in" in capsys.readouterr().out


def test_installed_reports_the_merge_and_says_when_the_dictionary_is_off(capsys) -> None:
    from stt_cli import config

    _personal(dictionary.Term(term="Figma"))
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
    assert main(["dict", "installed"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "warning: fixture-tools: 'Figma' is left out" in out
    assert "turned off" not in out

    config.save_setting("dictionary", False)
    assert main(["dict", "installed"]) == EXIT_OK
    assert "turned off in the settings, so none of these is used" in capsys.readouterr().out


def test_uninstall_with_nothing_installed_says_there_is_nothing(capsys) -> None:
    assert main(["dict", "uninstall", "fixture-tools"]) == EXIT_UNKNOWN_ITEM
    assert "there are no installed dictionaries" in capsys.readouterr().err


def test_an_unknown_id_names_the_closest_ones_and_the_search_command(capsys) -> None:
    assert main(["dict", "install", "medical"]) == EXIT_UNKNOWN_ITEM
    err = capsys.readouterr().err
    assert "medical-general" in err and "stt dict search" in err
    assert not installed.directory().exists() or not any(installed.directory().iterdir())


def test_uninstall_finds_the_file_by_listing_never_by_joining_the_argument(capsys) -> None:
    from stt_cli import config

    config.ensure_dirs()
    config.config_path().write_text("{}", "utf-8")
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
    # `dicts/installed/../../config` + ".json" is exactly `config.json`.
    assert main(["dict", "uninstall", "../../config"]) == EXIT_UNKNOWN_ITEM
    assert "stt dict installed" in capsys.readouterr().err
    assert config.config_path().exists()


def test_uninstall_deletes_the_dictionary_and_keeps_the_users_overlay(capsys, monkeypatch) -> None:
    from stt_cli.commands import dict_cmd

    monkeypatch.setattr(dict_cmd, "_source", lambda args: sources.BundledSource(FIXTURES))
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
    overlay = installed.overlay_path("fixture-tools")
    overlay.parent.mkdir(parents=True)
    overlay.write_text("base_version: 2\n", "utf-8")

    # Reinstalling under an overlay says so: the overlay was written against the old copy.
    assert main(["dict", "install", "fixture-tools"]) == EXIT_OK
    assert f"your overlay {overlay}" in capsys.readouterr().out

    assert main(["dict", "uninstall", "fixture-tools"]) == EXIT_OK
    assert str(overlay) in capsys.readouterr().out
    assert installed.installed_ids() == []
    assert overlay.read_text("utf-8") == "base_version: 2\n"


def test_the_overlay_lives_under_xdg_config_home(tmp_path) -> None:
    assert installed.overlay_path("medical-general") == (
        tmp_path / "xdg" / "stt-cli" / "dicts" / "overlays" / "medical-general.yaml"
    )


def test_show_lists_the_terms_of_an_installed_dictionary_and_what_the_merge_left_out(
    capsys,
) -> None:
    _personal(dictionary.Term(term="Figma"))
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
    assert main(["dict", "show", "fixture-tools"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "ConLoca (heard as: ConLog, ConLoka)" in out and "Figma" in out
    assert "warning: fixture-tools: 'Figma' is left out, because your dictionary" in out
    assert main(["dict", "show", "fixture-games"]) == EXIT_UNKNOWN_ITEM


def test_check_explains_the_effective_glossary_and_nothing_when_it_is_off(capsys) -> None:
    from stt_cli import config

    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
    assert main(["dict", "check", "we", "shipped", "Konloka"]) == EXIT_OK
    assert "ConLoca" in capsys.readouterr().out

    config.save_setting("dictionary", False)
    assert main(["dict", "check", "we", "shipped", "Konloka"]) == EXIT_OK
    assert "turned off" in capsys.readouterr().out


# ── the effective glossary ────────────────────────────────────────────────────


def test_an_installed_term_is_corrected_by_the_pipeline_without_a_personal_entry() -> None:
    assert _corrected("the scan showed pilonephritis") == "the scan showed pilonephritis"
    assert main(["dict", "install", "medical-general"]) == EXIT_OK
    assert _corrected("the scan showed pilonephritis") == "the scan showed pyelonephritis"
    assert not dictionary.path().exists()


def test_the_personal_entry_wins_a_name_collision_whole() -> None:
    _personal(dictionary.Term(term="Figma", aka=["Figmah"], note="mine"))
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")

    terms, warnings = installed.load_effective(Settings())
    figma = [term for term in terms.terms if term.term == "Figma"]
    assert figma == [dictionary.Term(term="Figma", aka=["Figmah"], note="mine")]
    # Nothing of the dropped entry is merged in: its alias is not corrected.
    assert dictionary.correct_text("Figmah and Vigma", terms) == "Figma and Vigma"
    assert any("fixture-tools" in w and "your dictionary" in w and "Figma" in w for w in warnings)


def test_an_alias_already_claimed_is_dropped_from_the_later_entry_only() -> None:
    """Kept on both, `_alias_index` would skip it for both and correct it to nothing."""
    _personal(dictionary.Term(term="Sketch", aka=["Vigma"]))
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")

    terms, warnings = installed.load_effective(Settings())
    assert dictionary.correct_text("Vigma", terms) == "Sketch"
    assert terms.find("Figma") == dictionary.Term(term="Figma", aka=[], note="a design tool")
    assert any("'Vigma'" in w and "fixture-tools" in w for w in warnings)


def test_an_earlier_installed_dictionary_wins_over_a_later_one() -> None:
    later = _distributable("zz-later", ("Factorio", ["factor eye"]))
    earlier = _distributable("aa-earlier", ("Factorio", ["factor io"]))

    terms, warnings = installed.merge(dictionary.Dictionary(), [later, earlier])
    assert terms.terms == [dictionary.Term(term="Factorio", aka=["factor io"])]
    assert warnings == [
        "zz-later: 'Factorio' is left out, because aa-earlier already has that spelling"
    ]


def test_an_alias_two_installed_dictionaries_share_stays_with_the_earlier_one() -> None:
    earlier = _distributable("aa-earlier", ("Gizmo", ["Vigma"]))
    later = _distributable("zz-later", ("Figma", ["Vigma", "Figmah"]))

    terms, warnings = installed.merge(dictionary.Dictionary(), [later, earlier])
    assert dictionary.correct_text("Vigma and Figmah", terms) == "Gizmo and Figma"
    assert terms.find("Figma") == dictionary.Term(term="Figma", aka=["Figmah"])
    assert any("'Vigma'" in w and "aa-earlier" in w for w in warnings)


def test_a_glossary_past_the_phonetic_screen_says_how_many_terms_it_left_out() -> None:
    personal = dictionary.Dictionary(
        terms=[dictionary.Term(term=f"Personal{n:03d}") for n in range(480)]
    )
    extra = _distributable("extra", *((f"Installed{n:03d}", []) for n in range(30)))

    _, warnings = installed.merge(personal, [extra])
    assert any(w.startswith("10 term(s) past the first 500") for w in warnings)


def test_a_glossary_past_the_alias_pattern_says_how_many_spellings_it_left_out() -> None:
    per_term = dictionary.MAX_ALIASES
    count = dictionary.MAX_SCREENED_TERMS + 1
    terms = [
        dictionary.Term(term=f"Term{n:03d}", aka=[f"t{n}x{k}" for k in range(per_term)])
        for n in range(count)
    ]

    _, warnings = installed.merge(dictionary.Dictionary(terms=terms), [])
    left_out = count * per_term - dictionary.MAX_ALIAS_BRANCHES
    assert left_out > 0
    assert f"{left_out} recorded spelling(s) past the first" in " ".join(warnings)


def test_spellings_the_alias_pattern_skips_anyway_are_not_blamed_on_its_cap() -> None:
    """Every entry claims the same spellings, so none of them is corrected — by the
    claimed-twice rule, not by the cap, and the cap warning must not say otherwise."""
    shared = [f"shared{k}" for k in range(dictionary.MAX_ALIASES)]
    terms = [
        dictionary.Term(term=f"Term{n:03d}", aka=list(shared))
        for n in range(dictionary.MAX_SCREENED_TERMS + 1)
    ]

    _, warnings = installed.merge(dictionary.Dictionary(terms=terms), [])
    assert not any("recorded spelling(s)" in w for w in warnings)


def test_the_pipeline_reports_what_the_merge_left_out(capsys) -> None:
    _personal(dictionary.Term(term="Figma"))
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
    pipeline._resolve(Settings(), pipeline.Reporter())
    assert "stt: dictionary: fixture-tools: 'Figma' is left out" in capsys.readouterr().err


def test_with_the_dictionary_off_nothing_installed_is_even_read() -> None:
    installed.directory().mkdir(parents=True)
    (installed.directory() / "broken.json").write_text("{not json", "utf-8")

    terms, warnings = installed.load_effective(Settings(dictionary=False))
    assert (terms.terms, warnings) == ([], [])
    with pytest.raises(UsageError) as broken:
        installed.load_effective(Settings())
    assert "stt dict uninstall broken" in broken.value.how


def test_a_dictionary_uninstalled_between_the_listing_and_the_read_is_left_out(
    monkeypatch,
) -> None:
    """`stt dict uninstall` in another terminal must not abort a transcription mid-read."""
    installed.install(sources.BundledSource(FIXTURES), "fixture-games")
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
    listed = installed.installed_ids()
    monkeypatch.setattr(installed, "installed_ids", lambda: listed)
    (installed.directory() / "fixture-tools.json").unlink()

    assert [adopted.id for adopted in installed.read_all()] == ["fixture-games"]
    installed.load_effective(Settings())
    with pytest.raises(SttError) as gone:
        installed.read("fixture-tools")
    assert gone.value.exit_code == EXIT_UNKNOWN_ITEM


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root writes anywhere")
def test_a_read_only_install_directory_is_an_error_not_a_traceback() -> None:
    installed.install(sources.BundledSource(FIXTURES), "fixture-games")
    installed.directory().chmod(0o500)
    try:
        with pytest.raises(UsageError) as install_refused:
            installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
        with pytest.raises(UsageError) as uninstall_refused:
            installed.uninstall("fixture-games")
    finally:
        installed.directory().chmod(0o700)
    assert "could not write" in install_refused.value.what
    assert "could not delete" in uninstall_refused.value.what
    assert installed.installed_ids() == ["fixture-games"]


# ── the cache fingerprint ─────────────────────────────────────────────────────


def test_installing_and_uninstalling_change_the_fingerprint_and_back() -> None:
    _personal(dictionary.Term(term="ConLoca"))
    before = _fingerprint()
    # Nothing installed: exactly the personal digest, so every archived run stays valid.
    assert pipeline._resolve(Settings(), QUIET)[0].dict_digest == dictionary.load().digest()

    installed.install(sources.BundledSource(FIXTURES), "fixture-games")
    after = _fingerprint()
    assert after != before

    assert main(["dict", "uninstall", "fixture-games"]) == EXIT_OK
    assert _fingerprint() == before


def test_reformatting_or_renumbering_an_installed_file_keeps_the_fingerprint() -> None:
    installed.install(sources.BundledSource(FIXTURES), "fixture-tools")
    target = installed.directory() / "fixture-tools.json"
    first = _fingerprint()

    document = json.loads(target.read_text("utf-8"))
    document["version"] = 7
    target.write_text(json.dumps(document, indent=4, ensure_ascii=False), "utf-8")
    assert _fingerprint() == first

    document["terms"][0]["aka"].append("Konloka")
    target.write_text(json.dumps(document), "utf-8")
    assert _fingerprint() != first


# ── the sources ───────────────────────────────────────────────────────────────


def test_the_fixture_index_is_what_the_fixture_files_say_byte_for_byte() -> None:
    """The same fixtures feed the website's API tests, which serve this index unchanged."""
    rendered = catalog.render_index(catalog.bundled(FIXTURES))
    assert rendered == (FIXTURES / catalog.INDEX).read_text("utf-8")


def test_a_remote_install_asks_the_api_and_writes_the_verified_bytes() -> None:
    site = _Site()
    installed.install(sources.RemoteSource("https://stt.example", get=site), "fixture-tools")

    assert site.asked == [
        "https://stt.example/api/dictionaries",
        "https://stt.example/api/dictionaries/fixture-tools",
    ]
    written = (installed.directory() / "fixture-tools.json").read_bytes()
    assert written == (FIXTURES / "fixture-tools.json").read_bytes()


def test_the_remote_flag_asks_the_marketplace_site(monkeypatch, capsys) -> None:
    site = _Site()
    monkeypatch.setattr(sources, "_https_get", site)

    assert main(["dict", "install", "fixture-tools", "--remote"]) == EXIT_OK
    assert site.asked == [
        f"{sources.MARKETPLACE}/api/dictionaries",
        f"{sources.MARKETPLACE}/api/dictionaries/fixture-tools",
    ]
    assert installed.installed_ids() == ["fixture-tools"]
    capsys.readouterr()
    assert main(["dict", "search", "fixture", "--remote"]) == EXIT_OK
    assert "* fixture-tools" in capsys.readouterr().out


def test_a_remote_file_that_does_not_match_the_index_installs_nothing() -> None:
    site = _Site()
    site.tamper = True
    with pytest.raises(NetworkError) as refused:
        installed.install(sources.RemoteSource("https://stt.example", get=site), "fixture-tools")

    assert refused.value.exit_code == EXIT_NETWORK
    assert "nothing was installed" in refused.value.how
    assert installed.installed_ids() == []


def test_an_unreachable_site_is_a_network_error(tmp_path) -> None:
    with socket.socket() as probe:  # a loopback port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    source = sources.RemoteSource(f"https://127.0.0.1:{port}")

    with pytest.raises(NetworkError) as refused:
        installed.install(source, "fixture-tools")
    assert refused.value.exit_code == EXIT_NETWORK
    assert "without --remote" in refused.value.how


def test_a_plain_http_marketplace_is_refused() -> None:
    with pytest.raises(UsageError):
        sources.RemoteSource("http://stt.example")


def test_a_redirect_is_followed_only_to_another_https_address() -> None:
    import urllib.error
    import urllib.request
    from email.message import Message

    handler = sources._https_only()()
    asked = urllib.request.Request("https://stt.example/api/dictionaries")
    moved = handler.redirect_request(
        asked, None, 302, "Found", Message(), "https://cdn.example/dictionaries"
    )
    assert moved is not None and moved.full_url == "https://cdn.example/dictionaries"
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(asked, None, 302, "Found", Message(), "http://cdn.example/x")


def test_a_file_that_is_not_what_its_index_row_says_installs_nothing(tmp_path) -> None:
    """The hash pins the file, not the row: a row naming another version must still be caught."""
    lying = _fixture_copy(tmp_path)
    index = lying / catalog.INDEX
    document = json.loads(index.read_text("utf-8"))
    row = next(row for row in document["dictionaries"] if row["id"] == "fixture-tools")
    row["version"] = 3
    index.write_text(json.dumps(document), "utf-8")

    with pytest.raises(UsageError) as refused:
        installed.install(sources.BundledSource(lying), "fixture-tools")
    assert "disagree" in refused.value.why
    assert installed.installed_ids() == []


def test_a_pipe_where_the_install_lock_goes_is_refused_instead_of_waited_on() -> None:
    import os
    import subprocess
    import sys

    lock = installed.directory().parent / "installed.lock"
    lock.parent.mkdir(parents=True)
    os.mkfifo(lock)
    probe = (
        "from pathlib import Path\n"
        "from stt_cli import installed, sources\n"
        "try:\n"
        f"    installed.install(sources.BundledSource(Path({str(FIXTURES)!r})), 'fixture-tools')\n"
        "except Exception as exc:\n"
        "    print(exc.why)\n"
    )
    # In a child with a deadline: the regression this guards is a hang, not a failure.
    asked = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert "not a regular file" in asked.stdout, asked.stderr


def test_a_hand_edited_bundled_file_is_a_damaged_package_not_a_network_error(tmp_path) -> None:
    damaged = _fixture_copy(tmp_path)
    path = damaged / "fixture-tools.json"
    path.write_text(path.read_text("utf-8").replace("a design tool", "a drawing tool"), "utf-8")

    with pytest.raises(SttError) as refused:
        installed.install(sources.BundledSource(damaged), "fixture-tools")
    assert refused.value.exit_code == EXIT_INTERNAL
    assert "reinstall stt" in refused.value.how
    assert installed.installed_ids() == []


def test_an_index_in_a_newer_format_asks_for_an_upgrade() -> None:
    blob = json.dumps({"format": catalog.INDEX_FORMAT + 1, "dictionaries": []}).encode()
    with pytest.raises(UsageError) as refused:
        catalog.parse_index(blob, origin="index")
    assert refused.value.how == "upgrade stt"


def test_an_empty_index_is_a_broken_catalog_not_an_empty_search() -> None:
    blob = json.dumps({"format": catalog.INDEX_FORMAT, "dictionaries": []}).encode()
    with pytest.raises(UsageError) as refused:
        catalog.parse_index(blob, origin="index")
    assert "lists no dictionaries" in refused.value.what


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "../../config"),
        ("id", "fixture-tools"),  # the id of the second row: one row per dictionary
        ("title", "Innocent\x1b]0;owned\x07"),
        ("sha256", "abc"),
        ("format", True),  # a bool is an int in Python, and True would read as format 1
    ],
)
def test_a_hostile_index_row_is_refused(field: str, value: object) -> None:
    document = json.loads((FIXTURES / catalog.INDEX).read_text("utf-8"))
    if field == "format":
        document["format"] = value
    else:
        document["dictionaries"][0][field] = value
    with pytest.raises(UsageError):
        catalog.parse_index(json.dumps(document).encode(), origin="index")
