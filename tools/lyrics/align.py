"""Forced alignment adapter over the MMS CTC model.

The optional aligner dependencies are imported lazily so the web app tests and
CI can import this module without torch/transformers/ctc-forced-aligner.
"""
from __future__ import annotations

import logging
import math
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from . import textnorm
from .audio import ONSET_RMS, max_window_rms, window_rms

log = logging.getLogger("lyrics")

DEFAULT_MODEL = "MahmoudAshraf/mms-300m-1130-forced-aligner"
MIN_WORD_SCORE = -2.0
MIN_WORD_SECONDS = 0.02
LINE_COVERAGE_TARGET = 0.80
STAR_FREQUENCY = "line"
ALIGNMENT_TIME_SHIFT = -0.20
WINDOW_GAP_SECONDS = 2.5
WINDOW_MAX_SECONDS = 30.0
WINDOW_PAD_SECONDS = 1.5
WINDOW_MIN_SECONDS = 1.0
LINE_WINDOW_START_PAD = 0.4
LINE_WINDOW_END_PAD = 0.7


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


def _line_end(line: dict[str, Any], next_start: float | None,
              duration: float) -> float:
    value = line.get("end")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    if next_start is not None:
        return max(float(line.get("start") or 0.0), next_start - 0.05)
    return min(duration, float(line.get("start") or 0.0) + 4.0)


def _vocal_gaps(audio, duration: float) -> list[tuple[float, float]]:
    if audio is None or duration <= 0:
        return []
    times, values = window_rms(audio, 0.0, duration, window=0.5, hop=0.5)
    gaps = []
    start = None
    for moment, value in zip(times, values):
        if value < ONSET_RMS:
            if start is None:
                start = float(moment)
        elif start is not None:
            if float(moment) - start >= WINDOW_GAP_SECONDS:
                gaps.append((start, float(moment)))
            start = None
    if start is not None and duration - start >= WINDOW_GAP_SECONDS:
        gaps.append((start, duration))
    return gaps


def build_line_windows(lines: list[dict[str, Any]], duration: float,
                       audio=None) -> list[tuple[float, float, list[int]]]:
    """Group timed lines into soft-alignment windows.

    Consecutive lines stay together through continuous singing. A long
    vocal-activity gap or a reference gap starts a new window, and long spans
    are capped so a whole song is never aligned in one pass. Each group is
    padded slightly so a nearby vocal onset is not cut off.
    """
    timed = [(index, float(line["start"])) for index, line in enumerate(lines)
             if line.get("start") is not None]
    if not timed:
        return [(0.0, max(0.0, duration), list(range(len(lines))))]

    starts = [start for _, start in timed]
    gaps = _vocal_gaps(audio, duration)
    groups: list[tuple[list[int], float, float]] = []
    current: list[int] = []
    group_first_start = 0.0
    group_start_hint = 0.0
    previous_start = 0.0
    previous_end = 0.0
    for position, (line_index, start) in enumerate(timed):
        next_start = starts[position + 1] if position + 1 < len(starts) else None
        end = _line_end(lines[line_index], next_start, duration)
        split = None
        if current:
            for gap_start, gap_end in gaps:
                if (gap_start > previous_start + 0.5
                        and gap_end < start - 0.05):
                    split = (gap_start, gap_end)
                    break
            if split is None and start - group_first_start > WINDOW_MAX_SECONDS:
                split = None
                force_split = True
            else:
                force_split = False
            if split is not None or force_split:
                end_hint = previous_end + WINDOW_PAD_SECONDS
                if split is not None:
                    end_hint = min(end_hint, split[0] + 0.5)
                groups.append((current, group_start_hint,
                               min(duration, end_hint)))
                current = []
                group_first_start = start
                group_start_hint = (
                    max(0.0, split[1] - WINDOW_PAD_SECONDS)
                    if split is not None else max(0.0, start - WINDOW_PAD_SECONDS))
                previous_end = end
        if not current:
            group_first_start = start
            if group_start_hint <= 0.0:
                group_start_hint = max(0.0, start - WINDOW_PAD_SECONDS)
            previous_end = end
        else:
            previous_end = max(previous_end, end)
        current.append(line_index)
        previous_start = start
    if current:
        groups.append((current, group_start_hint,
                       min(duration, previous_end + WINDOW_PAD_SECONDS)))

    windows = []
    for group, start_hint, end_hint in groups:
        end = max(end_hint, float(lines[group[-1]].get("start") or 0.0) + 0.2)
        if end - start_hint < WINDOW_MIN_SECONDS:
            end = min(duration, start_hint + WINDOW_MIN_SECONDS)
        windows.append((start_hint, end, group))
    return windows


