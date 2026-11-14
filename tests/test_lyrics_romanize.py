"""Romanization, pre-romanized normalization, and script-gate tests."""
from __future__ import annotations

import pytest

from tools.generate_lyrics import (
    _script_gate,
    _script_gate_blocks_publication,
)
from tools.lyrics import romanize


def _japanese_deps():
    pytest.importorskip("fugashi")
    pytest.importorskip("cutlet")
    pytest.importorskip("unidic_lite")


@pytest.mark.parametrize("source,expected", [
    ("君の名前は", "Kimi no namae wa"),
    ("東京", "Toukyou"),
    ("学校", "Gakkou"),
    ("ラーメン", "Raamen"),
    ("こんにちは", "Konnichiwa"),
    ("しんいち", "Shin'ichi"),
    ("こんや", "Kon'ya"),
    ("おおきい", "Ookii"),
    ("せんせい", "Sensei"),
    ("ハロー", "Haroo"),
    ("水を飲む", "Mizu o nomu"),
    ("学校へ行く", "Gakkou e iku"),
    ("高みを目指して", "Takami o mezashite"),
    ("構わず", "Kamawazu"),
    ("蹴られた", "Kerareta"),
    ("起きても", "Okite mo"),
    ("怒って", "Okotte"),
    ("走って", "Hashitte"),
    ("ぶったぎってくぜ", "Buttagitteku ze"),
    ("怒って (怒って)", "Okotte (okotte)"),
    ("GO!!! 君の名前", "GO!!! kimi no namae"),
    ("君の名前は, 東京!", "Kimi no namae wa, toukyou!"),
])
def test_japanese_romanization_rules(source, expected):
    _japanese_deps()
    assert romanize.romanize_line(source, "ja").text == expected


def test_japanese_kanji_readings_are_recorded():
    _japanese_deps()
    result = romanize.romanize_line("東京の学校", "ja")
    assert result.text == "Toukyou no gakkou"
    assert result.readings == [
        {"text": "東京", "reading": "トウキョウ", "romaji": "toukyou"},
        {"text": "学校", "reading": "ガッコウ", "romaji": "gakkou"},
    ]


@pytest.mark.parametrize("source,expected", [
    ("사랑해", "Salanghae"),
    ("안녕하세요", "Annyeonghase-yo"),
    ("한국어", "Hangug-eo"),
    ("싶어", "Sip-eo"),
    ("보고 싶어", "Bogo sip-eo"),
    ("TT", "TT"),
    ("아이 러브 유", "A-i leobeu yu"),
    ("강남 스타일", "Gangnam seuta-il"),
])
def test_korean_romanization_fixtures(source, expected):
    pytest.importorskip("hangul_romanize")
    assert romanize.romanize_line(source, "ko").text == expected


@pytest.mark.parametrize("source,expected", [
    ("Tōkyō", "Tokyo"),
    ("Tôkyô", "Tokyo"),
    ("Nanka shiawase Chotto shiawase", "Nanka shiawase chotto shiawase"),
    ("  Koin hajiku you ni arukidasou  ", "Koin hajiku you ni arukidasou"),
])
def test_pre_romanized_normalization(source, expected):
    assert romanize.normalize_pre_romanized(source) == expected


def test_script_gate_blocks_a_leaking_line():
    leaking = [{
        "text": "君の名前",
        "words": [{"text": "君"}, {"text": "名前"}],
    }]
    gate = _script_gate(leaking, "ja")
    assert gate["passed"] is False
    assert _script_gate_blocks_publication("ja", gate) is True
    assert gate["leaks"][0]["line"] == 1

    clean = [{"text": "Kimi no namae", "words": [{"text": "Kimi"}]}]
    assert _script_gate(clean, "ja")["passed"] is True
    assert _script_gate_blocks_publication("ja", _script_gate(clean, "ja")) is False
    assert _script_gate(clean, "ko")["passed"] is True
    hangul = [{"text": "사랑해", "words": []}]
    assert _script_gate(hangul, "ko")["passed"] is False
    assert _script_gate_blocks_publication("ko", _script_gate(hangul, "ko")) is True
