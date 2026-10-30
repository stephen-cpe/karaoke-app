"""Tests for the pages, the song catalog endpoint, media routes, and lyrics saving."""
import json
import os

import pytest

import karaoke.lyrics as lyrics_module
from app import create_app
from karaoke.lyrics import lyrics_revision


def save_lyrics_request(client, app, song_id, **payload):
    payload.setdefault(
        "base_revision", lyrics_revision(app.config["LYRICS_DIR"], song_id))
    return client.post("/api/save-lyrics", json={"song_id": song_id, **payload})


def editor_draft_info(client, song_id):
    return client.get(
        "/api/editor/draft", query_string={"song_id": song_id}).get_json()


def save_editor_draft(client, song_id, info, **payload):
    payload.setdefault("base_draft_revision", info["draft_revision"])
    payload.setdefault("base_active_revision", info["base_active_revision"])
    return client.post(
        "/api/editor/draft", json={"song_id": song_id, **payload})


def approve_editor_draft(client, song_id, info, **payload):
    payload.setdefault("draft_revision", info["draft_revision"])
    payload.setdefault("base_active_revision", info["base_active_revision"])
    return client.post(
        "/api/editor/approve", json={"song_id": song_id, **payload})


def test_index_page_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"KARAOKE" in res.data
    assert b'data-editor-available="1"' in res.data


def test_sync_editor_page_renders(client):
    res = client.get("/sync_editor.html")
    assert res.status_code == 200
    assert client.get("/lyrics-editor").status_code == 200
    assert b"Lyrics Editor" in res.data
    assert b"Drop all word timings" not in res.data


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

    res = save_lyrics_request(client, app, song, lyrics=SRT)
    assert res.status_code == 200
    assert res.get_json()["saved"] == "00001_Two Steps Behind_Def Leppard.srt"

    path = os.path.join(app.config["LYRICS_DIR"], "00001_Two Steps Behind_Def Leppard.srt")
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == SRT


def test_save_lyrics_preserves_unicode(client, app, add_song):
    song = add_song("1_Café Noir_Amélie.mp3")
    srt = "1\n00:00:00,000 --> 00:00:02,000\nCafé ☕ —— déjà vu\n\n"

    res = save_lyrics_request(client, app, song, lyrics=srt)
    assert res.status_code == 200

    path = os.path.join(app.config["LYRICS_DIR"], "1_Café Noir_Amélie.srt")
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == srt


def test_save_lyrics_removes_stale_word_timings(client, app, add_song, add_lyrics):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    add_lyrics("00001_Two Steps Behind_Def Leppard.words.json", "{}")

    res = save_lyrics_request(client, app, song, lyrics=SRT)
    assert res.status_code == 200
    assert not os.path.exists(os.path.join(
        app.config["LYRICS_DIR"], "00001_Two Steps Behind_Def Leppard.words.json"))


def test_save_lyrics_creates_missing_lyrics_directory(tmp_path):
    songs = tmp_path / "songs"
    songs.mkdir()
    (songs / "1_a_b.mp3").write_bytes(b"x")
    lyrics = tmp_path / "new" / "lyrics"  # does not exist yet

    app = create_app(songs_dir=songs, lyrics_dir=lyrics,
                     transposed_dir=tmp_path / "transposed",
                     enable_editor=True)
    res = save_lyrics_request(app.test_client(), app, "1_a_b.mp3", lyrics=SRT)

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
    res = save_lyrics_request(client, app, song, lyrics=SRT, words=WORDS)
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
    res = save_lyrics_request(client, app, song, lyrics=SRT)
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
    res = save_lyrics_request(client, app, song, lyrics=SRT, words=WORDS)
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
    assert b"<script defer src=\"/static/lyric_reconcile.js\"></script>" in editor
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


# --- Editor access and revision tracking ------------------------------------


LAN = {"REMOTE_ADDR": "192.168.1.25"}


def _editor_app(tmp_path, **kwargs):
    dirs = {
        "songs_dir": tmp_path / "songs",
        "lyrics_dir": tmp_path / "lyrics",
        "transposed_dir": tmp_path / "transposed",
    }
    for directory in dirs.values():
        directory.mkdir()
    return create_app(**dirs, **kwargs)


def test_editor_requires_localhost_and_enable_flag(client, add_song):
    song = add_song("1_a_b.mp3")

    assert client.get("/sync_editor.html", environ_base=LAN).status_code == 404
    assert client.get("/lyrics-editor", environ_base=LAN).status_code == 404
    lan_page = client.get("/player", environ_base=LAN).data
    assert b"sync_editor.html" not in lan_page
    assert b"data-editor-available" not in lan_page
    assert client.get(
        "/api/editor/lyrics",
        query_string={"song_id": song},
        environ_base=LAN,
    ).status_code == 404
    assert client.post(
        "/api/save-lyrics",
        json={"song_id": song, "lyrics": SRT, "base_revision": "stale"},
        environ_base=LAN,
    ).status_code == 404


