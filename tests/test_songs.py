"""Tests for song filename parsing and directory scanning."""

from app import parse_song_filename, scan_songs


def test_parse_full_filename():
    song = parse_song_filename("00001_Two Steps Behind_Def Leppard.mp3")
    assert song == {
        "id": "00001",
        "title": "Two Steps Behind",
        "artist": "Def Leppard",
        "filename": "00001_Two Steps Behind_Def Leppard.mp3",
    }


def test_parse_title_may_contain_underscores():
    song = parse_song_filename("00042_Under_Pressure_Queen.mp3")
    assert song["id"] == "00042"
    assert song["title"] == "Under_Pressure"
    assert song["artist"] == "Queen"


def test_parse_missing_artist():
    song = parse_song_filename("01001_Innocent.mp3")
    assert song["id"] == "01001"
    assert song["title"] == "Innocent"
    assert song["artist"] == ""


def test_parse_bare_title():
    song = parse_song_filename("mysong.mp3")
    assert song == {
        "id": "",
        "title": "mysong",
        "artist": "",
        "filename": "mysong.mp3",
    }


def test_scan_songs_filters_non_audio_and_sorts_case_insensitively(tmp_path):
    (tmp_path / "00002_Zebra_Zed.mp3").write_bytes(b"x")
    (tmp_path / "00001_alpha_Ant.wav").write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(b"x")
    (tmp_path / "normalize_audio.py").write_text("print('hi')")
    (tmp_path / "folder.mp3").mkdir()  # a directory is not a playable song

    titles = [song["title"] for song in scan_songs(tmp_path)]
    assert titles == ["alpha", "Zebra"]


def test_scan_songs_missing_directory(tmp_path):
    assert scan_songs(tmp_path / "does-not-exist") == []
