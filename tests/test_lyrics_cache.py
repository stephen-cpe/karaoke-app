"""Content-addressed cache tests, including the stale-transcript correction."""
from __future__ import annotations

import json

from tools.lyrics import cache


def _key(**overrides):
    base = dict(
        vocal_hash="vocal-hash",
        original_hash="original-hash",
        model="large-v3",
        language="ja",
        prompt="君の名前",
        chunk=55.0,
        overlap=8.0,
        transcriber_version="v2-i18n",
        beam_size=5,
        temperature=0.0,
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    base.update(overrides)
    return cache.transcript_cache_key(**base)


def test_stem_cache_is_keyed_by_original_content(tmp_path):
    first = tmp_path / "original-a.mp3"
    second = tmp_path / "original-b.mp3"
    first.write_bytes(b"first audio")
    second.write_bytes(b"second audio")
    first_hash = cache.sha256_file(first)
    second_hash = cache.sha256_file(second)
    assert first_hash != second_hash
    assert (cache.stem_cache_path(tmp_path, "song", first_hash)
            != cache.stem_cache_path(tmp_path, "song", second_hash))
    assert first_hash[:12] in str(cache.stem_cache_path(tmp_path, "song", first_hash))
    assert "htdemucs" in str(cache.stem_cache_path(tmp_path, "song", first_hash))


def test_transcript_key_includes_language_model_and_settings():
    base = _key()
    assert _key(language="en") != base
    assert _key(model="medium") != base
    assert _key(prompt="different prompt") != base
    assert _key(chunk=30.0) != base
    assert _key(overlap=4.0) != base
    assert _key(beam_size=1) != base
    assert _key(temperature=0.2) != base
    assert _key(condition_on_previous_text=True) != base
    assert _key(word_timestamps=False) != base
    assert _key(vocal_hash="other vocal") != base
    assert _key(original_hash="other original") != base


def test_cached_transcript_with_different_language_is_a_miss(tmp_path):
    path = tmp_path / "transcript.json"
    key = _key(language="ja")
    cache.save_transcript_cache(path, key, {"language": "ja", "segments": []})
    assert cache.load_transcript_cache(path, key) == {
        "language": "ja", "segments": []}
    other_key = _key(language="en")
    assert cache.load_transcript_cache(path, other_key) is None


def test_cached_transcript_with_different_model_is_a_miss(tmp_path):
    path = tmp_path / "transcript.json"
    key = _key(model="large-v3")
    cache.save_transcript_cache(path, key, {"language": "ja", "segments": []})
    other_key = _key(model="medium")
    assert cache.load_transcript_cache(path, other_key) is None


def test_new_cache_path_never_collides_with_legacy_stem_cache(tmp_path):
    key = _key()
    path = cache.transcript_cache_path(tmp_path, "song", key)
    assert path.name != "song.json"
    assert path.name.startswith("song-")
    assert path.name.endswith(".json")


def test_cache_file_carries_key_and_created_at(tmp_path):
    path = tmp_path / "transcript.json"
    key = _key()
    cache.save_transcript_cache(path, key, {"language": "ja", "segments": []})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["key"] == key
    assert isinstance(payload["created_at"], int)
    assert payload["data"]["language"] == "ja"
