"""Fixtures for browser-level Playwright tests.

The suite runs a real threaded Flask server against temporary app data, so the
tests exercise the same HTML, JavaScript, SSE, and control API as the app. It
skips cleanly when Playwright or a usable Chromium-based browser is missing.
"""
from __future__ import annotations

import io
import threading
import wave
from dataclasses import dataclass

import pytest
from werkzeug.serving import make_server


@dataclass(frozen=True)
class LiveServer:
    base_url: str
    songs: tuple[str, ...]

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def _silent_wav(seconds: float = 20.0, sample_rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buffer.getvalue()


@pytest.fixture
def live_server(app, add_song):
    """Run the Flask app on a real localhost HTTP server."""
    songs = (
        "00001_Test One_Artist A.wav",
        "00002_Test Two_Artist B.wav",
        "00003_Test Three_Artist C.wav",
    )
    for filename in songs:
        add_song(filename, _silent_wav())

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(
        target=server.serve_forever,
        name="karaoke-browser-test-server",
        daemon=True,
    )
    thread.start()
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{server.server_port}",
            songs=songs,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip(
        "playwright.sync_api", reason="Playwright is not installed")
    with sync_api.sync_playwright() as playwright:
        browser_instance = None
        errors = []
        for options in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                browser_instance = playwright.chromium.launch(
                    headless=True, **options)
                break
            except Exception as exc:  # noqa: BLE001 - report the actual launch failures
                errors.append(f"{options or 'bundled chromium'}: {exc}")

        if browser_instance is None:
            pytest.skip(
                "No usable Chromium-based browser for Playwright. "
                "Run `python -m playwright install chromium` or install Edge/Chrome. "
                + " | ".join(errors)
            )

        yield browser_instance
        browser_instance.close()


@pytest.fixture
def browser_context(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        yield context
    finally:
        context.close()
