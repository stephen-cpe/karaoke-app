"""LRCLIB parsing, retry, cache, override, and candidate-choice tests."""
from __future__ import annotations

import json

import pytest

from tools.lyrics import reference


def test_parse_lyricsfile_reads_line_and_word_times():
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
    # /api/get is attempted twice (initial + one retry) and /api/search twice.
    assert len(calls) == 4

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