def build_per_line_windows(lines: list[dict[str, Any]], duration: float,
                           audio=None, *, start_pad: float = WINDOW_PAD_SECONDS,
                           end_pad: float = WINDOW_PAD_SECONDS
                           ) -> list[tuple[float, float, list[int]]]:
    """One soft window per timed line, for ASR segment-based alignment."""
    windows = []
    for index, line in enumerate(lines):
        start = line.get("start")
        if start is None:
            continue
        start = float(start)
        end = line.get("end")
        if end is None:
            end = start + 4.0
        end = float(end)
        window_start = max(0.0, start - float(start_pad))
        window_end = min(duration, max(end, start + WINDOW_MIN_SECONDS)
                         + float(end_pad))
        if window_end <= window_start:
            window_end = min(duration, window_start + WINDOW_MIN_SECONDS)
        windows.append((window_start, window_end, [index]))
    if not windows:
        return [(0.0, max(0.0, duration), list(range(len(lines))))]
    return windows


def _emissions_slice(emissions, stride: float,
                     start: float, end: float):
    frames_per_second = 1000.0 / stride
    first = max(0, int(math.floor(start * frames_per_second)))
    last = min(int(emissions.size(0)),
               int(math.ceil(end * frames_per_second)))
    if last <= first:
        return emissions[:0], 0.0
    return emissions[first:last], first * stride / 1000.0


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1,
                       int(round(fraction * (len(ordered) - 1))))]


def _line_reference_bounds(lines: list[dict[str, Any]], index: int,
                           duration: float) -> tuple[float, float]:
    if not 0 <= index < len(lines):
        return 0.0, duration
    line = lines[index]
    start = float(line.get("start") or 0.0)
    end = line.get("end")
    if end is None:
        end = None
        for later in lines[index + 1:]:
            if later.get("start") is not None:
                end = float(later["start"])
                break
        end = float(end) if end is not None else start + 4.0
    return start, float(end)


