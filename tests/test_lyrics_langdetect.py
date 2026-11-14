"""Language detection tests using script, lexicon, and simulated Whisper LID."""
from __future__ import annotations

from tools.lyrics import langdetect


def test_script_summary_presence():
    summary = langdetect.script_summary(["君の名前は", "사랑해", "hello"])
    assert summary["kana"] >= 2
    assert summary["hangul"] >= 2
    assert summary["latin"] >= 5
    assert summary["lines_with_kana"] == 1
    assert summary["lines_with_hangul"] == 1


def test_tagalog_lexicon_ignores_sung_syllables():
    english = "na na na na, I will always love you"
    lexicon = langdetect.tagalog_lexicon(english)
    assert langdetect.tagalog_likely(lexicon) is False
    tagalog = "Ang pag-ibig ko ay para sa iyo, hindi kita iiwan"
    lexicon = langdetect.tagalog_lexicon(tagalog)
    assert langdetect.tagalog_likely(lexicon) is True


def test_detect_script_languages():
    ja = langdetect.detect_language("君の名前は", [])
    assert ja["language"] == "ja"
    assert ja["source"] == "script-kana"
    ko = langdetect.detect_language("사랑해", [])
    assert ko["language"] == "ko"
    assert ko["source"] == "script-hangul"


def test_detect_latin_reference_with_japanese_audio_is_preromanized():
    lid = [{"language": "ja", "probability": 0.98}]
    decision = langdetect.detect_language("Kimi no namae wa", lid)
    assert decision["language"] == "ja"
    assert decision["reference_romaji"] is True
    assert decision["source"] == "audio-ja-latin-reference"


def test_detect_tagalog_from_strong_lexicon():
    decision = langdetect.detect_language(
        "Ang pag-ibig ko ay para sa iyo, hindi kita iiwan", [])
    assert decision["language"] == "tl"
    assert decision["source"] == "tagalog-lexicon"


def test_english_with_one_tagalog_phrase_stays_english():
    decision = langdetect.detect_language(
        "I will always love you, mahal kita", [])
    assert decision["language"] == "en"


def test_taglish_with_repeated_tagalog_stays_tagalog():
    decision = langdetect.detect_language(
        "Ang pag-ibig ko, ang pag-ibig ko, hindi kita iiwan, "
        "para sa iyo, wala nang iba", [])
    assert decision["language"] == "tl"


def test_audio_only_tagalog():
    lid = [{"language": "tl", "probability": 0.91},
           {"language": "tl", "probability": 0.88}]
    decision = langdetect.detect_language("", lid)
    assert decision["language"] == "tl"
    assert decision["source"] == "audio-lid"


def test_override_wins():
    decision = langdetect.detect_language(
        "君の名前は", [], override="tl")
    assert decision["language"] == "tl"
    assert decision["source"] == "override"


def test_audio_majority_vote_and_probabilities():
    result = langdetect.choose_audio_language([
        {"language": "ja", "probability": 0.7},
        {"language": "ja", "probability": 0.6},
        {"language": "en", "probability": 0.8},
    ])
    assert result["language"] == "ja"
    assert result["votes"] == {"ja": 2, "en": 1}
    assert 0.0 < result["confidence"] <= 1.0
