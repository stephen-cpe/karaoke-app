"""Line building, ASR/reference token matching, and instrumental cues."""
from __future__ import annotations

import difflib
import logging
import re

from . import textnorm
from .audio import (
    PHRASE_LINGER,
    STRONG_ONSET_RMS,
    vocal_onset,
    vocal_phrase_end,
    window_rms,
)

log = logging.getLogger("lyrics")

INSTRUMENTAL_GAP = 3.0
MIN_LINE_LEVEL_TOKENS = 1
MIN_WORD_LENGTH = 0.12


def srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def tokens_similar(left: str, right: str) -> bool:
    """True when two short words are one typo apart (e.g., love/life)."""
    if left == right:
        return True
    if len(left) < 4 or len(right) < 4 or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1:] == shorter:
            return True
    return False


def map_lrc_time(anchors: dict[int, float], lines: list[dict], value: float,
                 default_offset: float = 0.0) -> float:
    """Map an LRC timestamp to local audio time using matched line anchors."""
    pairs = sorted((lines[index]["start"], local_start)
                   for index, local_start in anchors.items())
    if not pairs:
        return value + default_offset
    if value == pairs[0][0]:
        return pairs[0][1]
    if value == pairs[-1][0]:
        return pairs[-1][1]
    if value < pairs[0][0] or value > pairs[-1][0]:
        return value + default_offset
    for (lrc0, local0), (lrc1, local1) in zip(pairs, pairs[1:]):
        if lrc0 <= value <= lrc1:
            if lrc1 == lrc0:
                return local0
            return local0 + (local1 - local0) * (value - lrc0) / (lrc1 - lrc0)
    return value + default_offset


def distribute_words(tokens: list[dict], start: float, end: float) -> list[dict]:
    """Give every token a slice of [start, end], weighted by token length.

    This is character-proportional timing and is never measured. Every word is
    marked ``inferred: True``; callers should prefer line-level output when a
    whole line has no usable ASR timing.
    """
    if not tokens:
        return []
    if end <= start:
        end = start + 0.5
    weights = [max(1, len(token.get("norm") or token.get("text") or ""))
               for token in tokens]
    total = sum(weights)
    cursor = start
    words = []
    for index, (token, weight) in enumerate(zip(tokens, weights)):
        share = (end - start) * weight / total
        word_start = cursor
        word_end = end if index == len(tokens) - 1 else cursor + share
        if word_end <= word_start:
            word_end = word_start + MIN_WORD_LENGTH
        words.append({"text": token["text"],
                      "start": round(word_start, 3),
                      "end": round(word_end, 3),
                      "inferred": True})
        cursor = word_end
    return words


def interpolate_known(tokens: list[dict], known: dict[int, tuple[float, float]],
                      start: float, end: float) -> list[dict]:
    """Use measured word timings where available; mark filled gaps inferred."""
    words: list[dict | None] = [None] * len(tokens)
    for index, times in known.items():
        if not 0 <= index < len(tokens):
            continue
        word_start, word_end = times
        if word_start >= end - 0.05 or word_end <= start + 0.05:
            continue
        words[index] = {
            "text": tokens[index]["text"],
            "start": max(start, word_start),
            "end": min(end, max(word_start + MIN_WORD_LENGTH, word_end)),
        }

    index = 0
    while index < len(tokens):
        if words[index] is not None:
            index += 1
            continue
        run_start = index
        while index < len(tokens) and words[index] is None:
            index += 1
        run_end = index
        gap_start = words[run_start - 1]["end"] if run_start > 0 else start
        gap_end = words[run_end]["start"] if run_end < len(tokens) else end
        run = tokens[run_start:run_end]
        if gap_end <= gap_start:
            gap_end = gap_start + MIN_WORD_LENGTH * len(run)
        filled = distribute_words(run, gap_start, gap_end)
        words[run_start:run_end] = filled

    previous_end = start
    result = []
    for word in words:
        word_start = max(start, previous_end, word["start"])
        word_end = min(end, max(word_start + MIN_WORD_LENGTH, word["end"]))
        item = {
            "text": word["text"],
            "start": round(word_start, 3),
            "end": round(word_end, 3),
        }
        if word.get("inferred"):
            item["inferred"] = True
        result.append(item)
        previous_end = word_end
    return result


