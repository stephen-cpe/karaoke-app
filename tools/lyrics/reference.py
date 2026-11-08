"""LRCLIB lookup, candidate parsing, user overrides, and candidate choice."""
from __future__ import annotations

import difflib
import email.utils
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import cache, langdetect, textnorm
from .audio import ONSET_RMS, max_window_rms, vocal_onset

log = logging.getLogger("lyrics")

USER_AGENT = "karaoke-lyrics-sync/1.0 (personal local use)"
LRCLIB_TIMEOUT = 30
LRCLIB_DURATION_TOLERANCE = 5.0
LRCLIB_OK_TTL = 90 * 24 * 3600
LRCLIB_NOT_FOUND_TTL = 7 * 24 * 3600
LRC_TIMESTAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
VERSION_PENALTIES = {
    "karaoke": 6.0,
    "instrumental": 6.0,
    "cover": 3.0,
    "acoustic": 1.2,
    "live": 1.2,
    "remix": 1.2,
    "demo": 1.2,
}


class LRCLIBError(RuntimeError):
    """An unexpected LRCLIB response."""


class LRCLIBUnavailable(RuntimeError):
    """LRCLIB could not be reached or kept failing."""


@dataclass
class LookupResult:
    status: str
    records: list[dict[str, Any]] = field(default_factory=list)
    cached: bool = False
    error: str | None = None
    requests: int = 0


def _marker_line(text: str) -> bool:
    stripped = text.strip()
    return bool(re.fullmatch(r"\[[^\]]+\]", stripped)
                or re.fullmatch(r"\(instrumental\)", stripped, re.I))


