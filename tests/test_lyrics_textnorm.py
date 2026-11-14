"""Text normalization and tokenization tests for the multilingual pipeline."""
from pathlib import Path

import pytest

from tools.lyrics import textnorm

ROOT = Path(__file__).resolve().parents[1]
LYRICS = ROOT / "lyrics"


def _legacy_split(text: str):
    tokens = []
    for raw in text.split():
        norm = textnorm.legacy_norm_token(raw)
        if not norm:
            if tokens:
                tokens[-1]["text"] += raw
                tokens[-1]["norm"] = textnorm.legacy_norm_token(tokens[-1]["text"])
            continue
        tokens.append({"text": raw, "norm": norm})
    return tokens


def test_english_norm_matches_legacy_on_existing_corpus():
    files = sorted(LYRICS.glob("*.words.json"))
    if not files:
        pytest.skip("local lyrics corpus is not present")
    for path in files:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        for line in data.get("lines", []):
            text = str(line.get("text") or "")
            assert textnorm.norm_token(text, "en") == textnorm.legacy_norm_token(text)
            for word in line.get("words") or []:
                word_text = str(word.get("text") or "")
                assert (textnorm.norm_token(word_text, "en")
                        == textnorm.legacy_norm_token(word_text))


def test_english_tokenization_matches_legacy_on_existing_corpus():
    files = sorted(LYRICS.glob("*.words.json"))
    if not files:
        pytest.skip("local lyrics corpus is not present")
    for path in files:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        for line in data.get("lines", []):
            if line.get("instrumental"):
                continue
            text = str(line.get("text") or "")
            expected = _legacy_split(text)
            actual = textnorm.split_display_tokens(text, "en")
            assert [token["text"] for token in actual] == [
                token["text"] for token in expected]
            assert [token["norm"] for token in actual] == [
                token["norm"] for token in expected]


def test_tagalog_keeps_apostrophes_accents_and_hyphens():
    assert textnorm.norm_token("'yung", "tl") == "'yung"
    assert textnorm.norm_token("ñ", "tl") == "ñ"
    assert textnorm.norm_token("dáting", "tl") == "dáting"
    assert textnorm.norm_token("baliktad-hin", "tl") == "baliktadhin"
    assert textnorm.clean_text("  'yung   ñ  ", "tl") == "'yung ñ"


def test_norm_token_auto_keeps_cjk_and_hangul():
    assert textnorm.norm_token("君の名前") == "君の名前"
    assert textnorm.norm_token("사랑해") == "사랑해"
    assert textnorm.norm_token("Café") == "caf"


def test_japanese_tokenization_keeps_script_and_spacing():
    pytest.importorskip("fugashi")
    tokens = textnorm.split_display_tokens("君の名前は, 東京!", "ja")
    assert tokens
    assert all(token["norm"] for token in tokens)
    assert textnorm.join_tokens(tokens) == "君 の 名前 は, 東京!"
    assert textnorm.has_kana(textnorm.join_tokens(tokens))


def test_korean_tokenization_keeps_eojeol():
    tokens = textnorm.split_display_tokens("사랑해 안녕하세요", "ko")
    assert [token["text"] for token in tokens] == ["사랑해", "안녕하세요"]
    assert [token["norm"] for token in tokens] == ["사랑해", "안녕하세요"]


@pytest.mark.parametrize("text,language", [
    ("Hello, world!", "en"),
    ("'yung mahal ko", "tl"),
    ("君の名前は, 東京!", "ja"),
    ("사랑해 안녕하세요", "ko"),
])
def test_line_text_round_trip(text, language):
    if language == "ja":
        pytest.importorskip("fugashi")
    tokens = textnorm.split_display_tokens(text, language)
    assert textnorm.join_tokens(tokens) == textnorm.line_text(text, language)
