"""Focused unit tests for the standalone lyric and media tools."""
import json

from tools import check_lyrics
from tools.generate_lyrics import atomic_write_text, srt_time


def test_srt_time_formats_milliseconds():
    assert srt_time(0) == "00:00:00,000"
    assert srt_time(65.4321) == "00:01:05,432"


def test_atomic_write_text_replaces_existing_file(tmp_path):
    target = tmp_path / "lyrics.srt"
    target.write_text("old", encoding="utf-8")

    atomic_write_text(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"


def test_check_sidecar_handles_malformed_lines(tmp_path):
    (tmp_path / "song.words.json").write_text(
        json.dumps({"lines": ["not an object"]}), encoding="utf-8")
    (tmp_path / "song.srt").write_text("", encoding="utf-8")

    problems, lines, lyric_count, instrumental_count, word_count = (
        check_lyrics.check_sidecar("song", tmp_path))

    assert lines == ["not an object"]
    assert "not an object" in problems[0]
    assert (lyric_count, instrumental_count, word_count) == (0, 0, 0)
