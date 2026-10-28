"""Unit tests for the shared playback session model."""
import json

import pytest

import playback_state as playback_module
from playback_state import PlaybackError, PlaybackState, format_sse


def make_song(name, title=None):
    return {
        "id": name.split("_", 1)[0],
        "title": title or name,
        "artist": "Test Artist",
        "filename": name + ".mp3",
        "lyrics": False,
        "words": False,
    }


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(playback_module.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def state(clock):
    return PlaybackState()


def test_initial_snapshot(state):
    snapshot = state.snapshot()
    assert snapshot["status"] == "idle"
    assert snapshot["current"] is None
    assert snapshot["queue"] == []
    assert snapshot["host_connected"] is False
    assert snapshot["revision"] == 0
    assert snapshot["server_id"]


def test_add_song_queues_without_a_host(state):
    result = state.add_song(make_song("00001_First"))

    assert result["started"] is False
    snapshot = state.snapshot()
    assert snapshot["current"] is None
    assert [song["filename"] for song in snapshot["queue"]] == ["00001_First.mp3"]
    assert snapshot["status"] == "idle"
    assert state.pending_commands() == []


def test_register_host_starts_first_queued_song(state):
    state.add_song(make_song("00001_First"))
    state.add_song(make_song("00002_Second"))

    snapshot = state.register_host("tv")

    assert snapshot["host_connected"] is True
    assert snapshot["current"]["filename"] == "00001_First.mp3"
    assert [song["filename"] for song in snapshot["queue"]] == ["00002_Second.mp3"]
    assert snapshot["status"] == "loading"
    # The host loads the returned snapshot directly; no legacy command replay.
    assert state.pending_commands() == []


def test_add_song_with_connected_host_sends_load(state):
    state.register_host("tv")
    result = state.add_song(make_song("00001_First"))

    assert result["started"] is True
    commands = state.pending_commands()
    assert len(commands) == 1
    assert commands[0]["action"] == "load"
    assert commands[0]["song"]["filename"] == "00001_First.mp3"
    assert commands[0]["autoplay"] is True


@pytest.mark.parametrize("action", ["play", "pause", "skip_remote", "restart"])
def test_transport_requires_a_connected_host(state, action):
    state.add_song(make_song("00001_First"))

    with pytest.raises(PlaybackError) as error:
        getattr(state, action)()
    assert error.value.status_code == 409
    assert "offline" in error.value.message.lower()


def test_host_report_updates_playback_state(state):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))
    state.report_host("tv", {
        "status": "playing",
        "position": 3.25,
        "duration": 120,
        "transpose": 2,
        "volume": 0.4,
    })

    snapshot = state.snapshot()
    assert snapshot["status"] == "playing"
    assert snapshot["position"] == pytest.approx(3.25)
    assert snapshot["duration"] == 120
    assert snapshot["transpose"] == 2
    assert snapshot["volume"] == pytest.approx(0.4)


def test_huge_numbers_are_rejected_without_overflow(state):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))

    with pytest.raises(PlaybackError) as error:
        state.report_host("tv", {"position": 10**400})
    assert error.value.status_code == 400

    with pytest.raises(PlaybackError) as error:
        state.report_host("tv", {"volume": 10**400})
    assert error.value.status_code == 400

    with pytest.raises(PlaybackError) as error:
        state.seek(10**400)
    assert error.value.status_code == 400

    with pytest.raises(PlaybackError) as error:
        state.set_volume(10**400)
    assert error.value.status_code == 400


def test_pause_freezes_projected_position(state, clock):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))
    state.report_host("tv", {"status": "playing", "position": 5, "duration": 100})

    clock[0] += 3
    assert state.snapshot()["position"] == pytest.approx(8.0)

    paused = state.pause()
    assert paused["status"] == "paused"
    assert paused["position"] == pytest.approx(8.0)

    clock[0] += 10
    assert state.snapshot()["position"] == pytest.approx(8.0)
    assert [command["action"] for command in state.pending_commands()] == ["load", "pause"]


