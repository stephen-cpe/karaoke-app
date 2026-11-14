"""LRCLIB parsing, retry, cache, override, and candidate-choice tests."""
from __future__ import annotations

import json

import pytest

from tools.lyrics import reference


def test_parse_lyricsfile_reads_line_and_word_times():
    pytest.importorskip("yaml")
    payload = """
version: "1.0"
metadata:
  title: Test
lines:
  - text: Hello world
    start_ms: 1000
    end_ms: 3000
    words:
      - text: Hello
        start_ms: 1000
      - text: world
        start_ms: 2000
"""
    lines = reference.parse_lyricsfile(payload, "en")
    assert len(lines) == 1
    assert lines[0]["text"] == "Hello world"
    assert lines[0]["start"] == 1.0
    assert lines[0]["end"] == 3.0
    assert [word["text"] for word in lines[0]["words"]] == ["Hello", "world"]
    assert lines[0]["words"][0]["end"] == 2.0
    assert lines[0]["words"][1]["end"] == 3.0


def test_lrclib_404_is_retried_once_per_endpoint_then_cached(tmp_path, monkeypatch):
    calls = []

    def fake_http(url, params):
        calls.append(url)
        return 404, None, {}

    monkeypatch.setattr(reference.time, "sleep", lambda _seconds: None)
    client = reference.LRCLIBClient(tmp_path, min_interval=0, http_json=fake_http)
    result = client.lookup("Test", "Artist", 100)
    assert result.status == "not_found"
    assert not result.cached
    # /api/get is attempted twice (initial + one retry), /api/search twice,
    # and the title-only fallback search twice.
    assert len(calls) == 6

    cache_files = list((tmp_path / "lrclib-v2").glob("lookup-*.json"))
    assert len(cache_files) == 1
    payload = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert payload["status"] == "not_found"
    assert isinstance(payload["fetched_at"], float)

    calls.clear()
    cached = client.lookup("Test", "Artist", 100)
    assert cached.status == "not_found"
    assert cached.cached
    assert calls == []


def test_lrclib_404_retry_can_succeed(tmp_path, monkeypatch):
    responses = [
        (404, None, {}),
        (200, {"id": 1, "trackName": "Test", "artistName": "Artist",
               "duration": 100, "syncedLyrics": "[00:01.00]Hello world"}, {}),
        (200, [], {}),
    ]

    def fake_http(url, params):
        return responses.pop(0)

    monkeypatch.setattr(reference.time, "sleep", lambda _seconds: None)
    client = reference.LRCLIBClient(tmp_path, min_interval=0, http_json=fake_http)
    result = client.lookup("Test", "Artist", 100)
    assert result.status == "ok"
    assert result.records[0]["id"] == 1


def test_lrclib_5xx_is_not_cached(tmp_path, monkeypatch):
    def fake_http(url, params):
        return 503, None, {"Retry-After": "0"}

    monkeypatch.setattr(reference.time, "sleep", lambda _seconds: None)
    client = reference.LRCLIBClient(tmp_path, min_interval=0, http_json=fake_http)
    result = client.lookup("Test", "Artist", 100)
    assert result.status == "unreachable"
    assert not list((tmp_path / "lrclib-v2").glob("*.json"))


def test_retry_after_seconds():
    assert reference._retry_after_seconds({"Retry-After": "2"}) == 2.0
    assert reference._retry_after_seconds({"Retry-After": "0"}) == 0.0
    assert reference._retry_after_seconds({}) is None


def test_user_override_text_and_language(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "song.json").write_text('{"language": "tl"}', encoding="utf-8")
    (source / "song.lrc").write_text(
        "[00:01.00]Mahal kita\n[00:03.00]Mahal kita\n", encoding="utf-8")
    override = reference.user_override(source, "song")
    assert override is not None
    assert override["language"] == "tl"
    assert len(override["lines"]) == 2
    assert override["text_source"] == "user"


def test_user_override_language_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "song.json").write_text('{"language": "ja"}', encoding="utf-8")
    override = reference.user_override(source, "song")
    assert override is not None
    assert override["language"] == "ja"
    assert override["lines"] == []


def test_choose_candidate_rejects_wrong_duration_and_prefers_word_sync():
    pytest.importorskip("yaml")
    good = {
        "id": 2,
        "trackName": "Test",
        "artistName": "Artist",
        "duration": 100.0,
        "hasWordSync": True,
        "lyricsfile": """
lines:
  - text: Hello world
    start_ms: 1000
    end_ms: 3000
    words:
      - text: Hello
        start_ms: 1000
      - text: world
        start_ms: 2000
""",
    }
    bad = {
        "id": 1,
        "trackName": "Test",
        "artistName": "Artist",
        "duration": 130.0,
        "syncedLyrics": "[00:01.00]Hello world\n[00:04.00]Goodbye now\n",
    }
    chosen, scored = reference.choose_candidate(
        [bad, good], "en", 100.0, audio=None, local=None, fetch_full=None)
    assert chosen is not None
    assert chosen["record"]["id"] == 2
    assert chosen["word_sync"] is True
    assert scored[0]["record"]["id"] == 2


