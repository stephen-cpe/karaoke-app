"""Tests for the /transpose endpoint (FFmpeg is mocked; no real encoding)."""

import os
import subprocess


def fake_ffmpeg(monkeypatch, calls):
    """Patch subprocess.run with a stub that 'produces' the output file."""

    def _run(cmd, **kwargs):
        calls.append(cmd)
        with open(cmd[-1], "wb") as fh:
            fh.write(b"transposed")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _run)


def test_semitones_zero_returns_original_song(client, add_song):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    res = client.get("/transpose", query_string={"song_id": song, "semitones": 0})
    assert res.status_code == 200
    assert res.get_json() == {"url": f"/songs/{song}"}


def test_post_json_body_is_supported(client, add_song):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    res = client.post("/transpose", json={"song_id": song, "semitones": 0})
    assert res.status_code == 200
    assert res.get_json() == {"url": f"/songs/{song}"}


def test_post_json_must_be_an_object(client, add_song):
    add_song("1_a_b.mp3")
    res = client.post("/transpose", json=["not", "an", "object"])
    assert res.status_code == 400


def test_transpose_rejects_non_string_song_id(client):
    res = client.post("/transpose", json={"song_id": 123, "semitones": 1})
    assert res.status_code == 400


def test_unknown_song_returns_404(client):
    res = client.get("/transpose", query_string={"song_id": "nope.mp3", "semitones": 1})
    assert res.status_code == 404


def test_traversal_attempts_are_rejected(client):
    for bad in ["../app.py", "..\\app.py", "songs/../app.py", "C:\\windows\\evil.mp3"]:
        res = client.get("/transpose", query_string={"song_id": bad, "semitones": 1})
        assert res.status_code == 404


def test_missing_song_id_returns_400(client):
    res = client.get("/transpose", query_string={"semitones": 1})
    assert res.status_code == 400


def test_invalid_or_out_of_range_semitones_return_400(client, add_song):
    song = add_song("1_a_b.mp3")
    for bad in ["abc", "", 5, -9]:
        res = client.get("/transpose", query_string={"song_id": song, "semitones": bad})
        assert res.status_code == 400


def test_cached_transposition_served_without_ffmpeg(client, app, add_song, monkeypatch):
    song = add_song("1_a_b.mp3")
    cached = os.path.join(app.config["TRANSPOSED_DIR"], f"+1_{song}")
    with open(cached, "wb") as fh:
        fh.write(b"cached")

    def boom(*args, **kwargs):
        raise AssertionError("ffmpeg must not run when the transposed file is cached")

    monkeypatch.setattr(subprocess, "run", boom)
    res = client.get("/transpose", query_string={"song_id": song, "semitones": 1})
    assert res.status_code == 200
    assert res.get_json() == {"url": f"/transposed/+1_{song}"}


def test_generates_and_caches_transposed_file(client, app, add_song, monkeypatch):
    song = add_song("1_a_b.mp3")
    calls = []
    monkeypatch.delenv("KARAOKE_FFMPEG_FILTER", raising=False)
    fake_ffmpeg(monkeypatch, calls)

    res = client.get("/transpose", query_string={"song_id": song, "semitones": 2})
    assert res.status_code == 200
    assert res.get_json() == {"url": f"/transposed/+2_{song}"}
    assert os.path.isfile(os.path.join(app.config["TRANSPOSED_DIR"], f"+2_{song}"))

    (cmd,) = calls
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-filter:a") + 1] == f"rubberband=pitch={2 ** (2 / 12)}"


def test_missing_ffmpeg_returns_clear_error(client, add_song, monkeypatch):
    song = add_song("1_a_b.mp3")

    def no_ffmpeg(*args, **kwargs):
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr(subprocess, "run", no_ffmpeg)
    res = client.get("/transpose", query_string={"song_id": song, "semitones": 1})
    assert res.status_code == 500
    assert "ffmpeg" in res.get_json()["error"]


def test_failed_ffmpeg_returns_details_and_removes_partial_file(client, app, add_song, monkeypatch):
    song = add_song("1_a_b.mp3")

    def failing(cmd, **kwargs):
        with open(cmd[-1], "wb") as fh:
            fh.write(b"partial")
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"boom")

    monkeypatch.setattr(subprocess, "run", failing)
    res = client.get("/transpose", query_string={"song_id": song, "semitones": 1})
    assert res.status_code == 500
    assert res.get_json()["details"] == "boom"
    assert os.listdir(app.config["TRANSPOSED_DIR"]) == []


def test_ffmpeg_timeout_returns_504_and_removes_partial_file(client, app, add_song, monkeypatch):
    song = add_song("1_a_b.mp3")

    def timing_out(cmd, **kwargs):
        with open(cmd[-1], "wb") as fh:
            fh.write(b"partial")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", timing_out)
    res = client.get("/transpose", query_string={"song_id": song, "semitones": 1})
    assert res.status_code == 504
    assert "timed out" in res.get_json()["error"].lower()
    assert os.listdir(app.config["TRANSPOSED_DIR"]) == []


def test_wav_source_is_cached_as_mp3(client, app, add_song, monkeypatch):
    song = add_song("1_a_b.wav")
    calls = []
    fake_ffmpeg(monkeypatch, calls)

    res = client.get("/transpose", query_string={"song_id": song, "semitones": 1})
    assert res.status_code == 200
    assert res.get_json() == {"url": "/transposed/+1_1_a_b.mp3"}
    assert os.path.isfile(os.path.join(app.config["TRANSPOSED_DIR"], "+1_1_a_b.mp3"))
    assert calls[0][calls[0].index("-f") + 1] == "mp3"


def test_filter_template_can_be_overridden_with_env(client, add_song, monkeypatch):
    song = add_song("1_a_b.mp3")
    calls = []
    monkeypatch.setenv("KARAOKE_FFMPEG_FILTER", "asetrate=44100*{pitch},atempo=1/{pitch}")
    fake_ffmpeg(monkeypatch, calls)

    client.get("/transpose", query_string={"song_id": song, "semitones": 1})
    pitch = 2 ** (1 / 12)
    assert calls[0][calls[0].index("-filter:a") + 1] == f"asetrate=44100*{pitch},atempo=1/{pitch}"
