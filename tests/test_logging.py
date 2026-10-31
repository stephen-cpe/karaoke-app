"""Tests for server-side logging and the browser-error relay."""

import logging
import subprocess

import pytest

import karaoke.transposition as transposition_module
from app import check_ffmpeg, logger, setup_logging


@pytest.fixture
def karaoke_handlers_cleanup():
    """Remove handlers that setup_logging() installed, after the test."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_karaoke", False):
            handler.close()
            root.removeHandler(handler)


@pytest.fixture
def capture_all(caplog):
    """Capture every record the karaoke logger emits, regardless of level."""
    with caplog.at_level(logging.DEBUG, logger="karaoke"):
        yield caplog


# --- setup_logging -----------------------------------------------------------

def test_setup_logging_writes_to_file(tmp_path, karaoke_handlers_cleanup):
    log_file = tmp_path / "karaoke.log"
    setup_logging(log_file=str(log_file), level=logging.INFO)

    logger.info("hello from the test")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "hello from the test" in log_file.read_text(encoding="utf-8")


def test_setup_logging_does_not_stack_duplicate_handlers(tmp_path, karaoke_handlers_cleanup):
    setup_logging(log_file=str(tmp_path / "a.log"))
    setup_logging(log_file=str(tmp_path / "a.log"))

    karaoke_handlers = [h for h in logging.getLogger().handlers if getattr(h, "_karaoke", False)]
    assert len(karaoke_handlers) == 2  # one console + one file, not four


# --- request logging ----------------------------------------------------------

def test_requests_are_logged(client, capture_all):
    client.get("/api/songs")
    messages = [r.getMessage() for r in capture_all.records]
    assert any("GET /api/songs -> 200" in m for m in messages)


def test_missing_media_file_logs_warning(client, capture_all):
    client.get("/songs/nope.mp3")
    warnings = [r.getMessage() for r in capture_all.records if r.levelno == logging.WARNING]
    assert any("GET /songs/nope.mp3 -> 404" in m for m in warnings)


def test_missing_lyrics_file_logs_info_not_warning(client, capture_all):
    client.get("/lyrics/nope.srt")
    records = [r for r in capture_all.records if "GET /lyrics/nope.srt" in r.getMessage()]
    assert records
    assert records[0].levelno == logging.INFO


# --- browser error relay -------------------------------------------------------

def test_browser_errors_are_relayed_to_the_log(client, capture_all):
    res = client.post("/api/log", json={
        "level": "error",
        "message": "Audio playback failed",
        "details": {"codeName": "MEDIA_ERR_SRC_NOT_SUPPORTED", "code": 4},
    })
    assert res.status_code == 204

    records = [r for r in capture_all.records if "[browser]" in r.getMessage()]
    assert records
    assert records[0].levelno == logging.ERROR
    assert "Audio playback failed" in records[0].getMessage()
    assert "MEDIA_ERR_SRC_NOT_SUPPORTED" in records[0].getMessage()


def test_browser_log_validation(client):
    assert client.post("/api/log", json={}).status_code == 400
    assert client.post("/api/log", json={"message": ""}).status_code == 400
    assert client.post("/api/log", data="hi", content_type="text/plain").status_code == 400


def test_browser_log_truncates_long_payloads(client, capture_all):
    res = client.post("/api/log", json={"message": "x" * 5000, "details": {"blob": "y" * 5000}})
    assert res.status_code == 204

    messages = [r.getMessage() for r in capture_all.records if "[browser]" in r.getMessage()]
    assert messages and len(messages[0]) < 4500


def test_browser_log_unknown_level_defaults_to_error(client, capture_all):
    client.post("/api/log", json={"level": "banana", "message": "boom"})
    records = [r for r in capture_all.records if "[browser]" in r.getMessage()]
    assert records
    assert records[0].levelno == logging.ERROR


def test_browser_log_collapses_newlines(client, capture_all):
    client.post("/api/log", json={
        "message": "first\nsecond\r\nthird",
        "details": {"text": "alpha\nbeta"},
    })
    records = [r for r in capture_all.records if "[browser]" in r.getMessage()]
    assert records
    message = records[0].getMessage()
    assert "\n" not in message and "\r" not in message
    assert "first second third" in message


# --- transposition logging ------------------------------------------------------

def test_transpose_logs_song_not_found(client, capture_all):
    client.get("/transpose", query_string={"song_id": "missing.mp3", "semitones": 1})
    warnings = [r.getMessage() for r in capture_all.records if r.levelno == logging.WARNING]
    assert any("missing.mp3" in m and "not found" in m for m in warnings)


def test_transpose_logs_ffmpeg_failure_with_output(client, add_song, capture_all, monkeypatch):
    song = add_song("1_a_b.mp3")

    def failing(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"boom: no such filter")

    monkeypatch.setattr(
        transposition_module, "_run_ffmpeg_process", failing)
    client.get("/transpose", query_string={"song_id": song, "semitones": 1})

    errors = [r.getMessage() for r in capture_all.records
              if r.levelno == logging.ERROR and "Transpose failed" in r.getMessage()]
    assert errors and "no such filter" in errors[0]


# --- FFmpeg startup diagnostics --------------------------------------------------

class FakeFFmpegRun:
    """Stands in for subprocess.run during check_ffmpeg() tests."""

    def __init__(self, filters_output):
        self.filters_output = filters_output

    def __call__(self, cmd, **kwargs):
        if "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"ffmpeg version 7.1\n", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=self.filters_output, stderr=b"")


def test_check_ffmpeg_not_found(monkeypatch):
    monkeypatch.setattr(transposition_module.shutil, "which", lambda name: None)
    ok, description = check_ffmpeg()
    assert ok is False
    assert "not found" in description


def test_check_ffmpeg_reports_rubberband_available(monkeypatch):
    monkeypatch.setattr(transposition_module.shutil, "which", lambda name: r"C:\ffmpeg\ffmpeg.exe")
    monkeypatch.setattr(transposition_module.subprocess, "run",
                        FakeFFmpegRun(b" ..C rubberband    Apply time-stretching and pitch-shifting.\n"))
    ok, description = check_ffmpeg()
    assert ok is True
    assert "7.1" in description
    assert "rubberband filter available" in description


def test_check_ffmpeg_reports_missing_rubberband(monkeypatch):
    monkeypatch.setattr(transposition_module.shutil, "which", lambda name: r"C:\ffmpeg\ffmpeg.exe")
    monkeypatch.setattr(transposition_module.subprocess, "run", FakeFFmpegRun(b" T.C atempo    Adjust audio tempo.\n"))
    ok, description = check_ffmpeg()
    assert ok is True
    assert "NOT available" in description

def test_request_path_logging_strips_control_characters(client, capture_all):
    client.get("/%0aPWNED")
    messages = [r.getMessage() for r in capture_all.records]
    assert any("PWNED" in message for message in messages)
    assert all("\n" not in message and "\r" not in message for message in messages)


def test_check_ffmpeg_reports_failure_when_it_cannot_run(monkeypatch):
    monkeypatch.setattr(transposition_module.shutil, "which", lambda name: r"C:\ffmpeg\ffmpeg.exe")

    def broken(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(transposition_module.subprocess, "run", broken)
    ok, description = check_ffmpeg()
    assert ok is False
    assert "running it failed" in description