def test_acknowledgement_retains_newer_commands(state):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))
    state.pause()
    state.play()

    commands = state.pending_commands()
    assert [command["id"] for command in commands] == [1, 2, 3]

    snapshot = state.report_host("tv", {"status": "paused", "last_command_id": 2})
    assert snapshot["last_command_id"] == 2
    assert [command["id"] for command in state.pending_commands()] == [3]

    # A client cannot acknowledge commands the server never sent.
    state.report_host("tv", {"last_command_id": 999})
    assert state.pending_commands() == []
    assert state.snapshot()["last_command_id"] == 3


def test_stale_ended_event_does_not_skip_twice(state):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))
    state.add_song(make_song("00002_Second"))

    snapshot = state.host_ended("tv", filename="00002_Second.mp3")
    assert snapshot["current"]["filename"] == "00001_First.mp3"

    snapshot = state.host_ended("tv", filename="00001_First.mp3")
    assert snapshot["current"]["filename"] == "00002_Second.mp3"
    assert snapshot["queue"] == []


def test_remote_skip_advances_and_tells_host_to_load(state):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))
    state.add_song(make_song("00002_Second"))

    snapshot = state.skip_remote()

    assert snapshot["current"]["filename"] == "00002_Second.mp3"
    assert snapshot["status"] == "loading"
    last_command = state.pending_commands()[-1]
    assert last_command["action"] == "load"
    assert last_command["song"]["filename"] == "00002_Second.mp3"


def test_queue_reorder_remove_and_clear(state):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))
    state.add_song(make_song("00002_Second"))
    state.add_song(make_song("00003_Third"))

    state.reorder_queue(0, 1)
    assert [song["filename"] for song in state.snapshot()["queue"]] == [
        "00003_Third.mp3", "00002_Second.mp3",
    ]

    state.remove_queue(0)
    assert [song["filename"] for song in state.snapshot()["queue"]] == ["00002_Second.mp3"]

    state.clear_queue()
    assert state.snapshot()["queue"] == []

    with pytest.raises(PlaybackError) as error:
        state.remove_queue(0)
    assert error.value.status_code == 404

    with pytest.raises(PlaybackError) as error:
        state.reorder_queue(0, 1)
    assert error.value.status_code == 404


def test_stop_clears_current_and_queue(state):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))
    state.add_song(make_song("00002_Second"))

    snapshot = state.stop()

    assert snapshot["status"] == "idle"
    assert snapshot["current"] is None
    assert snapshot["queue"] == []
    assert snapshot["position"] == 0
    assert state.pending_commands()[-1]["action"] == "stop"


def test_refresh_presence_freezes_at_timeout_boundary(state, clock):
    state.register_host("tv")
    state.add_song(make_song("00001_First"))
    state.report_host("tv", {"status": "playing", "position": 3, "duration": 100})

    # HOST_TIMEOUT_SECONDS is 8; the host can only be trusted until t=1008.
    clock[0] += 9
    assert state.snapshot()["host_connected"] is False

    state.refresh_presence()
    snapshot = state.snapshot()
    assert snapshot["status"] == "paused"
    assert snapshot["position"] == pytest.approx(11.0)

    revision = snapshot["revision"]
    clock[0] += 20
    state.refresh_presence()
    assert state.snapshot()["revision"] == revision
    assert state.snapshot()["position"] == pytest.approx(11.0)


def test_second_host_takes_over_and_old_host_loses_access(state):
    state.register_host("tv-one")
    subscriber = state.subscribe("host", "tv-one")

    state.register_host("tv-two")

    event, data = subscriber.events.get_nowait()
    assert event == "host_revoked"
    assert "took over" in data["reason"].lower()

    with pytest.raises(PlaybackError) as error:
        state.report_host("tv-one", {"status": "playing"})
    assert error.value.status_code == 409

    snapshot = state.report_host("tv-two", {"status": "paused"})
    assert snapshot["host_connected"] is True

    with pytest.raises(PlaybackError):
        state.commands_for_host("tv-one")


def test_sse_frame_encoding():
    frame = format_sse("state", {"song": "Café", "position": 1})
    assert frame.startswith("event: state\ndata: ")
    assert frame.endswith("\n\n")
    payload = frame.split("data: ", 1)[1].strip()
    assert json.loads(payload) == {"song": "Café", "position": 1}
