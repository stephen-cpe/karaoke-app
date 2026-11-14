import json
from pathlib import Path

from tools import eval_lyrics


def test_long_word_energy_uses_audio_and_time_range(tmp_path, monkeypatch):
    """Guard the eval energy call against swapping audio for SAMPLE_RATE."""
    lyrics_dir = tmp_path / "lyrics"
    lyrics_dir.mkdir()
    stem = "test_song"
    (lyrics_dir / f"{stem}.words.json").write_text(json.dumps({
        "lines": [{
            "start": 10.0,
            "end": 15.0,
            "text": "hello world",
            "words": [
                {"text": "hello", "start": 10.0, "end": 10.5},
                {"text": "world", "start": 10.5, "end": 14.5},
            ],
        }],
        "pipeline": {"language": "en", "text_source": "lrclib-synced"},
    }), encoding="utf-8")

    monkeypatch.setattr(
        eval_lyrics, "check_sidecar",
        lambda stem, lyrics_dir: ([], [], 1, 0, 2))
    monkeypatch.setattr(eval_lyrics, "read_report", lambda *args: {})
    monkeypatch.setattr(
        eval_lyrics, "resolve_vocal_stem",
        lambda *args, **kwargs: Path("vocals.wav"))
    audio = object()
    monkeypatch.setattr(eval_lyrics, "load_vocal_audio", lambda path: audio)
    calls = []

    def fake_max_window_rms(vocal_audio, start, end, window=0.05,
                            sample_rate=16000):
        calls.append((vocal_audio, start, end, window, sample_rate))
        return 0.5

    monkeypatch.setattr(eval_lyrics, "max_window_rms", fake_max_window_rms)

    result = eval_lyrics.evaluate_song(
        stem, lyrics_dir, tmp_path / "cache", tmp_path / "reports", energy=True)

    assert result["long_words"] == 1
    assert result["long_words_supported_by_energy"] == 1
    assert calls == [(audio, 10.5, 14.5, 0.05, 16000)]
