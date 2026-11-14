"""Checker and evaluator integration tests for strict/provenance semantics."""
from __future__ import annotations

import json
from pathlib import Path

from tools import check_lyrics, eval_lyrics
from tools.lyrics import publish


def _write_pair(directory: Path, pipeline: dict | None = None):
    lines = [
        {
            "start": 0.0,
            "end": 1.0,
            "text": "Hello",
            "instrumental": False,
            "words": [],
        },
        {
            "start": 1.2,
            "end": 2.5,
            "text": "World",
            "instrumental": False,
            "words": [
                {"text": "World", "start": 1.2, "end": 2.0},
            ],
        },
    ]
    payload = {
        "version": 1,
        "song": "song",
        "lines": lines,
    }
    if pipeline is not None:
        payload["pipeline"] = pipeline
    (directory / "song.words.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (directory / "song.srt").write_text(
        publish.build_srt(lines), encoding="utf-8")


def test_strict_allows_explained_line_level_fallback(tmp_path):
    _write_pair(tmp_path, {
        "version": "v2-i18n",
        "language": "en",
        "line_level": [{"index": 1, "reason": "ASR timing coverage below threshold"}],
    })
    assert check_lyrics.main([
        "--lyrics-dir", str(tmp_path), "--strict"]) == 0


def test_strict_rejects_unexplained_line_level_fallback(tmp_path):
    _write_pair(tmp_path)
    assert check_lyrics.main([
        "--lyrics-dir", str(tmp_path), "--strict"]) == 1


def test_checker_resolves_provenance_vocal_stem(tmp_path):
    vocal = tmp_path / "vocals.wav"
    vocal.write_bytes(b"")
    assert check_lyrics.resolve_vocal_stem(
        "song", tmp_path, {"vocal_stem": str(vocal)}) == vocal


def test_eval_reports_line_retention_and_explained_line_level(tmp_path):
    _write_pair(tmp_path, {
        "version": "v2-i18n",
        "language": "en",
        "reference_lines": 2,
        "reference_lines_present": 1,
        "line_retention": 0.5,
        "needs_review": True,
        "review_reasons": ["reference line retention is 50.0% (target 95%)"],
        "line_level": [{"index": 1, "reason": "low coverage"}],
    })
    assert eval_lyrics.main([
        "--lyrics-dir", str(tmp_path), "--json"]) == 0
    import contextlib
    import io
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        eval_lyrics.main(["--lyrics-dir", str(tmp_path), "--json"])
    payload = json.loads(buffer.getvalue())
    song = payload["songs"][0]
    assert song["reference_lines"] == 2
    assert song["reference_lines_present"] == 1
    assert song["line_retention"] == 0.5
    assert song["line_level_lines"] == 1
    assert song["line_level_explained"] == 1
    assert song["line_level_unexplained"] == 0