def _merge_alignment_words(primary: AlignmentResult,
                           secondary: AlignmentResult,
                           lines: list[dict[str, Any]], duration: float,
                           threshold: float) -> AlignmentResult:
    """Merge a tight per-line pass with a whole-song pass.

    The tight pass is constrained to the LRCLIB line window and is reliable
    near the reference timestamps. The whole-song pass recovers words the
    window cut off, but can attach a repeated line to another occurrence, so
    only accept secondary timings close to the line's reference bounds.
    """
    merged: list[WordTiming] = []
    previous_end: dict[int, float] = {}
    for tight, wide in zip(primary.words, secondary.words):
        line_start, line_end = _line_reference_bounds(
            lines, tight.line, duration)
        margin = 1.5
        in_bounds = (line_start - margin <= wide.start
                     and wide.end <= line_end + margin)
        chosen = tight
        if (wide.aligned and in_bounds
                and wide.score >= threshold
                and (not tight.aligned
                     or wide.score > tight.score + 0.05)):
            chosen = wide
        last_end = previous_end.get(tight.line)
        if chosen.aligned and last_end is not None \
                and chosen.start < last_end - 0.05:
            chosen = tight if tight.aligned and tight.start >= last_end - 0.05 \
                else wide if wide.aligned and wide.start >= last_end - 0.05 \
                else tight
        merged.append(chosen)
        if chosen.aligned:
            previous_end[tight.line] = chosen.end

    aligned = sum(1 for word in merged if word.aligned)
    score_values = [word.score for word in merged if word.score > -900]
    return AlignmentResult(
        words=merged,
        total_words=primary.total_words,
        aligned_words=aligned,
        coverage=aligned / primary.total_words if primary.total_words else 0.0,
        mean_score=(sum(score_values) / len(score_values)
                    if score_values else -999.0),
        low_score_words=sum(1 for word in merged if word.score < threshold),
        seconds=primary.seconds + secondary.seconds,
    )


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

    def _build_sequence(self, tokens: list[dict[str, Any]], language: str,
                        star_frequency: str | None = None):
        star_frequency = star_frequency or STAR_FREQUENCY
        tokens_starred: list[str] = []
        text_starred: list[str] = []
        align_indices: list[int] = []
        line_of_token: list[int] = []
        for index, token in enumerate(tokens):
            norm = str(token.get("norm") or textnorm.norm_token(
                str(token.get("text", "")), language))
            chars = _alignment_text(norm, self.char_vocab)
            if not chars:
                continue
            align_indices.append(index)
            line_of_token.append(int(token.get("line", 0)))
            tokens_starred.append(" ".join(list(chars)))
            text_starred.append(str(token.get("text", "")))
        if star_frequency == "edges":
            tokens_starred = ["<star>"] + tokens_starred + ["<star>"]
            text_starred = ["<star>"] + text_starred + ["<star>"]
        elif star_frequency == "token":
            starred_tokens = []
            starred_text = []
            for token, text in zip(tokens_starred, text_starred):
                starred_tokens.extend(["<star>", token])
                starred_text.extend(["<star>", text])
            tokens_starred = starred_tokens
            text_starred = starred_text
        elif star_frequency == "line":
            starred_tokens = ["<star>"]
            starred_text = ["<star>"]
            previous_line = None
            for token, text, line in zip(
                    tokens_starred, text_starred, line_of_token):
                if previous_line is not None and line != previous_line:
                    starred_tokens.append("<star>")
                    starred_text.append("<star>")
                starred_tokens.append(token)
                starred_text.append(text)
                previous_line = line
            starred_tokens.append("<star>")
            starred_text.append("<star>")
            tokens_starred = starred_tokens
            text_starred = starred_text
        return tokens_starred, text_starred, align_indices

    def align_tokens(self, emissions, stride: float,
                     tokens: list[dict[str, Any]], language: str = "en",
                     offset: float = 0.0, *,
                     windows: list[tuple[float, float, list[int]]] | None = None,
                     star_frequency: str | None = None,
                     min_word_score: float | None = None,
                     time_shift: float | None = None) -> AlignmentResult:
        self.load()
        from ctc_forced_aligner import get_alignments, get_spans, postprocess_results
        started = time.perf_counter()
        threshold = (MIN_WORD_SCORE if min_word_score is None
                     else float(min_word_score))
        shift = (ALIGNMENT_TIME_SHIFT if time_shift is None
                 else float(time_shift))
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
        score_values: list[float] = []
        aligned_words = 0
        low_score_words = 0

        def align_slice(window_emissions, window_offset: float,
                        token_indices: list[int]) -> None:
            nonlocal aligned_words, low_score_words
            subset = [tokens[index] for index in token_indices]
            tokens_starred, text_starred, align_indices = self._build_sequence(
                subset, language, star_frequency)
            if not align_indices:
                return
            segments, scores, blank = get_alignments(
                window_emissions, tokens_starred, self.tokenizer)
            spans = get_spans(tokens_starred, segments, blank)
            results = postprocess_results(
                text_starred, spans, stride, scores, window_offset)
            results = [result for result in results
                       if result.get("text") != "<star>"]
            if len(results) != len(align_indices):
                log.warning("  aligner returned %d results for %d tokens",
                            len(results), len(align_indices))
            for result, local_index in zip(results, align_indices):
                word = words[token_indices[local_index]]
                word.start = round(max(
                    0.0, window_offset + float(result.get("start", 0.0))
                    + shift), 3)
                word.end = round(max(
                    word.start + MIN_WORD_SECONDS,
                    window_offset + float(result.get("end", 0.0))
                    + shift), 3)
                word.score = round(float(result.get("score", -999.0)), 4)
                word.aligned = (
                    word.end > word.start + MIN_WORD_SECONDS
                    and word.score >= threshold)
                if word.score > -900:
                    score_values.append(word.score)
                if word.score < threshold:
                    low_score_words += 1
                if word.aligned:
                    aligned_words += 1

        if windows:
            covered: set[int] = set()
            for window_start, window_end, line_indices in windows:
                covered.update(line_indices)
                line_set = set(line_indices)
                token_indices = [
                    index for index, token in enumerate(tokens)
                    if int(token.get("line", 0)) in line_set
                ]
                if not token_indices:
                    continue
                window_emissions, window_offset = _emissions_slice(
                    emissions, stride, window_start, window_end)
                if window_emissions.size(0) == 0:
                    continue
                align_slice(window_emissions, window_offset, token_indices)
            missing = [index for index in range(len(tokens))
                       if int(tokens[index].get("line", 0)) not in covered]
            if missing:
                token_indices = [
                    index for index, token in enumerate(tokens)
                    if int(token.get("line", 0)) not in covered
                ]
                align_slice(emissions, offset, token_indices)
        else:
            align_slice(emissions, offset, list(range(len(tokens))))

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

    def _snap_repeated_lines(self, lines: list[dict[str, Any]],
                             result: AlignmentResult, audio_data,
                             offset: float = 0.0) -> list[int]:
        """Move a repeated line back to its own offset-corrected occurrence.

        The CTC path can attach a repeated chorus/ad-lib to a later occurrence.
        When the line text repeats, the offset-corrected reference timestamp is
        the only way to tell the occurrences apart. Refuse a snap that would
        overlap the neighbouring aligned line.
        """
        if audio_data is None:
            return []
        words_by_line: dict[int, list[WordTiming]] = {}
        for word in result.words:
            words_by_line.setdefault(word.line, []).append(word)
        bounds: dict[int, tuple[float, float]] = {}
        for index, line in enumerate(lines):
            words = [word for word in words_by_line.get(index, [])
                     if word.aligned]
            if words:
                bounds[index] = (min(word.start for word in words),
                                 max(word.end for word in words))
            elif line.get("start") is not None:
                start = float(line["start"]) + offset
                bounds[index] = (start, float(line.get("end") or start))
        counts: dict[str, int] = {}
        for line in lines:
            key = " ".join(
                textnorm.norm_token(token, "tl")
                for token in str(line.get("text") or "").split())
            counts[key] = counts.get(key, 0) + 1
        snapped = []
        for index, line in enumerate(lines):
            key = " ".join(
                textnorm.norm_token(token, "tl")
                for token in str(line.get("text") or "").split())
            if counts.get(key, 0) < 2 or line.get("start") is None:
                continue
            words = [word for word in words_by_line.get(index, [])
                     if word.aligned]
            if not words:
                continue
            reference_start = float(line["start"]) + offset
            delta = reference_start - words[0].start
            if abs(delta) < 2.0:
                continue
            if max_window_rms(
                    audio_data, reference_start,
                    reference_start + 1.5) < ONSET_RMS:
                continue
            new_start = words[0].start + delta
            new_end = words[-1].end + delta
            if new_start < 0.0:
                continue
            previous = [value for key_index, value in bounds.items()
                        if key_index < index]
            following = [value for key_index, value in bounds.items()
                         if key_index > index]
            if previous and new_start < previous[-1][1] - 0.05:
                continue
            if following and new_end > following[0][0] + 0.05:
                continue
            for word in words:
                word.start = round(word.start + delta, 3)
                word.end = round(word.end + delta, 3)
            snapped.append(index)
        return snapped

    def _retry_low_coverage_lines(self, emissions, stride: float,
                                  lines: list[dict[str, Any]],
                                  tokens: list[dict[str, Any]],
                                  result: AlignmentResult, language: str,
                                  duration: float, threshold: float,
                                  shift: float) -> AlignmentResult:
        """Re-align only the lines that are still below the coverage target.

        A wider per-line window gives the CTC model more context than the
        tight LRCLIB window, without the whole-song pass's repeated-chorus
        ambiguity.
        """
        by_line: dict[int, list[WordTiming]] = {}
        for word in result.words:
            by_line.setdefault(word.line, []).append(word)
        retry_seconds = 0.0
        for index, line in enumerate(lines):
            if line.get("start") is None:
                continue
            line_words = by_line.get(index, [])
            total = len(line_words)
            aligned = sum(1 for word in line_words if word.aligned)
            coverage = aligned / total if total else 0.0
            if total == 0 or coverage >= LINE_COVERAGE_TARGET:
                continue
            start, end = _line_reference_bounds(lines, index, duration)
            window = (max(0.0, start - 2.0),
                      min(duration, max(end + 1.5, start + 1.0)), [index])
            subset = [token for token in tokens
                      if int(token.get("line", 0)) == index]
            if not subset:
                continue
            retry = self.align_tokens(
                emissions, stride, subset, language, windows=[window],
                star_frequency="edges", min_word_score=threshold,
                time_shift=shift)
            retry_seconds += retry.seconds
            local = {word.token: word for word in retry.words if word.aligned}
            for position, current in enumerate(result.words):
                if current.line != index:
                    continue
                candidate = local.get(current.token)
                if (candidate is not None
                        and start - 1.5 <= candidate.start
                        and candidate.end <= end + 1.5
                        and (not current.aligned
                             or candidate.score > current.score + 0.05)):
                    result.words[position] = candidate

        aligned_words = sum(1 for word in result.words if word.aligned)
        scores = [word.score for word in result.words if word.score > -900]
        result.aligned_words = aligned_words
        result.coverage = (aligned_words / result.total_words
                           if result.total_words else 0.0)
        result.mean_score = (sum(scores) / len(scores)
                             if scores else -999.0)
        result.low_score_words = sum(
            1 for word in result.words if word.score < threshold)
        result.seconds += retry_seconds
        return result

    def align_candidate(self, emissions, stride: float,
                        lines: list[dict[str, Any]],
                        tokens: list[dict[str, Any]],
                        language: str, duration: float = 0.0, *,
                        windowed: bool = False,
                        window_mode: str = "group",
                        star_frequency: str | None = None,
                        min_word_score: float | None = None,
                        audio_data=None,
                        snap_repeated: bool = False,
                        time_shift: float | None = None,
                        merge_passes: bool = False,
                        retry_low_coverage: bool = False,
                        start_pad: float | None = None,
                        end_pad: float | None = None
                        ) -> CandidateAlignment:
        threshold = (MIN_WORD_SCORE if min_word_score is None
                     else float(min_word_score))
        shift = (ALIGNMENT_TIME_SHIFT if time_shift is None
                 else float(time_shift))
        if duration <= 0:
            duration = max(
                (float(line.get("end") or line.get("start") or 0.0)
                 for line in lines), default=0.0) + 5.0
        if windowed:
            if window_mode == "line":
                windows = build_per_line_windows(
                    lines, duration, audio_data,
                    start_pad=(LINE_WINDOW_START_PAD if start_pad is None
                               else float(start_pad)),
                    end_pad=(LINE_WINDOW_END_PAD if end_pad is None
                             else float(end_pad)))
            else:
                windows = build_line_windows(lines, duration, audio_data)
        else:
            windows = None
        result = self.align_tokens(
            emissions, stride, tokens, language, windows=windows,
            star_frequency=star_frequency, min_word_score=threshold,
            time_shift=shift)
        if merge_passes:
            wide = self.align_tokens(
                emissions, stride, tokens, language, windows=None,
                star_frequency="edges", min_word_score=threshold,
                time_shift=shift)
            result = _merge_alignment_words(
                result, wide, lines, duration, threshold)
        if retry_low_coverage:
            result = self._retry_low_coverage_lines(
                emissions, stride, lines, tokens, result, language,
                duration, threshold, shift)
        by_line_for_offset: dict[int, list[WordTiming]] = {}
        for word in result.words:
            by_line_for_offset.setdefault(word.line, []).append(word)
        offsets = []
        for index, line in enumerate(lines):
            words = [word for word in by_line_for_offset.get(index, [])
                     if word.aligned]
            if words and line.get("start") is not None:
                offsets.append(words[0].start - float(line["start"]))
        offsets.sort()
        reference_offset = offsets[len(offsets) // 2] if offsets else 0.0
        snapped_lines = (
            self._snap_repeated_lines(
                lines, result, audio_data, offset=reference_offset)
            if snap_repeated else [])
        by_line: dict[int, list[WordTiming]] = {index: [] for index in range(len(lines))}
        for word in result.words:
            if 0 <= word.line < len(lines):
                by_line[word.line].append(word)

        line_results = []
        aligned_lines = 0
        lines_with_words = 0
        low_confidence_lines = []
        for index, line in enumerate(lines):
            line_words = by_line.get(index, [])
            total = len(line_words)
            aligned = sum(1 for word in line_words if word.aligned)
            coverage = aligned / total if total else 0.0
            line_scores = [word.score for word in line_words
                           if word.score > -900]
            mean_line_score = (sum(line_scores) / len(line_scores)
                               if line_scores else -999.0)
            if total:
                lines_with_words += 1
                if coverage >= LINE_COVERAGE_TARGET:
                    aligned_lines += 1
                if (coverage < LINE_COVERAGE_TARGET
                        or any(score < threshold for score in line_scores)
                        or mean_line_score < threshold):
                    low_confidence_lines.append(index)
            line_results.append({
                "index": index,
                "start": line.get("start"),
                "end": line.get("end"),
                "coverage": round(coverage, 4),
                "aligned_words": aligned,
                "total_words": total,
                "mean_score": round(mean_line_score, 4),
                "min_score": round(min(line_scores), 4) if line_scores else None,
                "words": line_words,
            })
        line_coverage = (aligned_lines / lines_with_words
                         if lines_with_words else 0.0)
        score_values = [word.score for word in result.words if word.score > -900]
        summary = {
            "word_coverage": round(result.coverage, 4),
            "line_coverage": round(line_coverage, 4),
            "mean_score": round(result.mean_score, 4),
            "aligned_words": result.aligned_words,
            "total_words": result.total_words,
            "low_score_words": result.low_score_words,
            "lines": len(lines),
            "windowed": bool(windowed),
            "windows": len(windows) if windows else 1,
            "star_frequency": star_frequency or STAR_FREQUENCY,
            "score_threshold": round(threshold, 4),
            "score_min": round(min(score_values), 4) if score_values else None,
            "score_p10": (round(_quantile(score_values, 0.10), 4)
                          if score_values else None),
            "score_median": (round(_quantile(score_values, 0.50), 4)
                             if score_values else None),
            "time_shift": shift,
            "reference_offset": round(reference_offset, 4),
            "low_confidence_lines": low_confidence_lines,
            "snapped_repeated_lines": snapped_lines,
            "seconds": round(result.seconds, 3),
        }
        return CandidateAlignment(summary=summary, lines=line_results)

    def align_audio_candidate(self, audio_np, lines: list[dict[str, Any]],
                              tokens: list[dict[str, Any]],
                              language: str, duration: float = 0.0, *,
                              windowed: bool = False,
                              window_mode: str = "group",
                              star_frequency: str | None = None,
                              min_word_score: float | None = None,
                              snap_repeated: bool = False,
                              time_shift: float | None = None,
                              merge_passes: bool = False,
                              retry_low_coverage: bool = False,
                              start_pad: float | None = None,
                              end_pad: float | None = None
                              ) -> CandidateAlignment:
        emissions, stride = self.prepare_emissions(audio_np)
        return self.align_candidate(
            emissions, stride, lines, tokens, language, duration,
            windowed=windowed, window_mode=window_mode,
            star_frequency=star_frequency,
            min_word_score=min_word_score, audio_data=audio_np,
            snap_repeated=snap_repeated, time_shift=time_shift,
            merge_passes=merge_passes,
            retry_low_coverage=retry_low_coverage,
            start_pad=start_pad, end_pad=end_pad)


def candidate_rank_key(candidate: dict[str, Any]) -> tuple:
    alignment = candidate.get("alignment") or {}
    record = candidate.get("record") or {}
    build = candidate.get("build_quality") or {}
    duration_error = float(candidate.get("duration_error") or 0.0)
    if build:
        return (
            float(build.get("coverage") or 0.0)
            - float(build.get("line_level_rate") or 0.0),
            -duration_error,
            float(alignment.get("mean_score") or -999.0),
            1 if record.get("hasWordSync") else 0,
        )
    return (
        float(alignment.get("line_coverage") or 0.0),
        float(alignment.get("word_coverage") or 0.0),
        -duration_error,
        float(alignment.get("mean_score") or -999.0),
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
