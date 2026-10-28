import os

import pytest

from app import create_app


@pytest.fixture
def app(tmp_path):
    """A Flask app wired to throwaway directories (nothing touches the real songs)."""
    dirs = {
        "songs_dir": tmp_path / "songs",
        "lyrics_dir": tmp_path / "lyrics",
        "transposed_dir": tmp_path / "transposed",
        "background_dir": tmp_path / "background",
    }
    for directory in dirs.values():
        directory.mkdir()
    return create_app(**dirs)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def add_song(app):
    """Create a fake audio file inside the app's songs directory."""

    def _add(filename, content=b"fake-audio-bytes"):
        with open(os.path.join(app.config["SONGS_DIR"], filename), "wb") as fh:
            fh.write(content)
        return filename

    return _add


@pytest.fixture
def add_lyrics(app):
    """Create a fake .srt file inside the app's lyrics directory."""

    def _add(filename, content="1\n00:00:00,000 --> 00:00:01,000\nHello\n"):
        with open(os.path.join(app.config["LYRICS_DIR"], filename), "w", encoding="utf-8") as fh:
            fh.write(content)
        return filename

    return _add
