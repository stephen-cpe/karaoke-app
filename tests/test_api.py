"""Tests for the pages, the song catalog endpoint, media routes, and lyrics saving."""
import json
import os

import pytest

from app import create_app


def test_index_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"KARAOKE" in res.data


def test_sync_editor_page_renders(client):
    res = client.get("/sync_editor.html")
    assert res.status_code == 200


def test_songs_endpoint_returns_scanned_catalog(client, add_song):
    add_song("00003_Bring Me To Life_Evanescence.mp3")
    add_song("00001_Two Steps Behind_Def Leppard.wav")
    add_song("cover.jpg")  # album art must be ignored

    res = client.get("/api/songs")
    assert res.status_code == 200

    catalog = res.get_json()["songs"]
    assert [song["title"] for song in catalog] == ["Bring Me To Life", "Two Steps Behind"]
    assert catalog[0]["artist"] == "Evanescence"
    assert catalog[0]["filename"] == "00003_Bring Me To Life_Evanescence.mp3"


def test_songs_endpoint_flags_existing_lyrics(client, add_song, add_lyrics):
    add_song("00001_Two Steps Behind_Def Leppard.mp3")
    add_song("00002_Bohemian Rhapsody_Queen.mp3")
    add_lyrics("00001_Two Steps Behind_Def Leppard.srt")

    catalog = client.get("/api/songs").get_json()["songs"]
    flags = {song["filename"]: song["lyrics"] for song in catalog}
    assert flags["00001_Two Steps Behind_Def Leppard.mp3"] is True
    assert flags["00002_Bohemian Rhapsody_Queen.mp3"] is False


def test_songs_endpoint_empty_when_no_songs(client):
    assert client.get("/api/songs").get_json() == {"songs": []}


# --- media routes -----------------------------------------------------------


def test_media_routes_only_serve_expected_file_types(client, app, add_song, add_lyrics):
    song = add_song("1_a_b.mp3")
    add_lyrics("1_a_b.srt")
    (app.config["LYRICS_DIR"] / "notes.txt").write_text("not lyrics", encoding="utf-8")
    (app.config["SONGS_DIR"] / "helper.py").write_text("print('hi')", encoding="utf-8")
    (app.config["TRANSPOSED_DIR"] / "+1_1_a_b.mp3").write_bytes(b"cached")
    (app.config["TRANSPOSED_DIR"] / "notes.txt").write_text("not audio", encoding="utf-8")

    assert client.get(f"/songs/{song}").status_code == 200
    assert client.get("/songs/helper.py").status_code == 404
    assert client.get("/lyrics/1_a_b.srt").status_code == 200
    assert client.get("/lyrics/notes.txt").status_code == 404
    assert client.get("/transposed/+1_1_a_b.mp3").status_code == 200
    assert client.get("/transposed/notes.txt").status_code == 404


def test_pages_use_local_vendor_assets_only(client):
    host = client.get("/").data
    editor = client.get("/sync_editor.html").data

    assert b"fonts.googleapis.com" not in host
    assert b"fonts.gstatic.com" not in host
    assert b"/static/vendor/bootstrap.min.css" in editor
    assert b"/static/vendor/wavesurfer.min.js" in editor
    assert b"cdn.jsdelivr.net" not in editor
    assert b"unpkg.com" not in editor

    for asset in (
        "/static/vendor/bootstrap.min.css",
        "/static/vendor/wavesurfer.min.js",
        "/static/vendor/bootstrap.LICENSE.txt",
        "/static/vendor/wavesurfer.LICENSE.txt",
    ):
        res = client.get(asset)
        assert res.status_code == 200, asset
        assert res.data, asset


def test_all_pages_ship_the_shared_confirmation_dialog(client):
    for page in ("/", "/remote", "/sync_editor.html"):
        data = client.get(page).data
        assert b"/static/confirm-dialog.css" in data, page
        assert b"/static/confirm-dialog.js" in data, page

    for asset in ("/static/confirm-dialog.css", "/static/confirm-dialog.js"):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert response.data, asset


def test_host_page_uses_generated_background(client):
    host = client.get("/").data

    assert b'id="background-canvas"' in host
    assert b"/static/background.js" in host
    assert client.get("/static/background.js").status_code == 200


# --- /api/save-lyrics -------------------------------------------------------

SRT = "1\n00:00:01,000 --> 00:00:03,500\nSomewhere in between\n\n"


def test_save_lyrics_writes_matching_srt(client, app, add_song):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")

    res = client.post("/api/save-lyrics", json={"song_id": song, "lyrics": SRT})
    assert res.status_code == 200
    assert res.get_json()["saved"] == "00001_Two Steps Behind_Def Leppard.srt"

    path = os.path.join(app.config["LYRICS_DIR"], "00001_Two Steps Behind_Def Leppard.srt")
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == SRT


def test_save_lyrics_preserves_unicode(client, app, add_song):
    song = add_song("1_Café Noir_Amélie.mp3")
    srt = "1\n00:00:00,000 --> 00:00:02,000\nCafé ☕ —— déjà vu\n\n"

    res = client.post("/api/save-lyrics", json={"song_id": song, "lyrics": srt})
    assert res.status_code == 200

    path = os.path.join(app.config["LYRICS_DIR"], "1_Café Noir_Amélie.srt")
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == srt


