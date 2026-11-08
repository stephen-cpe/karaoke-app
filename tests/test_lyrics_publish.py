"""Publishing tests: provenance hashes, edit protection, backups, atomic pair."""
from __future__ import annotations

import json
from pathlib import Path

from tools.lyrics import publish


def make_cues():
    return [
        {
            "start": 0.0,
            "end": 2.0,
            "text": "Hello world",
            "instrumental": False,
            "words": [
                {"text": "Hello", "start": 0.0, "end": 0.8},
                {"text": "world", "start": 0.8, "end": 1.6},
            ],
        },
        {
            "start": 3.0,
            "end": 5.0,
            "text": "Goodbye now",
            "instrumental": False,
            "words": [
                {"text": "Goodbye", "start": 3.0, "end": 3.8},
                {"text": "now", "start": 3.8, "end": 4.6},
            ],
        },
        {
            "start": 5.2,
            "end": 6.0,
            "text": "[Instrumental]",
            "instrumental": True,
            "words": [],
        },
    ]


def pipeline():
    return {
        "version": "v2-i18n",
        "language": "en",
        "text_source": "lrclib-synced",
        "timing_source": "asr",
        "needs_review": False,
        "review_reasons": [],
    }


def test_new_pair_is_written_with_provenance_hash(tmp_path):
    result = publish.write_outputs(
        "song", make_cues(), 6.0, pipeline(), tmp_path)
    assert result["written"] is True
    payload = json.loads((tmp_path / "song.words.json").read_text(encoding="utf-8"))
    assert payload["pipeline"]["version"] == "v2-i18n"
    assert payload["pipeline"]["output_sha256"] == publish.lines_sha256(payload["lines"])
    assert (tmp_path / "song.srt").is_file()
    assert (tmp_path / "song.words.json").is_file()


def test_edited_words_are_skipped_unless_overridden(tmp_path):
    result = publish.write_outputs(
        "song", make_cues(), 6.0, pipeline(), tmp_path)
    assert result["written"] is True
    words_path = tmp_path / "song.words.json"
    payload = json.loads(words_path.read_text(encoding="utf-8"))
    payload["lines"][0]["text"] = "Edited line"
    words_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    edited_bytes = words_path.read_bytes()
    skipped = publish.write_outputs(
        "song", make_cues(), 6.0, pipeline(), tmp_path)
    assert skipped["written"] is False
    assert skipped["skipped"] is True
    assert words_path.read_bytes() == edited_bytes


def test_overwrite_edited_creates_backup(tmp_path):
    publish.write_outputs("song", make_cues(), 6.0, pipeline(), tmp_path)
    words_path = tmp_path / "song.words.json"
    payload = json.loads(words_path.read_text(encoding="utf-8"))
    payload["lines"][0]["text"] = "Edited line"
    words_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    backup_dir = tmp_path / "backups"
    result = publish.write_outputs(
        "song", make_cues(), 6.0, pipeline(), tmp_path,
        overwrite_edited=True, backup_dir=backup_dir)
    assert result["written"] is True
    assert result["backups"]
    assert all(Path(path).is_file() for path in result["backups"])
    payload = json.loads(words_path.read_text(encoding="utf-8"))
    assert payload["lines"][0]["text"] == "Hello world"


def test_edited_srt_is_detected(tmp_path):
    publish.write_outputs("song", make_cues(), 6.0, pipeline(), tmp_path)
    srt_path = tmp_path / "song.srt"
    srt_path.write_text("1\n00:00:00,000 --> 00:00:02,000\nEdited\n",
                        encoding="utf-8")
    skipped = publish.write_outputs(
        "song", make_cues(), 6.0, pipeline(), tmp_path)
    assert skipped["written"] is False
    assert "srt" in skipped["reason"]


def test_legacy_pair_without_pipeline_is_skipped(tmp_path):
    words = {
        "version": 1,
        "song": "song",
        "lines": publish.canonical_lines(make_cues()),
    }
    (tmp_path / "song.words.json").write_text(
        json.dumps(words, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "song.srt").write_text(
        publish.build_srt(words["lines"]), encoding="utf-8")
    skipped = publish.write_outputs(
        "song", make_cues(), 6.0, pipeline(), tmp_path)
    assert skipped["written"] is False
    assert "provenance" in skipped["reason"]


def test_canonical_hash_ignores_editor_extra_keys():
    cues = make_cues()
    baseline = publish.lines_sha256(cues)
    cues[0]["words"][0]["editor_note"] = "keep me"
    cues[0]["editor_note"] = "keep me too"
    assert publish.lines_sha256(cues) == baseline
