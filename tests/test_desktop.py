"""Tests for desktop helpers and application version metadata."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from karaoke import desktop, transposition, version


def test_version_label_and_config(app):
    assert version.__version__ == "0.0.1"
    assert version.version_label() == "0.0.1 (PRE-ALPHA)"
    assert app.config["APP_VERSION"] == "0.0.1"
    assert app.config["APP_VERSION_LABEL"] == "0.0.1 (PRE-ALPHA)"
    assert app.config["APP_PRE_ALPHA"] is True


def test_info_endpoint_exposes_version(client):
    info = client.get("/api/info").get_json()
    assert info["version"] == "0.0.1"
    assert info["version_label"] == "0.0.1 (PRE-ALPHA)"
    assert info["pre_alpha"] is True


def test_ensure_data_directories_creates_writable_folders(tmp_path):
    directories = desktop.ensure_data_directories(tmp_path)

    assert set(directories) == {"songs", "lyrics", "transposed"}
    for directory in directories.values():
        assert directory.is_dir()
        assert not list(directory.glob(".karaoke-write-test-*"))


def test_ensure_data_directories_reports_unwritable_folder(
        tmp_path, monkeypatch):
    def fail_write(self, *args, **kwargs):
        raise PermissionError("simulated read-only folder")

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(desktop.StartupError, match="not writable"):
        desktop.ensure_data_directories(tmp_path)


def test_urls_for_port_uses_detected_lan_ip(monkeypatch):
    monkeypatch.setattr(desktop, "local_ip", lambda: "192.168.1.5")
    urls = desktop.urls_for_port(8087)

    assert urls == {
        "lan_ip": "192.168.1.5",
        "local_player": "http://localhost:8087/player",
        "phone_remote": "http://192.168.1.5:8087/remote",
        "other_devices": "http://192.168.1.5:8087",
        "lan_available": True,
    }


def test_urls_for_port_marks_loopback_as_no_lan(monkeypatch):
    monkeypatch.setattr(desktop, "local_ip", lambda: "127.0.0.1")
    urls = desktop.urls_for_port(8080)

    assert urls["lan_available"] is False
    assert urls["local_player"].endswith("/player")


def test_server_fallback_tries_ports_in_order(monkeypatch):
    attempts = []

    def fake_create_server(app, *, host, port, threads):
        attempts.append((host, port, threads))
        if port < 8082:
            raise OSError("address in use")
        return SimpleNamespace(effective_port=port)

    monkeypatch.setattr(desktop, "create_server", fake_create_server)
    server = desktop.create_server_with_fallback(
        None, ports=range(8080, 8084), threads=4)

    assert server.effective_port == 8082
    assert [port for _host, port, _threads in attempts] == [8080, 8081, 8082]


def test_server_fallback_reports_exhausted_ports(monkeypatch):
    def fake_create_server(app, *, host, port, threads):
        raise OSError("address in use")

    monkeypatch.setattr(desktop, "create_server", fake_create_server)
    with pytest.raises(desktop.StartupError, match="8080 to 8081"):
        desktop.create_server_with_fallback(None, ports=range(8080, 8082))


def test_frozen_app_prefers_bundled_ffmpeg(monkeypatch, tmp_path):
    exe = tmp_path / "karaoke-app.exe"
    exe.write_bytes(b"fake")
    bundled = tmp_path / "ffmpeg" / "ffmpeg.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"fake ffmpeg")

    monkeypatch.delenv("KARAOKE_FFMPEG", raising=False)
    monkeypatch.setattr(transposition.sys, "frozen", True, raising=False)
    monkeypatch.setattr(transposition.sys, "executable", str(exe))

    assert transposition.ffmpeg_executable() == str(bundled)


def test_explicit_ffmpeg_path_wins(monkeypatch):
    monkeypatch.setenv("KARAOKE_FFMPEG", "/custom/ffmpeg")
    assert transposition.ffmpeg_executable() == "/custom/ffmpeg"


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_single_instance_detects_second_launch():
    name = f"Local\\KaraokeApp-Test-{uuid.uuid4()}"
    first = desktop.SingleInstance(name)
    second = None
    try:
        assert first.already_running is False
        second = desktop.SingleInstance(name)
        assert second.already_running is True
    finally:
        if second is not None:
            second.close()
        first.close()


def test_sse_shutdown_wakes_stream(client, app):
    response = client.get(
        "/api/events?role=remote&client_id=phone", buffered=False)
    frames = response.response
    assert next(frames).decode() == "retry: 3000\n\n"
    assert next(frames).decode().startswith("event: state\n")

    app.extensions["shutdown_event"].set()
    app.extensions["playback"].close_subscribers()
    with pytest.raises(StopIteration):
        next(frames)
    response.close()