def test_save_lyrics_removes_stale_word_timings(client, app, add_song, add_lyrics):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    add_lyrics("00001_Two Steps Behind_Def Leppard.words.json", "{}")

    res = client.post("/api/save-lyrics", json={"song_id": song, "lyrics": SRT})
    assert res.status_code == 200
    assert not os.path.exists(os.path.join(
        app.config["LYRICS_DIR"], "00001_Two Steps Behind_Def Leppard.words.json"))


def test_save_lyrics_creates_missing_lyrics_directory(tmp_path):
    songs = tmp_path / "songs"
    songs.mkdir()
    (songs / "1_a_b.mp3").write_bytes(b"x")
    lyrics = tmp_path / "new" / "lyrics"  # does not exist yet

    app = create_app(songs_dir=songs, lyrics_dir=lyrics,
                     transposed_dir=tmp_path / "transposed")
    res = app.test_client().post(
        "/api/save-lyrics", json={"song_id": "1_a_b.mp3", "lyrics": SRT})

    assert res.status_code == 200
    assert (lyrics / "1_a_b.srt").exists()


def test_save_lyrics_unknown_song_returns_404(client, add_song):
    add_song("1_a_b.mp3")
    res = client.post("/api/save-lyrics", json={"song_id": "nope.mp3", "lyrics": SRT})
    assert res.status_code == 404


@pytest.mark.parametrize("bad_song_id", ["../evil.mp3", "..\\evil.mp3", "songs/evil.mp3"])
def test_save_lyrics_rejects_traversal(client, bad_song_id):
    res = client.post("/api/save-lyrics", json={"song_id": bad_song_id, "lyrics": SRT})
    assert res.status_code == 404


def test_save_lyrics_rejects_bad_payloads(client):
    assert client.post("/api/save-lyrics", json={}).status_code == 400
    assert client.post("/api/save-lyrics", json={"song_id": "a.mp3"}).status_code == 400
    assert client.post("/api/save-lyrics",
                       json={"song_id": "a.mp3", "lyrics": "   "}).status_code == 400
    assert client.post("/api/save-lyrics",
                       data="not json", content_type="text/plain").status_code == 400


def test_save_lyrics_rejects_non_string_song_id(client):
    for bad in [123, None, ["a.mp3"], {"filename": "a.mp3"}]:
        res = client.post("/api/save-lyrics", json={"song_id": bad, "lyrics": SRT})
        assert res.status_code == 400, bad


def test_save_lyrics_rejects_nul_byte_in_song_id(client):
    res = client.post("/api/save-lyrics",
                      json={"song_id": "a\x00b.mp3", "lyrics": SRT})
    assert res.status_code == 404


def test_save_lyrics_rejects_oversized_payload(client):
    res = client.post("/api/save-lyrics",
                      json={"song_id": "a.mp3", "lyrics": "x" * 1_000_001})
    assert res.status_code == 400


WORDS = {'version': 1, 'song': 'ignored', 'duration': 3.5, 'generated_by': 'test', 'lines': [{'start': 1.0, 'end': 3.5, 'text': 'Somewhere in between', 'instrumental': False, 'words': [{'text': 'Somewhere', 'start': 1.0, 'end': 2.0}, {'text': 'in', 'start': 2.0, 'end': 2.5}, {'text': 'between', 'start': 2.5, 'end': 3.5}]}]}


# --- /api/songs word-timing flags -----------------------------------------


def test_songs_endpoint_flags_existing_word_timings(client, add_song, add_lyrics):
    add_song("00001_Two Steps Behind_Def Leppard.mp3")
    add_song("00002_Bohemian Rhapsody_Queen.mp3")
    add_lyrics("00001_Two Steps Behind_Def Leppard.srt")
    add_lyrics("00001_Two Steps Behind_Def Leppard.words.json", json.dumps(WORDS))

    catalog = client.get("/api/songs").get_json()["songs"]
    flags = {song["filename"]: (song["lyrics"], song["words"]) for song in catalog}
    assert flags["00001_Two Steps Behind_Def Leppard.mp3"] == (True, True)
    assert flags["00002_Bohemian Rhapsody_Queen.mp3"] == (False, False)


# --- /api/save-lyrics word timings ------------------------------------------