def test_legacy_search_cache_and_text_choice(tmp_path):
    matching = {
        "id": 1,
        "trackName": "Test",
        "artistName": "Artist",
        "duration": 10.0,
        "syncedLyrics": "[00:01.00]Hello world\n[00:02.00]Goodbye now\n",
    }
    wrong = {
        "id": 2,
        "trackName": "Test",
        "artistName": "Artist",
        "duration": 10.0,
        "syncedLyrics": "[00:01.00]Completely different\n[00:02.00]Words here\n",
    }
    cache_dir = tmp_path / "lrclib"
    cache_dir.mkdir()
    (cache_dir / "artist_test.json").write_text(
        json.dumps([wrong, matching]), encoding="utf-8")

    loaded = reference.load_legacy_search(tmp_path, "Artist", "Test")
    assert loaded == [wrong, matching]
    assert reference.load_legacy_search(tmp_path, "Artist", "Missing") is None

    local = [
        {"text": "hello", "norm": "hello", "start": 1.0, "end": 1.5},
        {"text": "world", "norm": "world", "start": 1.5, "end": 2.0},
        {"text": "goodbye", "norm": "goodbye", "start": 2.0, "end": 2.5},
        {"text": "now", "norm": "now", "start": 2.5, "end": 3.0},
    ]
    chosen, scored = reference.choose_legacy_candidate(
        [wrong, matching], "en", 10.0, local)
    assert chosen is not None
    assert chosen["record"]["id"] == 1
    assert chosen["form_name"] == "synced"
    assert scored[0]["record"]["id"] == 1
    assert len(scored) == 1
    assert scored[0]["coverage"] == 1.0


def test_artist_name_close_accepts_spong_cola_typo():
    assert reference.artist_name_close("Spong Cola", "Sponge Cola")
    assert reference.artist_name_close("sponge cola", "Sponge Cola")
    assert not reference.artist_name_close("Spong Cola", "Wolf Hotel")


def test_artist_name_close_short_name_allows_one_edit_only():
    assert reference.artist_name_close("PSY", "PSI")
    assert not reference.artist_name_close("PSY", "Sia")
    assert reference.artist_name_close("MOMOLAND", "MOMOLAN")


def test_fuzzy_artist_candidates_requires_close_artist_and_duration():
    records = [
        {"id": 1, "artistName": "Sponge Cola", "duration": 216.0},
        {"id": 2, "artistName": "Wolf Hotel", "duration": 213.7},
        {"id": 3, "artistName": "Sponge Cola", "duration": 240.0},
    ]
    matches = reference.fuzzy_artist_candidates(records, "Spong Cola", 213.7)
    assert [record["id"] for record in matches] == [1]


def test_lrclib_title_fallback_accepts_artist_typo(tmp_path, monkeypatch):
    candidate = {
        "id": 7,
        "trackName": "Tambay",
        "artistName": "Sponge Cola",
        "duration": 216.0,
        "syncedLyrics": "[00:01.00]Makikisilong lang\n",
    }

    def fake_http(url, params):
        if "api/get" in url:
            return 404, None, {}
        if params and params.get("q") == "Tambay":
            return 200, [candidate], {}
        return 200, [], {}

    monkeypatch.setattr(reference.time, "sleep", lambda _seconds: None)
    client = reference.LRCLIBClient(tmp_path, min_interval=0,
                                    http_json=fake_http)
    result = client.lookup("Tambay", "Spong Cola", 213.7)
    assert result.status == "ok"
    assert [record["id"] for record in result.records] == [7]
    assert result.artist_fuzzy_match == {
        "query": "Spong Cola", "matched": "Sponge Cola"}


def test_script_compatibility():
    assert reference.langdetect.script_compatibility("ja", "君の名前") == 1.0
    assert reference.langdetect.script_compatibility("ja", "Kimi no namae") == 0.6
    assert reference.langdetect.script_compatibility("ko", "사랑해") == 1.0
    assert reference.langdetect.script_compatibility("ko", "君の名前") == 0.0
    assert reference.langdetect.script_compatibility("tl", "mahal kita") == 1.0
    assert reference.langdetect.script_compatibility("tl", "君の名前") == 0.0


@pytest.mark.parametrize("language,text,expected", [
    ("ja", "君の名前は", "ja"),
    ("ko", "사랑해", "ko"),
])
def test_language_script_detection(language, text, expected):
    decision = reference.langdetect.detect_language(text, [])
    assert decision["language"] == expected
    assert decision["source"].startswith("script")