def local_timing_is_reasonable(tokens: list[dict], known: dict[int, tuple[float, float]],
                               line_start: float, line_end: float) -> bool:
    if not known:
        return False
    coverage = len(known) / len(tokens) if tokens else 0.0
    if coverage < 0.55:
        offsets = sorted(known)
        consecutive = offsets == list(range(offsets[0], offsets[-1] + 1))
        if coverage < 0.25 or len(offsets) < 2 or not consecutive:
            return False
    ordered = sorted(known.items())
    first_start = ordered[0][1][0]
    last_end = ordered[-1][1][1]
    if first_start < line_start - 1.5 or first_start > line_start + 2.0:
        return False
    if last_end < line_start + 0.25 * (line_end - line_start):
        return False
    previous_end = None
    for _, (word_start, word_end) in ordered:
        if word_start < line_start - 1.5 or word_end > line_end + 0.75:
            return False
        if word_end - word_start > 5.0:
            return False
        if previous_end is not None and word_start - previous_end > 6.0:
            return False
        previous_end = word_end
    return True


def robust_line_anchors(lines: list[dict], tokens: list[dict],
                        token_local: dict[int, int],
                        local: list[dict]) -> tuple[dict[int, float], dict]:
    starts_by_line: dict[int, list[float]] = {}
    first_token: dict[int, int] = {}
    for token_index, token in enumerate(tokens):
        first_token.setdefault(token["line"], token_index)
    for token_index, local_index in token_local.items():
        line_index = tokens[token_index]["line"]
        if token_index != first_token[line_index]:
            continue
        starts_by_line.setdefault(line_index, []).append(local[local_index]["start"])
    if not starts_by_line:
        return {}, {}

    offsets = sorted(min(starts) - lines[line_index]["start"]
                     for line_index, starts in starts_by_line.items())
    median = offsets[len(offsets) // 2]
    mad = sorted(abs(offset - median) for offset in offsets)[len(offsets) // 2]
    tolerance = max(1.25, min(3.0, 5.0 * mad))
    anchors = {}
    for line_index, starts in starts_by_line.items():
        start = min(starts)
        if abs(start - lines[line_index]["start"] - median) <= tolerance:
            anchors[line_index] = start

    monotonic = {}
    last_start = None
    for line_index in sorted(anchors):
        if last_start is not None and anchors[line_index] <= last_start:
            continue
        monotonic[line_index] = anchors[line_index]
        last_start = anchors[line_index]
    return monotonic, {"median_offset": median, "mad": mad,
                       "tolerance": tolerance}


def mapped_line_windows(lines: list[dict], anchors: dict[int, float],
                        duration: float, offset: float = 0.0) -> list[tuple[float, float]]:
    starts = [max(0.0, map_lrc_time(anchors, lines, line["start"], offset))
              for line in lines]
    windows = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else duration
        if end <= start:
            end = start + 0.5
        windows.append((start, end))
    return windows


def align_reference_words(reference: dict, local: list[dict], duration: float,
                          language: str = "en", iterations: int = 2) -> None:
    """Match reference tokens to local ASR words in per-line time windows."""
    lines = reference["lines"]
    tokens = reference["tokens"]
    anchors = reference.get("anchors", {})
    tokens_by_line: dict[int, list[int]] = {}
    for token_index, token in enumerate(tokens):
        tokens_by_line.setdefault(token["line"], []).append(token_index)

    token_local: dict[int, int] = {}
    for _ in range(max(1, iterations)):
        offset = reference.get("offset", 0.0)
        windows = mapped_line_windows(lines, anchors, duration, offset)
        new_token_local: dict[int, int] = {}
        new_anchors: dict[int, float] = dict(anchors)
        claimed: set[int] = set()
        for line_index, token_indices in tokens_by_line.items():
            start, end = windows[line_index]
            candidates = [
                (local_index, word)
                for local_index, word in enumerate(local)
                if local_index not in claimed
                and start - 1.5 <= word["start"] <= end + 1.5
            ]
            if not candidates:
                continue
            matcher = difflib.SequenceMatcher(
                None,
                [tokens[index]["norm"] for index in token_indices],
                [word["norm"] for _, word in candidates],
                autojunk=False,
            )
            matched: dict[int, int] = {}
            for block in matcher.get_matching_blocks():
                for token_offset in range(block.size):
                    matched[block.a + token_offset] = candidates[block.b + token_offset][0]
            if not matched:
                continue
            last_offset = max(matched)
            if last_offset < len(token_indices) - 1:
                used = set(matched.values())
                last_end = local[matched[last_offset]]["end"]
                spare = [
                    (local_index, word)
                    for local_index, word in candidates
                    if local_index not in used
                    and word["start"] >= last_end - 0.05
                    and word["start"] <= last_end + 1.2
                ]
                for token_offset, (local_index, word) in zip(
                        range(last_offset + 1, len(token_indices)), spare):
                    if tokens_similar(tokens[token_indices[token_offset]]["norm"],
                                      word["norm"]):
                        matched[token_offset] = local_index
                        last_end = word["end"]
                    else:
                        break
            ratio = len(matched) / len(token_indices)
            longest = max((block.size for block in matcher.get_matching_blocks()),
                          default=0)
            if ratio < 0.34 and len(token_indices) > 3 and longest < 2:
                continue
            for token_offset, local_index in matched.items():
                new_token_local[token_indices[token_offset]] = local_index
                claimed.add(local_index)

            first_offset = min(matched)
            first_start = local[matched[first_offset]]["start"]
            if first_offset <= 1 and ratio >= 0.5:
                new_anchors[line_index] = max(0.0, first_start - 0.35 * first_offset)
            elif ratio >= 0.75:
                new_anchors[line_index] = max(
                    0.0, first_start - 0.35 * min(first_offset, 2))

        if not new_anchors:
            break
        monotonic = {}
        last_start = None
        for line_index in sorted(new_anchors):
            if last_start is not None and new_anchors[line_index] <= last_start:
                continue
            monotonic[line_index] = new_anchors[line_index]
            last_start = new_anchors[line_index]
        anchors = monotonic
        token_local = new_token_local

    reference["anchors"] = anchors
    reference["token_local"] = token_local
    offsets = sorted(anchors[index] - lines[index]["start"] for index in anchors)
    if offsets:
        reference["offset"] = offsets[len(offsets) // 2]


def _line_max_duration(token_count: int) -> float:
    if token_count <= 2:
        return 8.0
    if token_count <= 4:
        return 10.0
    return 14.0


def build_lyric_lines(reference: dict, duration: float, audio=None,
                      language: str = "en") -> list[dict]:
    """Build one cue per reference line, falling back to line-level timing."""
    lines = reference["lines"]
    tokens = reference["tokens"]
    token_local = reference.get("token_local", {})
    anchors = reference.get("anchors", {})

    tokens_by_line: dict[int, list[int]] = {index: [] for index in range(len(lines))}
    for token_index, token in enumerate(tokens):
        tokens_by_line[token["line"]].append(token_index)

    line_info = []
    previous_start = -1.0
    previous_word_end = 0.0
    for line_index, line in enumerate(lines):
        line_tokens = tokens_by_line[line_index]
        mapped_start = map_lrc_time(anchors, lines, line["start"],
                                    reference.get("offset", 0.0))
        next_lrc = lines[line_index + 1]["start"] if line_index + 1 < len(lines) else duration
        mapped_next = map_lrc_time(anchors, lines, next_lrc,
                                   reference.get("offset", 0.0))
        if mapped_next <= mapped_start:
            mapped_next = mapped_start + 1.0

        known = {}
        for token_index in line_tokens:
            if token_index in token_local:
                local = reference["local"][token_local[token_index]]
                known[token_index - line_tokens[0]] = (local["start"], local["end"])

        if known and 0 in known and abs(known[0][0] - mapped_start) <= 2.0:
            line_start = known[0][0]
        else:
            line_start = mapped_start

        if audio is not None:
            onset = vocal_onset(audio, line_start + 0.15,
                                min(mapped_next, line_start + 4.0))
            if onset is not None:
                if not known and onset > line_start + 0.5:
                    line_start = onset
                elif known and 0 in known:
                    first_known = known[0][0]
                    if first_known > line_start + 1.2 and onset < first_known - 0.05:
                        line_start = onset

        if audio is not None and previous_start >= 0 and line_start - previous_start > 6.0:
            back_start = max(previous_start + 0.2, line_start - 2.0)
            back_onset = vocal_onset(audio, back_start, line_start + 0.1,
                                     threshold=STRONG_ONSET_RMS)
            if back_onset is not None and back_start + 0.2 <= back_onset < line_start - 0.2:
                line_start = back_onset

        if previous_word_end and line_start < previous_word_end + 0.05 \
                and previous_word_end - line_start < 2.0:
            line_start = previous_word_end + 0.05

        if line_start <= previous_start:
            line_start = previous_start + 0.12
        line_start = max(0.0, min(line_start, max(0.0, duration - 0.3)))
        previous_start = line_start
        line_info.append((line_tokens, mapped_start, mapped_next, known, line_start))

        valid = {offset: times for offset, times in known.items()
                 if 0.0 <= times[1] - times[0] <= 5.0}
        if valid:
            last_offset = max(valid)
            trailing = max(0, len(line_tokens) - 1 - last_offset)
            previous_word_end = valid[last_offset][1] + MIN_WORD_LENGTH * trailing
        else:
            previous_word_end = 0.0

    built = []
    for line_index, (line_tokens, _mapped_start, _mapped_next,
                     known, line_start) in enumerate(line_info):
        if not line_tokens:
            continue
        token_count = len(line_tokens)
        next_start = duration
        for later_tokens, _, _, _, later_start in line_info[line_index + 1:]:
            if later_tokens:
                next_start = later_start
                break
        line_end = min(next_start - 0.05, line_start + _line_max_duration(token_count))
        if line_end <= line_start:
            line_end = line_start + 0.8

        valid = {offset: times for offset, times in known.items()
                 if 0.0 <= times[1] - times[0] <= 5.0}
        required_end = 0.0
        trailing = 0
        last_offset = None
        if valid:
            last_offset = max(valid)
            trailing = max(0, token_count - 1 - last_offset)
            required_end = valid[last_offset][1] + MIN_WORD_LENGTH * trailing

        if audio is not None:
            phrase_end = vocal_phrase_end(audio, line_start, line_end)
            if phrase_end is not None:
                line_end = min(line_end, max(line_start + 0.6,
                                             phrase_end + PHRASE_LINGER))

        if valid and trailing == 0 and len(valid) / token_count >= 0.6 and audio is not None:
            last_end = valid[max(valid)][1]
            if line_end - last_end > PHRASE_LINGER:
                tail_end = vocal_phrase_end(audio, last_end + 0.05, line_end,
                                            phrase_gap=0.8)
                if tail_end is not None:
                    line_end = max(required_end,
                                   min(line_end, tail_end + PHRASE_LINGER))

        if valid and trailing == 0 and len(valid) / token_count >= 0.75:
            last_end = valid[last_offset][1]
            gap_cutoff = next_start - 1.0 if next_start < duration else duration
            new_phrase = any(last_end + 0.05 <= word["start"] < gap_cutoff
                             for word in reference.get("local", []))
            if new_phrase:
                line_end = min(line_end, last_end + PHRASE_LINGER)

        if valid and trailing > 0:
            last_end = valid[last_offset][1]
            if line_end - last_end > 3.0:
                line_end = min(line_end,
                               last_end + max(1.2, 0.55 * trailing) + PHRASE_LINGER)
        line_end = max(line_end, required_end)
        line_end = min(duration, max(line_start + 0.3, line_end))

        line_tokens_list = [tokens[index] for index in line_tokens]
        line_text = textnorm.join_tokens(line_tokens_list)
        if not line_text:
            continue

        words: list[dict] = []
        line_level_reason = None
        if local_timing_is_reasonable(line_tokens_list, known, line_start, line_end):
            words = interpolate_known(line_tokens_list, known, line_start, line_end)
        else:
            line_level_reason = "ASR timing coverage below the line-level threshold"
            words = []

        if words:
            words = [word for word in words
                     if word["text"] and word["end"] > word["start"]]
        if words:
            words[0]["start"] = round(max(0.0, line_start), 3)
            words[-1]["end"] = round(
                min(line_end, max(words[-1]["start"] + MIN_WORD_LENGTH,
                                  words[-1]["end"])), 3)
            built.append({
                "start": words[0]["start"],
                "end": line_end,
                "text": line_text,
                "words": words,
                "instrumental": False,
                "reference_index": line_index,
                "timing_source": "asr",
            })
        else:
            built.append({
                "start": round(line_start, 3),
                "end": round(line_end, 3),
                "text": line_text,
                "words": [],
                "instrumental": False,
                "reference_index": line_index,
                "timing_source": "none",
                "line_level_reason": line_level_reason or "no usable word timing",
            })
    return built


def add_instrumental_cues(lines: list[dict], duration: float, audio=None) -> list[dict]:
    cues = [dict(line) for line in lines]
    cues.sort(key=lambda cue: cue["start"])

    def make_gap(gap_start: float, gap_end: float):
        if gap_end - gap_start <= 0.8:
            return None
        if audio is not None and gap_end - gap_start < 4.5:
            _, values = window_rms(audio, gap_start, gap_end, sample_rate=16000)
            if len(values) and float(values.max()) >= 0.12:
                return None
        return {
            "start": round(gap_start, 3),
            "end": round(gap_end, 3),
            "text": "[Instrumental]",
            "words": [],
            "instrumental": True,
            "reference_index": None,
            "timing_source": "none",
        }

    with_gaps = []
    previous_end = 0.0
    for cue in cues:
        if cue["start"] - previous_end > INSTRUMENTAL_GAP:
            gap = make_gap(previous_end + (0.2 if with_gaps else 0.4),
                           cue["start"] - 0.2)
            if gap:
                with_gaps.append(gap)
        with_gaps.append(cue)
        previous_end = max(previous_end, cue["end"])

    if duration - previous_end > INSTRUMENTAL_GAP:
        gap = make_gap(previous_end + 0.2, duration - 0.8)
        if gap:
            with_gaps.append(gap)

    ordered = []
    previous_end = -1.0
    for cue in sorted(with_gaps, key=lambda item: item["start"]):
        if cue["start"] <= previous_end:
            cue["start"] = previous_end + 0.01
        if cue["end"] <= cue["start"]:
            cue["end"] = cue["start"] + 0.5

        words = cue.get("words", [])
        cursor = cue["start"]
        for index, word in enumerate(words):
            remaining = len(words) - index - 1
            latest_start = max(cursor, cue["end"] - MIN_WORD_LENGTH * (remaining + 1))
            word_start = max(cursor, min(word["start"], latest_start))
            word_end = min(cue["end"], max(word_start + MIN_WORD_LENGTH, word["end"]))
            if word_end < word_start + MIN_WORD_LENGTH:
                cue["end"] = word_start + MIN_WORD_LENGTH * (remaining + 1)
                word_end = word_start + MIN_WORD_LENGTH
            word["start"] = round(word_start, 3)
            word["end"] = round(word_end, 3)
            cursor = word_end

        cue["start"] = max(0.0, min(cue["start"], max(0.0, duration - MIN_WORD_LENGTH)))
        cue["end"] = min(duration, max(cue["start"] + MIN_WORD_LENGTH, cue["end"]))
        if words and any(float(word.get("end", 0.0)) > cue["end"] + 0.001
                         for word in words):
            cue["words"] = []
            cue["timing_source"] = "none"
            cue["line_level_reason"] = (
                "word timing did not fit inside the recorded duration")

        ordered.append(cue)
        previous_end = cue["end"]
    return ordered


def build_from_transcript(transcript: dict, duration: float,
                          language: str = "en") -> list[dict]:
    """Split ASR words into readable lines; ASR timings are measured, not spread."""
    lines = []
    for segment in transcript.get("segments", []):
        words = []
        for word in segment.get("words", []):
            text = (word.get("word") or "").strip()
            if not textnorm.norm_token(text, language):
                continue
            start = float(word.get("start", 0.0))
            end = float(word.get("end", start + 0.15))
            words.append({"text": text, "start": start,
                          "end": max(start + MIN_WORD_LENGTH, end)})
        if not words:
            continue

        chunk = []
        chunk_start = words[0]["start"]
        for index, word in enumerate(words):
            chunk.append(word)
            is_last = index == len(words) - 1
            next_word = None if is_last else words[index + 1]
            pause = 0.0 if next_word is None else next_word["start"] - word["end"]
            ended_sentence = bool(re.search(r"[.!?][\"')\]]?$", word["text"]))
            long_chunk = word["end"] - chunk_start >= 7.0 or len(chunk) >= 10
            if is_last or ended_sentence or long_chunk or pause >= 0.8:
                for item in chunk:
                    item["text"] = textnorm.clean_text(
                        item["text"], language=language, capitalize=False)
                if chunk and chunk[0]["text"]:
                    if language in ("en", "tl"):
                        chunk[0]["text"] = (chunk[0]["text"][0].upper()
                                            + chunk[0]["text"][1:])
                    else:
                        chunk[0]["text"] = textnorm.capitalize_line(
                            chunk[0]["text"])
                text = " ".join(item["text"] for item in chunk)
                text = textnorm.clean_text(text, language=language)
                end = min(duration, chunk[-1]["end"] + 0.35)
                lines.append({
                    "start": round(chunk_start, 3),
                    "end": round(end, 3),
                    "text": text,
                    "words": chunk,
                    "instrumental": False,
                    "reference_index": None,
                    "timing_source": "asr",
                })
                chunk = []
                if next_word is not None:
                    chunk_start = next_word["start"]
    return lines


def flat_lrc_tokens(lines: list[dict], language: str = "en") -> list[dict]:
    tokens = []
    for line_index, line in enumerate(lines):
        for token_index, token in enumerate(
                textnorm.split_display_tokens(line["text"], language)):
            tokens.append({
                "text": token["text"],
                "norm": token["norm"],
                "line": line_index,
                "token": token_index,
                "line_start": line["start"],
            })
    return tokens
