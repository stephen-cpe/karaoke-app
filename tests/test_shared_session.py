"""Tests for the shared-session pages and phone/host API."""
import json

import pytest

import playback_state as playback_module

LAN = {"REMOTE_ADDR": "192.168.1.25"}


def register_host(client, client_id="tv-client"):
    response = client.post("/api/host/register", json={"client_id": client_id})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def control(client, action, **payload):
    return client.post("/api/control", json={"action": action, **payload})


# --- Role-aware pages -------------------------------------------------------


def test_root_serves_host_player_on_localhost(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"/static/scripts.js" in response.data
    assert b"/static/remote.js" not in response.data
    assert b"sync_editor.html" in response.data


def test_root_serves_remote_for_lan_requests(client):
    response = client.get("/", environ_base=LAN)

    assert response.status_code == 200
    assert b"/static/remote.js" in response.data
    assert b"/static/scripts.js" not in response.data
    assert b"/static/remote.css" in response.data
    assert b"<audio" not in response.data.lower()
    assert b"<video" not in response.data.lower()
    assert b"sync_editor.html" not in response.data


@pytest.mark.parametrize("environ_base", [{}, LAN])
def test_explicit_player_and_remote_overrides(client, environ_base):
    player = client.get("/player", environ_base=environ_base)
    remote = client.get("/remote", environ_base=environ_base)

    assert b"/static/scripts.js" in player.data
    assert b"/static/remote.js" in remote.data


# --- Session state / control API -------------------------------------------


def test_info_endpoint_reports_role_and_urls(client):
    host_info = client.get("/api/info").get_json()
    remote_info = client.get("/api/info", environ_base=LAN).get_json()

    assert host_info["role"] == "host"
    assert remote_info["role"] == "remote"
    assert host_info["player_url"].endswith("/player")
    assert remote_info["remote_url"].endswith("/remote")
    assert host_info["server_id"] == remote_info["server_id"]
    assert "host_connected" in remote_info


def test_state_endpoint_initial_shape(client):
    response = client.get("/api/state")
    assert response.status_code == 200

    state = response.get_json()["state"]
    for key in ["server_id", "revision", "host_connected", "status", "position",
                "duration", "current", "queue", "transpose", "volume", "error"]:
        assert key in state
    assert state["current"] is None
    assert state["queue"] == []
    assert state["host_connected"] is False


def test_add_song_validates_catalog_and_fills_lyrics_flags(client, add_song, add_lyrics):
    filename = add_song("00001_Two Steps Behind_Def Leppard.mp3")
    add_lyrics("00001_Two Steps Behind_Def Leppard.srt")

    assert control(client, "add", filename="nope.mp3").status_code == 404
    assert control(client, "add", filename="../evil.mp3").status_code == 404
    assert control(client, "add", filename="songs/evil.mp3").status_code == 404

    response = control(client, "add", filename=filename)
    assert response.status_code == 200
    body = response.get_json()
    assert body["started"] is False
    queued = body["state"]["queue"][0]
    assert queued["lyrics"] is True
    assert queued["words"] is False


def test_transport_controls_are_rejected_without_host(client, add_song):
    add_song("00001_First.mp3")
    control(client, "add", filename="00001_First.mp3")

    for action in ["play", "pause", "restart", "skip"]:
        response = control(client, action)
        assert response.status_code == 409, (action, response.get_json())
        assert "offline" in response.get_json()["error"].lower()


def test_host_register_starts_queued_song(client, add_song):
    add_song("00001_First.mp3")
    add_song("00002_Second.mp3")
    control(client, "add", filename="00001_First.mp3")
    control(client, "add", filename="00002_Second.mp3")

    body = register_host(client)

    assert body["commands"] == []
    assert body["state"]["host_connected"] is True
    assert body["state"]["current"]["filename"] == "00001_First.mp3"
    assert [song["filename"] for song in body["state"]["queue"]] == ["00002_Second.mp3"]
    assert body["state"]["status"] == "loading"


def test_add_song_with_connected_host_starts_immediately(client, add_song):
    add_song("00001_First.mp3")
    add_song("00002_Second.mp3")
    register_host(client)

    first = control(client, "add", filename="00001_First.mp3").get_json()
    second = control(client, "add", filename="00002_Second.mp3").get_json()

    assert first["started"] is True
    assert first["state"]["current"]["filename"] == "00001_First.mp3"
    assert second["started"] is False
    assert [song["filename"] for song in second["state"]["queue"]] == ["00002_Second.mp3"]


def test_host_report_and_remote_controls_flow(client, add_song):
    add_song("00001_First.mp3")
    add_song("00002_Second.mp3")
    control(client, "add", filename="00001_First.mp3")
    control(client, "add", filename="00002_Second.mp3")
    register_host(client)

    report = client.post("/api/host/report", json={
        "client_id": "tv-client",
        "status": "playing",
        "position": 5,
        "duration": 120,
    })
    assert report.status_code == 200
    assert report.get_json()["state"]["status"] == "playing"

    paused = control(client, "pause").get_json()["state"]
    assert paused["status"] == "paused"
    assert paused["position"] == pytest.approx(5, abs=1.5)

    seek = control(client, "seek", position=30).get_json()["state"]
    assert seek["position"] == pytest.approx(30)

    transposed = control(client, "transpose", semitones=2).get_json()["state"]
    assert transposed["transpose"] == 2

    quieter = control(client, "volume", volume=0.25).get_json()["state"]
    assert quieter["volume"] == pytest.approx(0.25)

    skipped = control(client, "skip").get_json()["state"]
    assert skipped["current"]["filename"] == "00002_Second.mp3"
    assert skipped["queue"] == []
    assert skipped["status"] == "loading"

    restarted = control(client, "restart").get_json()["state"]
    assert restarted["status"] == "playing"
    assert restarted["position"] == 0

    stopped = control(client, "stop").get_json()["state"]
    assert stopped["status"] == "idle"
    assert stopped["current"] is None
    assert stopped["queue"] == []


def test_remote_queue_editing_does_not_need_a_host(client, add_song):
    add_song("00001_First.mp3")
    add_song("00002_Second.mp3")
    control(client, "add", filename="00001_First.mp3")
    control(client, "add", filename="00002_Second.mp3")

    # Invalid indexes are rejected rather than silently ignored.
    assert control(client, "reorder", index=0).status_code == 400

    reordered = client.post("/api/control",
                            json={"action": "reorder", "from": 0, "to": 1})
    assert reordered.status_code == 200
    assert [song["filename"] for song in reordered.get_json()["state"]["queue"]] == [
        "00002_Second.mp3", "00001_First.mp3",
    ]

    removed = control(client, "remove", index=0)
    assert removed.status_code == 200
    assert [song["filename"] for song in removed.get_json()["state"]["queue"]] == [
        "00001_First.mp3",
    ]

    cleared = control(client, "clear")
    assert cleared.status_code == 200
    assert cleared.get_json()["state"]["queue"] == []


def test_host_ended_advances_and_ignores_stale_filename(client, add_song):
    add_song("00001_First.mp3")
    add_song("00002_Second.mp3")
    control(client, "add", filename="00001_First.mp3")
    control(client, "add", filename="00002_Second.mp3")
    register_host(client)

    stale = client.post("/api/host/ended", json={
        "client_id": "tv-client",
        "filename": "00002_Second.mp3",
    })
    assert stale.status_code == 200
    assert stale.get_json()["state"]["current"]["filename"] == "00001_First.mp3"

    ended = client.post("/api/host/ended", json={
        "client_id": "tv-client",
        "filename": "00001_First.mp3",
    })
    assert ended.status_code == 200
    assert ended.get_json()["state"]["current"]["filename"] == "00002_Second.mp3"
    assert ended.get_json()["state"]["queue"] == []


def test_second_host_takes_over(client):
    register_host(client, "tv-one")
    register_host(client, "tv-two")

    old_report = client.post("/api/host/report", json={
        "client_id": "tv-one", "status": "playing",
    })
    new_report = client.post("/api/host/report", json={
        "client_id": "tv-two", "status": "paused",
    })
    old_commands = client.get("/api/host/commands?client_id=tv-one")

    assert old_report.status_code == 409
    assert new_report.status_code == 200
    assert old_commands.status_code == 409


def test_host_presence_expires_via_state_heartbeat(client, add_song, monkeypatch):
    add_song("00001_First.mp3")
    control(client, "add", filename="00001_First.mp3")
    register_host(client)
    session = client.application.extensions["playback"]
    assert session.snapshot()["host_connected"] is True

    # Force a clock clearly beyond the last report without relying on sleeps.
    monkeypatch.setattr(playback_module.time, "monotonic",
                        lambda: session._host_last_seen + session.HOST_TIMEOUT_SECONDS + 1)

    state = client.get("/api/state").get_json()["state"]
    assert state["host_connected"] is False
    assert state["status"] == "paused"


def test_sse_stream_starts_with_retry_and_state(client):
    response = client.get("/api/events?role=remote&client_id=phone", buffered=False)
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    # Hop-by-hop headers are rejected by waitress (PEP 3333).
    assert "Connection" not in response.headers

    try:
        frames = response.response
        assert next(frames).decode() == "retry: 3000\n\n"
        frame = next(frames).decode()
        assert frame.startswith("event: state\ndata: ")
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert "revision" in payload and "queue" in payload
    finally:
        response.close()


# --- PWA --------------------------------------------------------------------


def test_manifest_and_service_worker_routes(client):
    manifest_response = client.get("/manifest.webmanifest")
    worker_response = client.get("/sw.js")

    assert manifest_response.status_code == 200
    assert manifest_response.mimetype == "application/manifest+json"
    manifest = json.loads(manifest_response.data)
    assert manifest["start_url"] == "/remote"
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512"}

    assert worker_response.status_code == 200
    assert worker_response.headers["Service-Worker-Allowed"] == "/"
    assert worker_response.headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("filename", [
    "icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png",
])
def test_pwa_icons_exist(client, filename):
    response = client.get("/static/" + filename)
    assert response.status_code == 200
    assert response.data.startswith(b"\x89PNG")