def test_save_lyrics_writes_word_timings(client, app, add_song):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    res = client.post("/api/save-lyrics", json={
        "song_id": song, "lyrics": SRT, "words": WORDS})
    assert res.status_code == 200
    body = res.get_json()
    assert body["saved"] == "00001_Two Steps Behind_Def Leppard.srt"
    assert body["words"] == "00001_Two Steps Behind_Def Leppard.words.json"

    srt_path = os.path.join(app.config["LYRICS_DIR"], "00001_Two Steps Behind_Def Leppard.srt")
    words_path = os.path.join(app.config["LYRICS_DIR"], "00001_Two Steps Behind_Def Leppard.words.json")
    assert os.path.isfile(srt_path)
    assert os.path.isfile(words_path)
    with open(words_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["song"] == "00001_Two Steps Behind_Def Leppard"
    assert saved["lines"][0]["words"][2]["text"] == "between"


def test_save_lyrics_without_words_removes_stale_word_timings(client, app, add_song, add_lyrics):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    add_lyrics("00001_Two Steps Behind_Def Leppard.words.json", "{}")
    res = client.post("/api/save-lyrics", json={"song_id": song, "lyrics": SRT})
    assert res.status_code == 200
    assert res.get_json()["words"] is None
    assert not os.path.exists(os.path.join(
        app.config["LYRICS_DIR"], "00001_Two Steps Behind_Def Leppard.words.json"))


def test_save_lyrics_rejects_invalid_word_timings(client, app, add_song):
    song = add_song("1_a_b.mp3")
    srt_path = os.path.join(app.config["LYRICS_DIR"], "1_a_b.srt")
    with open(srt_path, "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:00,000 --> 00:00:01,000\nHello\n")

    invalid_cases = [
        {"lines": []},
        {"lines": [{"start": 1, "end": 1, "text": "x", "instrumental": False, "words": [{"text": "x", "start": 1, "end": 1}]}]},
        {"lines": [{"start": 0, "end": 2, "text": "[Instrumental]", "instrumental": True, "words": [{"text": "x", "start": 0, "end": 1}]}]},
        {"lines": [{"start": 0, "end": 2, "text": "hello", "instrumental": False, "words": [{"text": "goodbye", "start": 0, "end": 1}]}]},
        {"lines": [{"start": 0, "end": 2, "text": "hello", "instrumental": False, "words": [{"text": "hello", "start": 2.5, "end": 3}]}]},
        {"lines": [{"start": 10**400, "end": 2, "text": "hello", "instrumental": False, "words": []}]},
        {"lines": WORDS["lines"] + [{"start": 0, "end": 2, "text": "hello", "instrumental": False, "words": [{"text": "hello", "start": 0, "end": 2}]}]},
    ]
    for case in invalid_cases:
        res = client.post("/api/save-lyrics", json={"song_id": song, "lyrics": SRT, "words": case})
        assert res.status_code == 400, (case, res.get_json())
    assert (open(srt_path, encoding="utf-8").read()
            == "1\n00:00:00,000 --> 00:00:01,000\nHello\n")


def test_save_lyrics_rejects_oversized_word_timings(client, app, add_song, monkeypatch):
    monkeypatch.setitem(app.config, "MAX_WORDS_BYTES", 64)
    song = add_song("1_a_b.mp3")
    res = client.post("/api/save-lyrics", json={
        "song_id": song, "lyrics": SRT, "words": WORDS})
    assert res.status_code == 400


def test_save_lyrics_rejects_oversized_payload_appended(client, app, add_song):
    song = add_song("1_a_b.mp3")
    oversized = "x" * 1_000_001
    res = client.post("/api/save-lyrics", json={"song_id": song, "lyrics": oversized})
    assert res.status_code == 400

def test_save_lyrics_rejects_negative_and_non_boolean_word_metadata(client, add_song):
    song = add_song("1_a_b.mp3")
    cases = [
        {"lines": [{"start": -1, "end": 2, "text": "hello", "instrumental": False,
                    "words": [{"text": "hello", "start": 0, "end": 1}]}]},
        {"lines": [{"start": 0, "end": 2, "text": "[Instrumental]",
                    "instrumental": "false", "words": []}]},
        {"lines": [{"start": 0, "end": 2, "text": "hello", "instrumental": False,
                    "words": [{"text": "hello", "start": 0, "end": 1}]}],
         "duration": float("nan")},
    ]
    for case in cases:
        res = client.post("/api/save-lyrics",
                          json={"song_id": song, "lyrics": SRT, "words": case})
        assert res.status_code == 400, (case, res.get_json())


def test_oversized_request_body_returns_json_413(client):
    res = client.post("/api/save-lyrics", data=b"x" * 8_000_001,
                      content_type="application/json")
    assert res.status_code == 413
    assert res.is_json
    assert "too large" in res.get_json()["error"].lower()

def test_pages_avoid_inline_javascript(client):
    host = client.get("/").data
    editor = client.get("/sync_editor.html").data
    remote = client.get("/remote").data

    assert b"onclick=" not in host
    assert b"<script defer src=\"/static/sync_editor.js\"></script>" in editor
    assert b"<script>" not in editor
    assert b"onclick=" not in remote

def test_relative_media_directories_resolve_from_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "songs").mkdir()
    (tmp_path / "songs" / "1_a_b.mp3").write_bytes(b"fake-audio")

    app = create_app(songs_dir="songs", lyrics_dir="lyrics",
                     transposed_dir="transposed")
    response = app.test_client().get("/songs/1_a_b.mp3")

    assert response.status_code == 200
    assert response.data == b"fake-audio"
    assert app.config["SONGS_DIR"] == (tmp_path / "songs").resolve()