def test_editor_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("KARAOKE_EDITOR", raising=False)
    app = _editor_app(tmp_path)
    editor_client = app.test_client()

    assert app.config["ENABLE_EDITOR"] is False
    assert editor_client.get("/sync_editor.html").status_code == 404
    assert editor_client.get("/lyrics-editor").status_code == 404
    assert editor_client.post("/api/save-lyrics", json={}).status_code == 404
    disabled_page = editor_client.get("/").data
    assert b"sync_editor.html" not in disabled_page
    assert b"data-editor-available" not in disabled_page
    assert editor_client.get(
        "/api/editor/lyrics", query_string={"song_id": "nope.mp3"}
    ).status_code == 404
    assert b"sync_editor.html" not in editor_client.get("/").data


def test_editor_env_flag_enables_localhost_page(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAOKE_EDITOR", "1")
    app = _editor_app(tmp_path)

    assert app.config["ENABLE_EDITOR"] is True
    assert app.test_client().get("/sync_editor.html").status_code == 200


def test_editor_lyrics_bundle_includes_revision(client, app, add_song, add_lyrics):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    stem = song.rsplit(".", 1)[0]
    add_lyrics(stem + ".srt", SRT)

    bundle = client.get(
        "/api/editor/lyrics", query_string={"song_id": song}).get_json()

    assert bundle["srt"].replace("\r\n", "\n") == SRT
    assert bundle["words"] is None
    assert bundle["revision"] == lyrics_revision(app.config["LYRICS_DIR"], song)


def test_save_words_only_derives_srt_and_roundtrips(
        client, app, add_song):
    song = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    revision = lyrics_revision(app.config["LYRICS_DIR"], song)

    res = client.post("/api/save-lyrics", json={
        "song_id": song,
        "words": WORDS,
        "base_revision": revision,
    })

    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["revision"] == lyrics_revision(app.config["LYRICS_DIR"], song)

    stem = song.rsplit(".", 1)[0]
    srt_path = app.config["LYRICS_DIR"] / f"{stem}.srt"
    srt_text = srt_path.read_text(encoding="utf-8")
    assert "Somewhere in between" in srt_text
    assert "00:00:01,000 --> 00:00:03,500" in srt_text

    bundle = client.get(
        "/api/editor/lyrics", query_string={"song_id": song}).get_json()
    assert bundle["revision"] == body["revision"]
    assert json.loads(bundle["words"])["lines"][0]["text"] == "Somewhere in between"


def test_line_level_words_save_derives_srt(client, app, add_song):
    song = add_song("1_a_b.mp3")
    revision = lyrics_revision(app.config["LYRICS_DIR"], song)
    words = {
        "lines": [{
            "start": 0.5,
            "end": 2.5,
            "text": "Line level only",
            "instrumental": False,
            "words": [],
        }],
    }

    res = client.post("/api/save-lyrics", json={
        "song_id": song,
        "words": words,
        "base_revision": revision,
    })

    assert res.status_code == 200, res.get_json()
    stem = song.rsplit(".", 1)[0]
    srt_text = (app.config["LYRICS_DIR"] / f"{stem}.srt").read_text(encoding="utf-8")
    assert "Line level only" in srt_text
    assert "00:00:00,500 --> 00:00:02,500" in srt_text


def test_save_lyrics_rejects_stale_revision(client, app, add_song, add_lyrics):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    stale_revision = lyrics_revision(app.config["LYRICS_DIR"], song)
    old_srt = "1\n00:00:00,000 --> 00:00:01,000\nOld line\n"
    add_lyrics(stem + ".srt", old_srt)

    res = client.post("/api/save-lyrics", json={
        "song_id": song,
        "lyrics": SRT,
        "base_revision": stale_revision,
    })

    assert res.status_code == 409
    assert res.get_json()["revision"] == lyrics_revision(
        app.config["LYRICS_DIR"], song)
    assert (app.config["LYRICS_DIR"] / f"{stem}.srt").read_text(
        encoding="utf-8") == old_srt


def test_save_lyrics_requires_base_revision(client, app, add_song):
    song = add_song("1_a_b.mp3")

    res = client.post("/api/save-lyrics", json={"song_id": song, "lyrics": SRT})

    assert res.status_code == 400
    assert "base_revision" in res.get_json()["error"]


# --- Draft and approval workflow -------------------------------------------


def test_draft_save_does_not_publish_or_appear_in_catalog(
        client, app, add_song):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    info = editor_draft_info(client, song)

    res = save_editor_draft(client, song, info, srt=SRT)

    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["draft_revision"]
    assert not (app.config["LYRICS_DIR"] / f"{stem}.srt").exists()
    assert not (app.config["LYRICS_DIR"] / f"{stem}.words.json").exists()

    catalog = client.get("/api/songs").get_json()["songs"]
    assert catalog[0]["lyrics"] is False
    assert catalog[0]["words"] is False
    assert client.get(f"/lyrics/{stem}.srt").status_code == 404
    assert client.get(
        f"/lyrics/drafts/{stem}.draft.json").status_code == 404
    assert client.get(f"/lyrics/drafts/{stem}.srt").status_code == 404

    loaded = editor_draft_info(client, song)
    assert loaded["source"] == "draft"
    assert loaded["draft_revision"] == body["draft_revision"]
    assert loaded["srt"].replace("\r\n", "\n") == SRT
    assert loaded["words"] is None


def test_approve_publishes_word_draft_and_clears_draft(
        client, app, add_song):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    info = editor_draft_info(client, song)
    draft_res = save_editor_draft(client, song, info, words=WORDS)
    assert draft_res.status_code == 200, draft_res.get_json()
    assert not (app.config["LYRICS_DIR"] / f"{stem}.srt").exists()
    assert not (app.config["LYRICS_DIR"] / f"{stem}.words.json").exists()
    draft = editor_draft_info(client, song)

    res = approve_editor_draft(client, song, draft)

    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["words"] == f"{stem}.words.json"
    assert body["revision"] == lyrics_revision(
        app.config["LYRICS_DIR"], song)
    assert (app.config["LYRICS_DIR"] / f"{stem}.srt").exists()
    assert (app.config["LYRICS_DIR"] / f"{stem}.words.json").exists()
    assert editor_draft_info(client, song)["draft_revision"] is None
    catalog = client.get("/api/songs").get_json()["songs"]
    assert catalog[0]["lyrics"] is True
    assert catalog[0]["words"] is True


def test_mixed_line_and_word_cues_publish(client, app, add_song):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    mixed = {
        "lines": [
            WORDS["lines"][0],
            {
                "start": 4.0,
                "end": 5.5,
                "text": "Line only",
                "instrumental": False,
                "words": [],
            },
        ],
    }
    info = editor_draft_info(client, song)
    draft_res = save_editor_draft(client, song, info, words=mixed)
    assert draft_res.status_code == 200, draft_res.get_json()
    draft = editor_draft_info(client, song)

    res = approve_editor_draft(client, song, draft)

    assert res.status_code == 200, res.get_json()
    words = json.loads(
        (app.config["LYRICS_DIR"] / f"{stem}.words.json").read_text(
            encoding="utf-8"))
    assert words["lines"][0]["words"]
    assert words["lines"][1]["words"] == []
    srt_text = (app.config["LYRICS_DIR"] / f"{stem}.srt").read_text(
        encoding="utf-8")
    assert "Somewhere in between" in srt_text
    assert "Line only" in srt_text


def test_draft_review_metadata_round_trips_and_stays_out_of_active_words(
        client, app, add_song):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    info = editor_draft_info(client, song)
    review = [{"line": 1, "reason": "words-added"}]

    draft_res = save_editor_draft(
        client, song, info, words=WORDS, review=review)
    assert draft_res.status_code == 200, draft_res.get_json()
    loaded = editor_draft_info(client, song)
    assert loaded["review"] == review

    draft = editor_draft_info(client, song)
    approved = approve_editor_draft(client, song, draft)
    assert approved.status_code == 200, approved.get_json()
    published = json.loads(
        (app.config["LYRICS_DIR"] / f"{stem}.words.json").read_text(
            encoding="utf-8"))
    assert "review" not in published
    assert all("review" not in line for line in published["lines"])


def test_invalid_draft_review_metadata_is_rejected(client, app, add_song):
    song = add_song("1_a_b.mp3")
    info = editor_draft_info(client, song)

    res = save_editor_draft(
        client, song, info, srt=SRT,
        review=[{"line": "one", "reason": "words-added"}])

    assert res.status_code == 400
    assert "review" in res.get_json()["error"].lower()
    assert editor_draft_info(client, song)["draft_revision"] is None


def test_srt_only_publish_removes_stale_active_words(
        client, app, add_song, add_lyrics):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    add_lyrics(stem + ".srt", SRT)
    add_lyrics(stem + ".words.json", json.dumps(WORDS))
    info = editor_draft_info(client, song)
    assert info["source"] == "active"
    draft_res = save_editor_draft(client, song, info, srt=SRT)
    assert draft_res.status_code == 200, draft_res.get_json()
    assert (app.config["LYRICS_DIR"] / f"{stem}.words.json").exists()
    draft = editor_draft_info(client, song)

    res = approve_editor_draft(client, song, draft)

    assert res.status_code == 200, res.get_json()
    assert res.get_json()["words"] is None
    assert (app.config["LYRICS_DIR"] / f"{stem}.srt").exists()
    assert not (app.config["LYRICS_DIR"] / f"{stem}.words.json").exists()


def test_draft_save_rejects_stale_draft_revision(client, app, add_song):
    song = add_song("1_a_b.mp3")
    info = editor_draft_info(client, song)
    first = save_editor_draft(client, song, info, srt=SRT)
    assert first.status_code == 200

    stale = save_editor_draft(client, song, info, srt=SRT)
    assert stale.status_code == 409
    assert stale.get_json()["draft_revision"]

    current = editor_draft_info(client, song)
    assert current["source"] == "draft"


def test_approval_rejects_changed_active_lyrics(client, app, add_song, add_lyrics):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    info = editor_draft_info(client, song)
    draft_res = save_editor_draft(client, song, info, srt=SRT)
    assert draft_res.status_code == 200
    draft = editor_draft_info(client, song)

    changed_active = "1\n00:00:00,000 --> 00:00:01,000\nNewer active line\n"
    add_lyrics(stem + ".srt", changed_active)

    res = approve_editor_draft(client, song, draft)

    assert res.status_code == 409
    assert "active lyrics changed" in res.get_json()["error"].lower()
    assert (app.config["LYRICS_DIR"] / f"{stem}.srt").read_text(
        encoding="utf-8") == changed_active
    assert editor_draft_info(client, song)["source"] == "draft"


def test_publish_failure_restores_active_and_keeps_draft(
        client, app, add_song, add_lyrics, monkeypatch):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    old_srt = "1\n00:00:00,000 --> 00:00:02,000\nOld active line\n"
    old_words = json.dumps(WORDS)
    add_lyrics(stem + ".srt", old_srt)
    add_lyrics(stem + ".words.json", old_words)
    info = editor_draft_info(client, song)
    draft_res = save_editor_draft(client, song, info, words=WORDS)
    assert draft_res.status_code == 200
    draft = editor_draft_info(client, song)

    real_replace = lyrics_module.os.replace
    calls = {"count": 0}

    def fail_second_replace(source, destination):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(lyrics_module.os, "replace", fail_second_replace)

    res = approve_editor_draft(client, song, draft)

    assert res.status_code == 500
    assert (app.config["LYRICS_DIR"] / f"{stem}.srt").read_text(
        encoding="utf-8") == old_srt
    assert (app.config["LYRICS_DIR"] / f"{stem}.words.json").read_text(
        encoding="utf-8") == old_words
    assert editor_draft_info(client, song)["source"] == "draft"


def test_invalid_srt_is_rejected_without_writes(
        client, app, add_song, add_lyrics):
    song = add_song("1_a_b.mp3")
    stem = song.rsplit(".", 1)[0]
    bad_srt = "this is not an srt"
    old_srt = "1\n00:00:00,000 --> 00:00:01,000\nOld active line\n"
    add_lyrics(stem + ".srt", old_srt)

    active_info = editor_draft_info(client, song)
    active = client.post("/api/save-lyrics", json={
        "song_id": song,
        "lyrics": bad_srt,
        "base_revision": active_info["active_revision"],
    })
    assert active.status_code == 400
    assert (app.config["LYRICS_DIR"] / f"{stem}.srt").read_text(
        encoding="utf-8") == old_srt

    draft_info = editor_draft_info(client, song)
    good_draft = save_editor_draft(client, song, draft_info, srt=SRT)
    assert good_draft.status_code == 200
    before = editor_draft_info(client, song)
    draft = save_editor_draft(client, song, before, srt=bad_srt)
    assert draft.status_code == 400
    after = editor_draft_info(client, song)
    assert after["srt"].replace("\r\n", "\n") == SRT
    assert after["draft_revision"] == before["draft_revision"]


def test_approval_requires_a_saved_draft(client, app, add_song):
    song = add_song("1_a_b.mp3")
    info = editor_draft_info(client, song)

    res = client.post("/api/editor/approve", json={
        "song_id": song,
        "draft_revision": "not-a-draft",
        "base_active_revision": info["active_revision"],
    })

    assert res.status_code == 404
