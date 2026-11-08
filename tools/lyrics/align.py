"""Forced alignment adapter over the MMS CTC model.

The optional aligner dependencies are imported lazily so the web app tests and
CI can import this module without torch/transformers/ctc-forced-aligner.
"""
from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from . import textnorm

log = logging.getLogger("lyrics")

DEFAULT_MODEL = "MahmoudAshraf/mms-300m-1130-forced-aligner"
MIN_WORD_SCORE = -4.0
MIN_WORD_SECONDS = 0.02
LINE_COVERAGE_TARGET = 0.80
STAR_FREQUENCY = "edges"


@dataclass
class WordTiming:
    index: int
    line: int
    token: int
    text: str
    start: float
    end: float
    score: float
    aligned: bool
    inferred: bool = False


@dataclass
class AlignmentResult:
    words: list[WordTiming]
    total_words: int
    aligned_words: int
    coverage: float
    mean_score: float
    low_score_words: int
    seconds: float = 0.0


@dataclass
class CandidateAlignment:
    summary: dict[str, Any]
    lines: list[dict[str, Any]] = field(default_factory=list)


def _alignment_text(text: str, vocab: set[str]) -> str:
    """Reduce one display token to characters the MMS tokenizer knows."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return "".join(char for char in ascii_text if char in vocab)


class AlignSession:
    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self.model = None
        self.tokenizer = None
        self.device = None
        self.dtype = None
        self.char_vocab: set[str] = set()
        self.load_seconds = 0.0
        self.seconds = 0.0
        self._torch = None

    def load(self):
        if self.model is not None:
            return
        try:
            import torch
            from ctc_forced_aligner import load_alignment_model
        except ImportError as exc:
            raise RuntimeError(
                "forced alignment needs the optional lyrics-i18n dependencies; "
                "install requirements-lyrics-i18n.txt and the pinned "
                "ctc-forced-aligner package") from exc
        self._torch = torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        started = time.perf_counter()
        model, tokenizer = load_alignment_model(
            device, self.model_name, None, dtype)
        self.model = model
        self.tokenizer = tokenizer
        self.device = device
        self.dtype = dtype
        self.load_seconds = time.perf_counter() - started
        self.char_vocab = {
            key.lower() for key in tokenizer.get_vocab() if len(key) == 1
        }
        log.info("  loaded aligner %s (%s) in %.1fs",
                 self.model_name, device.upper(), self.load_seconds)

    def close(self):
        self.model = None
        self.tokenizer = None
        self.device = None
        self.dtype = None
        self.char_vocab = set()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def prepare_emissions(self, audio_np):
        self.load()
        import numpy as np
        from ctc_forced_aligner import generate_emissions
        torch = self._torch
        array = np.ascontiguousarray(audio_np, dtype=np.float32).copy()
        waveform = torch.from_numpy(array).to(device=self.device, dtype=self.dtype)
        started = time.perf_counter()
        emissions, stride = generate_emissions(
            self.model, waveform, window_length=30, context_length=2, batch_size=4)
        self.seconds = time.perf_counter() - started
        return emissions, stride

    def _build_sequence(self, tokens: list[dict[str, Any]], language: str):
        tokens_starred: list[str] = []
        text_starred: list[str] = []
        align_indices: list[int] = []
        for index, token in enumerate(tokens):
            norm = str(token.get("norm") or textnorm.norm_token(
                str(token.get("text", "")), language))
            chars = _alignment_text(norm, self.char_vocab)
            if not chars:
                continue
            align_indices.append(index)
            tokens_starred.append(" ".join(list(chars)))
            text_starred.append(str(token.get("text", "")))
        if STAR_FREQUENCY == "edges":
            tokens_starred = ["<star>"] + tokens_starred + ["<star>"]
            text_starred = ["<star>"] + text_starred + ["<star>"]
        elif STAR_FREQUENCY == "segment":
            starred_tokens = []
            starred_text = []
            for token, text in zip(tokens_starred, text_starred):
                starred_tokens.extend(["<star>", token])
                starred_text.extend(["<star>", text])
            tokens_starred = starred_tokens
            text_starred = starred_text
        return tokens_starred, text_starred, align_indices

    def align_tokens(self, emissions, stride: float,
                     tokens: list[dict[str, Any]], language: str = "en",
                     offset: float = 0.0) -> AlignmentResult:
        self.load()
        from ctc_forced_aligner import get_alignments, get_spans, postprocess_results
        started = time.perf_counter()
        words = [
            WordTiming(
                index=index,
                line=int(token.get("line", 0)),
                token=int(token.get("token", 0)),
                text=str(token.get("text", "")),
                start=0.0,
                end=0.0,
                score=-999.0,
                aligned=False,
            )
            for index, token in enumerate(tokens)
        ]
        tokens_starred, text_starred, align_indices = self._build_sequence(
            tokens, language)
        if not align_indices:
            return AlignmentResult(
                words=words, total_words=len(tokens), aligned_words=0,
                coverage=0.0, mean_score=-999.0, low_score_words=0,
                seconds=time.perf_counter() - started)

        segments, scores, blank = get_alignments(
            emissions, tokens_starred, self.tokenizer)
        spans = get_spans(tokens_starred, segments, blank)
        results = postprocess_results(
            text_starred, spans, stride, scores, 0.0)
        results = [result for result in results if result.get("text") != "<star>"]
        if len(results) != len(align_indices):
            log.warning("  aligner returned %d results for %d tokens",
                        len(results), len(align_indices))
        score_values = []
        low_score_words = 0
        aligned_words = 0
        for result, token_index in zip(results, align_indices):
            word = words[token_index]
            word.start = round(offset + float(result.get("start", 0.0)), 3)
            word.end = round(offset + float(result.get("end", 0.0)), 3)
            word.score = round(float(result.get("score", -999.0)), 4)
            word.aligned = (
                word.end > word.start + MIN_WORD_SECONDS
                and word.score >= MIN_WORD_SCORE)
            if word.score > -900:
                score_values.append(word.score)
            if word.score < -2.0:
                low_score_words += 1
            if word.aligned:
                aligned_words += 1
        mean_score = (sum(score_values) / len(score_values)
                      if score_values else -999.0)
        return AlignmentResult(
            words=words,
            total_words=len(tokens),
            aligned_words=aligned_words,
            coverage=aligned_words / len(tokens) if tokens else 0.0,
            mean_score=mean_score,
            low_score_words=low_score_words,
            seconds=time.perf_counter() - started,
        )

    def align_window(self, audio_np, tokens: list[dict[str, Any]],
                     language: str = "en", offset: float = 0.0) -> AlignmentResult:
        emissions, stride = self.prepare_emissions(audio_np)
        return self.align_tokens(emissions, stride, tokens, language, offset)

    def align_candidate(self, emissions, stride: float,
                        lines: list[dict[str, Any]],
                        tokens: list[dict[str, Any]],
                        language: str) -> CandidateAlignment:
        result = self.align_tokens(emissions, stride, tokens, language)
        by_line: dict[int, list[WordTiming]] = {index: [] for index in range(len(lines))}
        for word in result.words:
            if 0 <= word.line < len(lines):
                by_line[word.line].append(word)

        line_results = []
        aligned_lines = 0
        lines_with_words = 0
        for index, line in enumerate(lines):
            line_words = by_line.get(index, [])
            total = len(line_words)
            aligned = sum(1 for word in line_words if word.aligned)
            coverage = aligned / total if total else 0.0
            if total:
                lines_with_words += 1
                if coverage >= LINE_COVERAGE_TARGET:
                    aligned_lines += 1
            line_results.append({
                "index": index,
                "start": line.get("start"),
                "end": line.get("end"),
                "coverage": round(coverage, 4),
                "aligned_words": aligned,
                "total_words": total,
                "words": line_words,
            })
        line_coverage = (aligned_lines / lines_with_words
                         if lines_with_words else 0.0)
        summary = {
            "word_coverage": round(result.coverage, 4),
            "line_coverage": round(line_coverage, 4),
            "mean_score": round(result.mean_score, 4),
            "aligned_words": result.aligned_words,
            "total_words": result.total_words,
            "low_score_words": result.low_score_words,
            "lines": len(lines),
            "seconds": round(result.seconds, 3),
        }
        return CandidateAlignment(summary=summary, lines=line_results)

    def align_audio_candidate(self, audio_np, lines: list[dict[str, Any]],
                              tokens: list[dict[str, Any]],
                              language: str) -> CandidateAlignment:
        emissions, stride = self.prepare_emissions(audio_np)
        return self.align_candidate(emissions, stride, lines, tokens, language)


def candidate_rank_key(candidate: dict[str, Any]) -> tuple:
    alignment = candidate.get("alignment") or {}
    record = candidate.get("record") or {}
    return (
        float(alignment.get("line_coverage") or 0.0),
        float(alignment.get("word_coverage") or 0.0),
        float(alignment.get("mean_score") or -999.0),
        -float(candidate.get("duration_error") or 0.0),
        1 if record.get("hasWordSync") else 0,
    )


def alignment_word_dicts(line_alignment: dict[str, Any]) -> list[dict[str, Any]]:
    words = []
    for word in line_alignment.get("words") or []:
        if word.aligned:
            words.append({
                "text": word.text,
                "start": word.start,
                "end": word.end,
                "score": word.score,
            })
    return words
