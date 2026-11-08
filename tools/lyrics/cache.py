"""Content-addressed cache keys and atomic JSON writes."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

CACHE_VERSION = 2


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def atomic_write_text(path: Path, text: str) -> None:
    """Write text without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def stem_cache_root(cache_dir: Path, original_hash: str) -> Path:
    return cache_dir / "stems" / original_hash[:12]


def stem_cache_path(cache_dir: Path, stem: str, original_hash: str) -> Path:
    return stem_cache_root(cache_dir, original_hash) / "htdemucs" / stem / "vocals.wav"


def transcript_cache_key(*, vocal_hash: str, original_hash: str, model: str,
                         language: str, prompt: str, chunk: float, overlap: float,
                         transcriber_version: str, beam_size: int,
                         temperature: float, condition_on_previous_text: bool,
                         word_timestamps: bool) -> str:
    material = {
        "cache_version": CACHE_VERSION,
        "vocal_sha256": vocal_hash,
        "original_sha256": original_hash,
        "model": model,
        "language": language,
        "prompt": prompt,
        "chunk": chunk,
        "overlap": overlap,
        "transcriber_version": transcriber_version,
        "beam_size": beam_size,
        "temperature": temperature,
        "condition_on_previous_text": condition_on_previous_text,
        "word_timestamps": word_timestamps,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def transcript_cache_path(cache_dir: Path, stem: str, key: str) -> Path:
    return cache_dir / "transcripts" / f"{stem}-{key[:12]}.json"


def load_transcript_cache(path: Path, expected_key: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("key") != expected_key:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def save_transcript_cache(path: Path, key: str, data: dict[str, Any]) -> None:
    atomic_write_json(path, {
        "key": key,
        "created_at": int(time.time()),
        "data": data,
    })


def lrclib_cache_path(cache_dir: Path, kind: str, material: str) -> Path:
    return cache_dir / "lrclib-v2" / f"{kind}-{short_hash(material, 16)}.json"