def lrc_raw_lines(synced: str) -> list[dict[str, Any]]:
    rows = []
    for raw in (synced or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        stamps = LRC_TIMESTAMP.findall(raw)
        if not stamps:
            continue
        text = LRC_TIMESTAMP.sub("", raw).strip()
        if not text:
            continue
        if _marker_line(text) or re.fullmatch(r"\(instrumental\)", text, re.I):
            continue
        for minutes, seconds in stamps:
            rows.append({
                "start": int(minutes) * 60 + float(seconds),
                "end": None,
                "text": text,
                "words": [],
            })
    rows.sort(key=lambda row: row["start"])
    return rows


def parse_lrc(synced: str, language: str = "en") -> list[dict[str, Any]]:
    rows = lrc_raw_lines(synced)
    for row in rows:
        row["text"] = textnorm.clean_text(row["text"], language=language)
    return rows


def parse_plain(plain: str, language: str = "en") -> list[dict[str, Any]]:
    rows = []
    for raw in (plain or "").splitlines():
        text = raw.strip()
        if not text or _marker_line(text):
            continue
        rows.append({
            "start": None,
            "end": None,
            "text": textnorm.clean_text(text, language=language),
            "words": [],
        })
    return rows


def _ms_to_seconds(value: Any) -> float | None:
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def parse_lyricsfile(value: Any, language: str = "en") -> list[dict[str, Any]]:
    if not value:
        return []
    payload = value
    if isinstance(value, str):
        try:
            import yaml
        except ImportError:
            log.warning("PyYAML is not installed; lyricsfile cannot be parsed")
            return []
        try:
            payload = yaml.safe_load(value)
        except Exception as exc:
            log.warning("lyricsfile is not valid YAML: %s", exc)
            return []
    if not isinstance(payload, dict):
        return []
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return []
    rows = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        raw_text = line.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        if _marker_line(raw_text):
            continue
        start = _ms_to_seconds(line.get("start_ms"))
        end = _ms_to_seconds(line.get("end_ms"))
        words = []
        for word in line.get("words") or []:
            if not isinstance(word, dict):
                continue
            word_text = word.get("text")
            if not isinstance(word_text, str) or not word_text.strip():
                continue
            word_start = _ms_to_seconds(word.get("start_ms"))
            words.append({
                "text": str(word_text).strip(),
                "start": word_start,
                "end": None,
            })
        words.sort(key=lambda item: item["start"] if item["start"] is not None else 0.0)
        for index, word in enumerate(words):
            next_start = (words[index + 1]["start"]
                          if index + 1 < len(words) else end)
            word["end"] = next_start if next_start is not None else word["start"]
        rows.append({
            "start": start,
            "end": end,
            "text": textnorm.clean_text(raw_text.strip(), language=language,
                                        capitalize=False),
            "words": words,
        })
    rows.sort(key=lambda row: row["start"] if row["start"] is not None else 0.0)
    return rows


def _form_text(lines: list[dict[str, Any]]) -> str:
    return "\n".join(str(line.get("text", "")) for line in lines)


def record_forms(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Parse all text forms in a record without choosing a language yet."""
    raw_forms: dict[str, list[dict[str, Any]]] = {
        "lyricsfile": parse_lyricsfile(record.get("lyricsfile") or "", "en"),
        "synced": lrc_raw_lines(record.get("syncedLyrics") or ""),
        "plain": [],
    }
    for raw in (record.get("plainLyrics") or "").splitlines():
        text = raw.strip()
        if text and not _marker_line(text):
            raw_forms["plain"].append({
                "start": None, "end": None, "text": text, "words": [],
            })
    forms = {}
    for name, rows in raw_forms.items():
        text = _form_text(rows)
        forms[name] = {
            "name": name,
            "lines": rows,
            "text": text,
            "line_count": len(rows),
            "has_timestamps": any(row.get("start") is not None for row in rows),
            "word_sync": any(row.get("words") for row in rows),
            "script": langdetect.script_summary(text),
            "tagalog": langdetect.tagalog_lexicon(text),
        }
    return forms


def version_penalty(candidate: dict[str, Any], duration: float) -> float:
    haystack = " ".join(
        str(candidate.get(key) or "")
        for key in ("trackName", "albumName", "artistName")
    ).lower()
    penalty = sum(weight for word, weight in VERSION_PENALTIES.items()
                  if re.search(rf"\b{re.escape(word)}\b", haystack))
    candidate_duration = candidate.get("duration")
    if candidate_duration and duration > 0:
        penalty += min(2.0, abs(float(candidate_duration) - duration) / 10.0)
    return penalty


def form_quality(form: dict[str, Any]) -> float:
    score = 0.0
    if form.get("line_count"):
        score += 1.0
    if form.get("has_timestamps"):
        score += 0.7
    if form.get("word_sync"):
        score += 0.8
    return score


def choose_form(forms: dict[str, dict[str, Any]], language: str) -> dict[str, Any]:
    preference = ("synced", "lyricsfile", "plain") if language == "en" else (
        "lyricsfile", "synced", "plain")
    best = None
    best_score = -1.0
    for name in preference:
        form = forms.get(name)
        if not form or not form.get("lines"):
            continue
        compatibility = langdetect.script_compatibility(language, form["text"])
        score = compatibility * 5.0 + form_quality(form)
        if name == "lyricsfile" and form.get("word_sync"):
            score += 0.5
        if best is None or score > best_score:
            best = form
            best_score = score
    return best or {
        "name": "none", "lines": [], "text": "", "line_count": 0,
        "has_timestamps": False, "word_sync": False,
        "script": langdetect.script_summary(""),
        "tagalog": langdetect.tagalog_lexicon(""),
    }


def clean_lines(lines: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    cleaned = []
    for line in lines:
        text = textnorm.clean_text(str(line.get("text") or ""),
                                   language=language, capitalize=True)
        if not text:
            continue
        cleaned.append({
            "start": line.get("start"),
            "end": line.get("end"),
            "text": text,
            "words": list(line.get("words") or []),
        })
    return cleaned


def estimate_line_starts(lines: list[dict[str, Any]], local_words: list[dict[str, Any]],
                         duration: float) -> list[dict[str, Any]]:
    if all(line.get("start") is not None for line in lines):
        return lines
    if local_words:
        first = float(local_words[0]["start"])
        last = float(local_words[-1]["end"])
    else:
        first = 0.0
        last = max(1.0, duration)
    span = max(0.5, last - first)
    weights = [max(1, len(str(line.get("text") or ""))) for line in lines]
    total = sum(weights)
    cursor = first
    result = []
    for line, weight in zip(lines, weights):
        row = dict(line)
        if row.get("start") is None:
            row["start"] = cursor
        cursor += span * weight / total
        result.append(row)
    return result


def flatten_tokens(lines: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    tokens = []
    for line_index, line in enumerate(lines):
        for token_index, token in enumerate(
                textnorm.split_display_tokens(str(line.get("text", "")), language)):
            tokens.append({
                "text": token["text"],
                "norm": token["norm"],
                "line": line_index,
                "token": token_index,
                "line_start": float(line.get("start") or 0.0),
            })
    return tokens


def _line_energy_score(lines: list[dict[str, Any]], audio, duration: float) -> float:
    if audio is None:
        return 0.0
    timed = [line for line in lines if line.get("start") is not None]
    if not timed:
        return 0.0
    hits = 0
    for line in timed:
        start = float(line["start"])
        end = min(duration, float(line.get("end") or start + 4.0))
        rms = max_window_rms(audio, start, min(end, start + 2.0))
        onset = vocal_onset(audio, max(0.0, start - 1.5), min(duration, start + 1.5))
        if rms >= ONSET_RMS or onset is not None:
            hits += 1
    return hits / len(timed)


def _text_match_score(local: list[dict[str, Any]], tokens: list[dict[str, Any]],
                      lines: list[dict[str, Any]]) -> tuple[float, float, float]:
    if not local or not tokens:
        return 0.0, 0.0, 0.0
    matcher = difflib.SequenceMatcher(
        None, [word["norm"] for word in local], [token["norm"] for token in tokens],
        autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    matched = sum(block.size for block in blocks)
    if not matched:
        return 0.0, 0.0, 0.0
    token_local = {}
    for block in blocks:
        for offset in range(block.size):
            token_local[block.b + offset] = block.a + offset
    offsets = []
    first_token: dict[int, int] = {}
    for token_index, token in enumerate(tokens):
        first_token.setdefault(token["line"], token_index)
    for token_index, local_index in token_local.items():
        line_index = tokens[token_index]["line"]
        if token_index != first_token[line_index]:
            continue
        line_start = lines[line_index].get("start")
        if line_start is None:
            continue
        offsets.append(float(local[local_index]["start"]) - float(line_start))
    offset = sorted(offsets)[len(offsets) // 2] if offsets else 0.0
    return matched / len(tokens), matched / len(local), offset


def score_record(record: dict[str, Any], forms: dict[str, dict[str, Any]],
                 language: str, duration: float, audio,
                 local: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    candidate_duration = record.get("duration")
    duration_error = (abs(float(candidate_duration) - duration)
                      if candidate_duration is not None else 999.0)
    form = choose_form(forms, language)
    lines = clean_lines(form.get("lines") or [], language)
    lines = estimate_line_starts(lines, local or [], duration)
    tokens = flatten_tokens(lines, language) if lines else []
    compatibility = langdetect.script_compatibility(language, form.get("text") or "")
    energy = _line_energy_score(lines, audio, duration)
    penalty = version_penalty(record, duration)
    coverage = local_coverage = offset = 0.0
    if local and tokens:
        coverage, local_coverage, offset = _text_match_score(local, tokens, lines)
    score = (
        4.0 * compatibility
        + 2.0 * energy
        + form_quality(form)
        + 8.0 * coverage
        + 4.0 * local_coverage
        - 0.05 * abs(offset)
        - penalty
    )
    if duration_error > LRCLIB_DURATION_TOLERANCE:
        score -= 20.0
    if record.get("instrumental"):
        score -= 20.0
    return {
        "record": record,
        "forms": forms,
        "form": form,
        "lines": lines,
        "tokens": tokens,
        "language": language,
        "score": score,
        "duration_error": duration_error,
        "compatibility": compatibility,
        "energy_score": energy,
        "penalty": penalty,
        "coverage": coverage,
        "local_coverage": local_coverage,
        "offset": offset,
        "word_sync": bool(form.get("word_sync")),
        "form_name": form.get("name"),
    }


def choose_candidate(records: list[dict[str, Any]], language: str, duration: float,
                     audio, local: list[dict[str, Any]] | None = None,
                     fetch_full: Callable[[int], dict[str, Any] | None] | None = None
                     ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not records:
        return None, []
    preliminary = []
    for record in records:
        forms = record_forms(record)
        scored = score_record(record, forms, language, duration, audio, local)
        preliminary.append(scored)
    preliminary.sort(key=lambda item: item["score"], reverse=True)

    # Fetch the top few full records by id; search results can carry a trimmed
    # lyricsfile while /api/get/<id> has the complete one.
    if fetch_full is not None:
        for item in preliminary[:3]:
            record_id = item["record"].get("id")
            if record_id is None:
                continue
            try:
                full = fetch_full(int(record_id))
            except Exception as exc:
                log.warning("  LRCLIB by-id fetch failed for %s: %s", record_id, exc)
                continue
            if not full:
                continue
            forms = record_forms(full)
            rescored = score_record(full, forms, language, duration, audio, local)
            item.update(rescored)
        preliminary.sort(key=lambda item: item["score"], reverse=True)

    best = preliminary[0]
    if best["duration_error"] > LRCLIB_DURATION_TOLERANCE or best["record"].get("instrumental"):
        return None, preliminary
    return best, preliminary


def legacy_search_path(cache_dir: Path, artist: str, title: str) -> Path:
    """Path of the pre-Phase-1 LRCLIB search cache for one song."""
    key = re.sub(r"[^a-z0-9]+", "_", f"{artist}_{title}".lower()).strip("_")
    return cache_dir / "lrclib" / f"{key}.json"


def load_legacy_search(cache_dir: Path, artist: str, title: str
                       ) -> list[dict[str, Any]] | None:
    """Load the old artist/title search cache used by the English pipeline.

    Phase 1 switched to the LRCLIB /api/get endpoint, whose records can carry
    a different ``lyricsfile`` form. English output must keep the old synced
    text, so the compatibility path reads the same cache the old code used.
    """
    path = legacy_search_path(cache_dir, artist, title)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    return [record for record in data if isinstance(record, dict)]


def _token_match(local: list[dict[str, Any]],
                 tokens: list[dict[str, Any]]) -> tuple[int, dict[int, int]]:
    if not local or not tokens:
        return 0, {}
    matcher = difflib.SequenceMatcher(
        None, [word["norm"] for word in local],
        [token["norm"] for token in tokens], autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    matched = sum(block.size for block in blocks)
    token_local: dict[int, int] = {}
    for block in blocks:
        for offset in range(block.size):
            token_local[block.b + offset] = block.a + offset
    return matched, token_local


def _robust_line_anchors(lines: list[dict[str, Any]],
                         tokens: list[dict[str, Any]],
                         token_local: dict[int, int],
                         local: list[dict[str, Any]]
                         ) -> tuple[dict[int, float], dict[str, float]]:
    """Keep line anchors that agree with the song's overall text offset."""
    starts_by_line: dict[int, list[float]] = {}
    first_token: dict[int, int] = {}
    for token_index, token in enumerate(tokens):
        first_token.setdefault(token["line"], token_index)
    for token_index, local_index in token_local.items():
        line_index = tokens[token_index]["line"]
        if token_index != first_token[line_index]:
            continue
        starts_by_line.setdefault(line_index, []).append(
            local[local_index]["start"])
    if not starts_by_line:
        return {}, {}

    offsets = sorted(min(starts) - float(lines[line_index]["start"])
                     for line_index, starts in starts_by_line.items())
    median = offsets[len(offsets) // 2]
    mad = sorted(abs(offset - median) for offset in offsets)[len(offsets) // 2]
    tolerance = max(1.25, min(3.0, 5.0 * mad))

    anchors = {}
    for line_index, starts in starts_by_line.items():
        start = min(starts)
        if abs(start - float(lines[line_index]["start"]) - median) <= tolerance:
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


def choose_legacy_candidate(records: list[dict[str, Any]], language: str,
                            duration: float, local: list[dict[str, Any]]
                            ) -> tuple[dict[str, Any] | None,
                                       list[dict[str, Any]]]:
    """Score old-cache synced lyrics against Whisper words like Phase 0 did."""
    if not records or not local:
        return None, []
    scored_items = []
    for record in records:
        lines = parse_lrc(record.get("syncedLyrics") or "", language)
        if not lines:
            continue
        tokens = flatten_tokens(lines, language)
        if not tokens:
            continue
        matched, token_local = _token_match(local, tokens)
        if not matched:
            continue
        anchors, anchor_stats = _robust_line_anchors(
            lines, tokens, token_local, local)
        if not anchors:
            continue
        offset = anchor_stats["median_offset"]
        coverage = matched / len(tokens)
        local_coverage = matched / len(local)
        penalty = version_penalty(record, duration)
        score = 8 * coverage + 4 * local_coverage - 0.05 * abs(offset) - penalty
        text = _form_text(lines)
        scored_items.append({
            "record": record,
            "forms": {},
            "form": {
                "name": "synced",
                "lines": lines,
                "text": text,
                "line_count": len(lines),
                "has_timestamps": True,
                "word_sync": False,
                "script": langdetect.script_summary(text),
                "tagalog": langdetect.tagalog_lexicon(text),
            },
            "lines": lines,
            "tokens": tokens,
            "language": language,
            "score": score,
            "duration_error": (
                abs(float(record.get("duration")) - duration)
                if record.get("duration") is not None else 999.0),
            "compatibility": langdetect.script_compatibility(language, text),
            "energy_score": 0.0,
            "penalty": penalty,
            "coverage": coverage,
            "local_coverage": local_coverage,
            "offset": offset,
            "word_sync": False,
            "form_name": "synced",
            "token_local": token_local,
            "anchors": anchors,
            "anchor_mad": anchor_stats["mad"],
            "anchor_tolerance": anchor_stats["tolerance"],
        })
    scored_items.sort(key=lambda item: item["score"], reverse=True)
    return (scored_items[0] if scored_items else None), scored_items


def user_override(source_dir: Path, stem: str) -> dict[str, Any] | None:
    """Load lyrics/source/<stem>.lrc, .txt, or .json when present."""
    if not source_dir.is_dir():
        return None
    language = None
    language_path = source_dir / f"{stem}.json"
    if language_path.is_file():
        try:
            payload = json.loads(language_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("language"), str):
                language = payload["language"].strip().lower() or None
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("  user language override is unreadable: %s", exc)
    for suffix in (".lrc", ".txt"):
        path = source_dir / f"{stem}{suffix}"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            log.warning("  user lyric override is unreadable: %s", exc)
            return None
        if suffix == ".lrc":
            rows = lrc_raw_lines(text)
        else:
            rows = [{"start": None, "end": None, "text": line.strip(), "words": []}
                    for line in text.splitlines() if line.strip()]
        if rows:
            return {"path": str(path), "lines": rows, "language": language,
                    "text_source": "user"}
    if language:
        return {"path": str(language_path), "lines": [], "language": language,
                "text_source": "user"}
    return None


def _retry_after_seconds(headers: Any) -> float | None:
    value = None
    try:
        value = headers.get("Retry-After") if headers else None
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        moment = email.utils.parsedate_to_datetime(str(value))
        return max(0.0, moment.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


class LRCLIBClient:
    def __init__(self, cache_dir: Path, *, timeout: float = LRCLIB_TIMEOUT,
                 min_interval: float = 0.35, http_json=None):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.min_interval = min_interval
        self.requests = 0
        self._last_request = 0.0
        self._http_json = http_json or self._default_http_json

    def _wait(self):
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0.0, 0.12))

    def _default_http_json(self, url: str, params: dict[str, Any] | None):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                data = json.loads(body) if body else None
                return response.status, data, dict(response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                data = json.loads(body) if body else None
            except json.JSONDecodeError:
                data = None
            return exc.code, data, dict(exc.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LRCLIBUnavailable(str(exc)) from exc

    def _request(self, path: str, params: dict[str, Any] | None = None):
        url = "https://lrclib.net" + path
        not_found_attempts = 0
        attempt = 0
        last_error = None
        while attempt < 4:
            attempt += 1
            self._wait()
            try:
                status, data, headers = self._http_json(url, params)
            except LRCLIBUnavailable as exc:
                last_error = exc
                if attempt >= 4:
                    raise
                time.sleep((2 ** (attempt - 1)) * 0.6 + random.uniform(0.0, 0.3))
                continue
            finally:
                self._last_request = time.monotonic()
                self.requests += 1

            if status == 200:
                return data
            if status == 404:
                not_found_attempts += 1
                if not_found_attempts >= 2:
                    return None
                time.sleep(0.8 + random.uniform(0.0, 0.3))
                continue
            if status in (429, 500, 502, 503, 504):
                retry_after = _retry_after_seconds(headers)
                if attempt >= 4:
                    raise LRCLIBUnavailable(
                        f"LRCLIB returned {status} after {attempt} attempts")
                delay = retry_after if retry_after is not None else (
                    (2 ** (attempt - 1)) * 0.8 + random.uniform(0.0, 0.4))
                time.sleep(min(30.0, delay))
                continue
            raise LRCLIBError(f"LRCLIB returned HTTP {status} for {path}")
        raise LRCLIBUnavailable(str(last_error or "LRCLIB request failed"))

    def _read_lookup_cache(self, path: Path) -> LookupResult | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        age = time.time() - float(payload.get("fetched_at") or 0.0)
        status = payload.get("status")
        if status == "ok" and age <= LRCLIB_OK_TTL:
            records = payload.get("records")
            if isinstance(records, list):
                return LookupResult("ok", records, cached=True)
        if status == "not_found" and age <= LRCLIB_NOT_FOUND_TTL:
            return LookupResult("not_found", [], cached=True)
        return None

    def lookup(self, track_name: str, artist_name: str, duration: float) -> LookupResult:
        material = json.dumps({
            "track_name": track_name,
            "artist_name": artist_name,
            "duration": round(float(duration), 3),
        }, sort_keys=True, separators=(",", ":"))
        path = cache.lrclib_cache_path(self.cache_dir, "lookup", material)
        cached = self._read_lookup_cache(path)
        if cached is not None:
            return cached

        records: dict[str, dict[str, Any]] = {}
        unavailable: list[str] = []
        try:
            exact = self._request("/api/get", {
                "track_name": track_name,
                "artist_name": artist_name,
                "duration": round(float(duration)),
            })
            if isinstance(exact, dict):
                records[str(exact.get("id") or "exact")] = exact
        except LRCLIBUnavailable as exc:
            unavailable.append(str(exc))

        try:
            search = self._request("/api/search", {
                "track_name": track_name,
                "artist_name": artist_name,
            })
            if isinstance(search, list):
                for record in search:
                    if not isinstance(record, dict):
                        continue
                    key = str(record.get("id") or "")
                    if not key:
                        continue
                    existing = records.get(key)
                    if existing is None:
                        records[key] = record
                    else:
                        existing_file = existing.get("lyricsfile") or ""
                        new_file = record.get("lyricsfile") or ""
                        if len(new_file) > len(existing_file):
                            records[key] = record
        except LRCLIBUnavailable as exc:
            unavailable.append(str(exc))
            log.warning("  LRCLIB search unavailable: %s", exc)

        if not records and unavailable:
            return LookupResult("unreachable", [], error="; ".join(unavailable),
                                requests=self.requests)

        if not records:
            cache.atomic_write_json(path, {
                "status": "not_found",
                "fetched_at": time.time(),
                "track_name": track_name,
                "artist_name": artist_name,
                "duration": round(float(duration), 3),
            })
            return LookupResult("not_found", [], requests=self.requests)

        result_records = list(records.values())
        cache.atomic_write_json(path, {
            "status": "ok",
            "fetched_at": time.time(),
            "track_name": track_name,
            "artist_name": artist_name,
            "duration": round(float(duration), 3),
            "records": result_records,
        })
        return LookupResult("ok", result_records, requests=self.requests)

    def fetch_by_id(self, record_id: int) -> dict[str, Any] | None:
        material = str(record_id)
        path = cache.lrclib_cache_path(self.cache_dir, "id", material)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - float(payload.get("fetched_at") or 0.0)
            if payload.get("status") == "ok" and age <= LRCLIB_OK_TTL:
                record = payload.get("record")
                return record if isinstance(record, dict) else None
            if payload.get("status") == "not_found" and age <= LRCLIB_NOT_FOUND_TTL:
                return None
        except (OSError, json.JSONDecodeError):
            pass
        record = self._request(f"/api/get/{record_id}")
        if isinstance(record, dict):
            cache.atomic_write_json(path, {
                "status": "ok", "fetched_at": time.time(), "record": record,
            })
            return record
        cache.atomic_write_json(path, {
            "status": "not_found", "fetched_at": time.time(),
        })
        return None


def reference_from_override(override: dict[str, Any], language: str,
                            duration: float,
                            local: list[dict[str, Any]] | None = None
                            ) -> dict[str, Any]:
    lines = clean_lines(override.get("lines") or [], language)
    lines = estimate_line_starts(lines, local or [], duration)
    tokens = flatten_tokens(lines, language) if lines else []
    return {
        "record": None,
        "forms": {},
        "form": {"name": "user", "lines": lines, "word_sync": False},
        "lines": lines,
        "tokens": tokens,
        "language": language,
        "score": 1.0,
        "duration_error": 0.0,
        "compatibility": 1.0,
        "energy_score": 0.0,
        "penalty": 0.0,
        "coverage": 0.0,
        "local_coverage": 0.0,
        "offset": 0.0,
        "word_sync": False,
        "form_name": "user",
        "text_source": "user",
        "override_path": override.get("path"),
    }


def reference_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    form = candidate["form"]
    return {
        **candidate,
        "text_source": {
            "lyricsfile": "lrclib-lyricsfile",
            "synced": "lrclib-synced",
            "plain": "lrclib-plain",
        }.get(form.get("name"), "lrclib-plain"),
    }


def candidate_line_starts(candidate: dict[str, Any]) -> list[float | None]:
    return [line.get("start") for line in candidate.get("lines", [])]


def reference_duration(candidate: dict[str, Any]) -> float:
    record = candidate.get("record") or {}
    try:
        return float(record.get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0
