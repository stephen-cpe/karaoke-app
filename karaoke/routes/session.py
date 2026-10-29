"""Shared-session API: state, SSE, controls, and host lifecycle."""
from __future__ import annotations

import queue as thread_queue

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from karaoke.catalog import enrich_song, find_song
from karaoke.config import MAX_CLIENT_ID_LENGTH, SSE_KEEPALIVE_SECONDS
from karaoke.http import (
    base_url,
    client_id_from,
    is_local_request,
    json_body,
    local_ip,
    request_port,
)
from playback_state import format_sse

session = Blueprint("session", __name__)


def _state():
    return current_app.extensions["playback"]


@session.get("/api/state")
def api_state():
    state = _state()
    state.refresh_presence()
    return jsonify({"state": state.snapshot()})


@session.get("/api/events")
def api_events():
    role = request.args.get("role", "remote")
    if role not in {"host", "remote"}:
        role = "remote"
    client_id = (request.args.get("client_id") or "").strip()[:MAX_CLIENT_ID_LENGTH]
    state = _state()
    subscriber, snapshot = state.subscribe_with_snapshot(role, client_id)

    def generate():
        try:
            yield "retry: 3000\n\n"
            yield format_sse("state", snapshot)
            while True:
                try:
                    event, data = subscriber.events.get(timeout=SSE_KEEPALIVE_SECONDS)
                except thread_queue.Empty:
                    state.refresh_presence()
                    yield ": keep-alive\n\n"
                    continue
                yield format_sse(event, data)
        finally:
            state.unsubscribe(subscriber)

    response = Response(stream_with_context(generate()),
                        mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-store"
    response.headers["X-Accel-Buffering"] = "no"
    response.call_on_close(lambda: state.unsubscribe(subscriber))
    return response


@session.post("/api/control")
def api_control():
    data = json_body()
    action = data.get("action")
    state = _state()
    state.refresh_presence()
    extra = {}

    if action == "add":
        song = find_song(current_app.config["SONGS_DIR"], data.get("filename"))
        if song is None:
            return jsonify({"error": "Song not found."}), 404
        result = state.add_song(
            enrich_song(song, current_app.config["LYRICS_DIR"]))
        snapshot = result["snapshot"]
        extra["started"] = result["started"]
    elif action == "remove":
        snapshot = state.remove_queue(data.get("index"))
    elif action == "clear":
        snapshot = state.clear_queue()
    elif action == "reorder":
        snapshot = state.reorder_queue(data.get("from"), data.get("to"))
    elif action == "skip":
        snapshot = state.skip_remote()
    elif action == "play":
        snapshot = state.play()
    elif action == "pause":
        snapshot = state.pause()
    elif action == "restart":
        snapshot = state.restart()
    elif action == "seek":
        snapshot = state.seek(data.get("position"))
    elif action == "transpose":
        snapshot = state.set_transpose(
            data.get("semitones", data.get("value")))
    elif action == "volume":
        snapshot = state.set_volume(data.get("volume"))
    elif action == "stop":
        snapshot = state.stop()
    else:
        return jsonify({"error": "Unknown control action."}), 400

    return jsonify({"state": snapshot, **extra})


@session.get("/api/info")
def api_info():
    port = request_port()
    lan_ip = local_ip()
    local = is_local_request()
    state = _state()
    state.refresh_presence()
    snapshot = state.snapshot()
    return jsonify({
        "role": "host" if local else "remote",
        "server_id": snapshot["server_id"],
        "host_connected": snapshot["host_connected"],
        "join_url": base_url(lan_ip, port),
        "remote_url": base_url(lan_ip, port) + "/remote",
        "player_url": base_url("localhost", port) + "/player",
        "lan_ip": lan_ip,
        "port": port,
    })


@session.post("/api/host/register")
def api_host_register():
    data = json_body()
    state = _state()
    return jsonify(state.register_host_with_commands(client_id_from(data)))


@session.post("/api/host/report")
def api_host_report():
    data = json_body()
    state = _state()
    client_id = client_id_from(data)
    report = {key: value for key, value in data.items() if key != "client_id"}
    return jsonify(state.report_host_with_commands(client_id, report))


@session.get("/api/host/commands")
def api_host_commands():
    state = _state()
    client_id = client_id_from(request.args.to_dict())
    return jsonify(state.commands_payload(client_id))


@session.post("/api/host/ended")
def api_host_ended():
    data = json_body()
    snapshot = _state().host_ended(
        client_id_from(data),
        data.get("last_command_id"),
        data.get("filename"),
    )
    return jsonify({"state": snapshot})


@session.post("/api/host/error")
def api_host_error():
    data = json_body()
    snapshot = _state().host_error(
        client_id_from(data),
        data.get("message"),
        data.get("last_command_id"),
        data.get("filename"),
    )
    return jsonify({"state": snapshot})


@session.post("/api/host/disconnect")
def api_host_disconnect():
    data = json_body()
    return jsonify({"state": _state().disconnect_host(client_id_from(data))})
