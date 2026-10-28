"""Shared karaoke session state.

The Flask process owns the active session so every browser sees the same
queue and now-playing song. The server/TV browser is the only audio host:
it registers, reports its real playback position, and executes commands
sent by phone remotes.

State is intentionally in-memory. Restarting the server starts a fresh
session; refreshing a browser does not.
"""
from __future__ import annotations

import json
import queue as thread_queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from utils import is_finite_number


class PlaybackError(Exception):
    """A control command could not be applied."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class Subscriber:
    """One open SSE connection."""

    role: str
    client_id: str
    events: thread_queue.Queue = field(default_factory=lambda: thread_queue.Queue(maxsize=200))

    def push(self, event: str, data: dict[str, Any]) -> None:
        try:
            self.events.put_nowait((event, data))
        except thread_queue.Full:
            # Drop the oldest event so a slow client can catch up on the next
            # full state snapshot instead of blocking the server.
            try:
                self.events.get_nowait()
                self.events.put_nowait((event, data))
            except (thread_queue.Empty, thread_queue.Full):
                pass


def format_sse(event: str, data: dict[str, Any]) -> str:
    """Encode one Server-Sent Event frame."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


class PlaybackState:
    """Thread-safe shared playback state for one Flask app instance."""

    HOST_TIMEOUT_SECONDS = 8.0
    STATUSES = {"idle", "loading", "playing", "paused", "error"}

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # A restart creates a new id, which tells a reconnecting host that
        # command ids started over from zero.
        self._server_id = uuid.uuid4().hex
        self._revision = 0
        self._subscribers: dict[int, Subscriber] = {}
        self._next_subscriber_id = 1

        self._queue: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._status = "idle"
        self._position = 0.0
        self._duration = 0.0
        self._position_updated_at = time.monotonic()
        self._transpose = 0
        self._volume = 1.0
        self._last_error: str | None = None

        self._host_id: str | None = None
        self._host_last_seen = 0.0
        self._host_offline_reported = False

        self._command_id = 0
        self._acked_command_id = 0
        self._pending_commands: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def _host_connected_locked(self) -> bool:
        return (
            self._host_id is not None
            and self._host_last_seen > 0
            and time.monotonic() - self._host_last_seen <= self.HOST_TIMEOUT_SECONDS
        )

    def _position_locked(self) -> float:
        if self._status != "playing" or not self._host_connected_locked():
            return max(0.0, self._position)
        elapsed = max(0.0, time.monotonic() - self._position_updated_at)
        position = self._position + elapsed
        if self._duration > 0:
            position = min(position, self._duration)
        return max(0.0, position)

    def _freeze_position_locked(self, until: float | None = None) -> None:
        """Store the extrapolated position before leaving the playing state.

        ``until`` caps the extrapolation at the moment the host was last known
        to be alive (used when a host times out), so a stale player does not
        silently gain extra playback time after it stopped reporting.
        """
        position = max(0.0, self._position)
        if self._status == "playing":
            end = time.monotonic()
            if until is not None:
                end = min(end, until)
            position += max(0.0, end - self._position_updated_at)
        if self._duration > 0:
            position = min(position, self._duration)
        self._position = max(0.0, position)
        self._position_updated_at = time.monotonic()

    def _snapshot_locked(self) -> dict[str, Any]:
        connected = self._host_connected_locked()
        now = time.monotonic()
        last_seen_age = None
        if self._host_id is not None and self._host_last_seen > 0:
            last_seen_age = round(max(0.0, now - self._host_last_seen), 1)
        return {
            "server_id": self._server_id,
            "revision": self._revision,
            "host_connected": connected,
            "host_last_seen_seconds": last_seen_age,
            "status": self._status,
            "position": round(self._position_locked(), 3),
            "duration": round(max(0.0, self._duration), 3),
            "current": dict(self._current) if self._current else None,
            "queue": [dict(song) for song in self._queue],
            "transpose": int(self._transpose),
            "volume": round(float(self._volume), 3),
            "error": self._last_error,
            "last_command_id": self._acked_command_id,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    # ------------------------------------------------------------------
    # SSE subscriptions
    # ------------------------------------------------------------------

    def subscribe(self, role: str, client_id: str) -> Subscriber:
        subscriber = Subscriber(role=role, client_id=client_id)
        with self._lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers[subscriber_id] = subscriber
            # Replay commands that were produced while the host was between
            # SSE connections. The host ignores duplicates by command id.
            if role == "host" and client_id and client_id == self._host_id:
                for command in self._pending_commands:
                    subscriber.push("command", command)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            for subscriber_id, active in list(self._subscribers.items()):
                if active is subscriber:
                    del self._subscribers[subscriber_id]
                    break

    def pending_commands(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(command) for command in self._pending_commands]

    def commands_for_host(self, client_id: str) -> list[dict[str, Any]]:
        """Pending commands for the active host (polling fallback)."""
        with self._lock:
            self._require_host_id_locked(client_id)
            return [dict(command) for command in self._pending_commands]

    def _broadcast_locked(self, event: str, data: dict[str, Any],
                          role: str | None = None,
                          client_id: str | None = None) -> None:
        for subscriber in list(self._subscribers.values()):
            if role is not None and subscriber.role != role:
                continue
            if client_id is not None and subscriber.client_id != client_id:
                continue
            subscriber.push(event, data)

    def _touch_locked(self) -> None:
        self._revision += 1

    def _broadcast_state_locked(self) -> None:
        self._touch_locked()
        self._broadcast_locked("state", self._snapshot_locked())

    def _send_command_locked(self, action: str, **payload: Any) -> dict[str, Any]:
        self._command_id += 1
        command = {"id": self._command_id, "action": action, **payload}
        self._pending_commands.append(command)
        self._broadcast_locked("command", command, role="host", client_id=self._host_id)
        return command

    def _ack_commands_locked(self, last_command_id: Any) -> None:
        try:
            acked = int(last_command_id)
        except (TypeError, ValueError):
            return
        # A client cannot acknowledge commands the server never issued.
        acked = max(0, min(acked, self._command_id))
        if acked <= self._acked_command_id:
            return
        self._acked_command_id = acked
        self._pending_commands = [
            command for command in self._pending_commands
            if int(command.get("id", 0)) > acked
        ]

    # ------------------------------------------------------------------
    # Host lifecycle
    # ------------------------------------------------------------------

    def _require_host_id_locked(self, client_id: str) -> None:
        if not client_id or client_id != self._host_id:
            raise PlaybackError("This browser is not the active player.", 409)

    def _require_host_locked(self, client_id: str | None = None) -> None:
        if client_id is not None:
            self._require_host_id_locked(client_id)
        if not self._host_connected_locked():
            raise PlaybackError("The player is offline.", 409)

    def register_host(self, client_id: str) -> dict[str, Any]:
        if not isinstance(client_id, str) or not client_id.strip():
            raise PlaybackError("client_id is required.", 400)
        client_id = client_id.strip()[:120]
        with self._lock:
            previous = self._host_id
            previous_connected = previous is not None and self._host_connected_locked()
            if previous and previous != client_id:
                if previous_connected:
                    self._broadcast_locked(
                        "host_revoked",
                        {"reason": "Another player took over."},
                        role="host",
                        client_id=previous,
                    )
                # A different player must not replay the old player's commands.
                self._pending_commands = []
                self._acked_command_id = self._command_id
            elif previous == client_id and not previous_connected:
                # Same browser reconnecting after a timeout: stale commands
                # would otherwise load a song it already left behind.
                self._pending_commands = []
                self._acked_command_id = self._command_id
            self._host_id = client_id
            self._host_last_seen = time.monotonic()
            self._host_offline_reported = False

            # If the queue already has songs but no current song (for example,
            # songs were added while the player page was closed), start the
            # first one now. The host loads it from the returned snapshot.
            if self._current is None and self._queue:
                self._current = self._queue.pop(0)
                self._status = "loading"
                self._position = 0.0
                self._duration = 0.0
                self._transpose = 0
                self._last_error = None
                self._position_updated_at = time.monotonic()
                self._touch_locked()

            self._broadcast_state_locked()
            return self._snapshot_locked()

    def disconnect_host(self, client_id: str) -> dict[str, Any]:
        with self._lock:
            if client_id and client_id == self._host_id:
                if self._status in {"playing", "loading"}:
                    self._freeze_position_locked()
                    self._status = "paused"
                self._host_id = None
                self._host_last_seen = 0.0
                self._host_offline_reported = True
                self._broadcast_state_locked()
            return self._snapshot_locked()

    def refresh_presence(self) -> None:
        """Mark an unresponsive player offline and notify subscribers.

        Host liveness is derived from report timestamps, so without this a
        quiet player (no state changes) would look connected until some other
        request happened to take a snapshot. SSE and remote heartbeats call it
        so every browser sees the player go offline promptly.
        """
        with self._lock:
            if self._host_id is None or self._host_connected_locked():
                return

            changed = False
            if self._status in {"playing", "loading"}:
                # The host was last known alive at its last report; playback
                # can only be trusted up to the timeout boundary.
                self._freeze_position_locked(
                    self._host_last_seen + self.HOST_TIMEOUT_SECONDS)
                self._status = "paused"
                changed = True
            if not self._host_offline_reported:
                self._host_offline_reported = True
                changed = True
            if changed:
                self._broadcast_state_locked()

    def report_host(self, client_id: str, report: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_host_id_locked(client_id)
            self._host_last_seen = time.monotonic()
            self._host_offline_reported = False
            self._ack_commands_locked(report.get("last_command_id"))

            status = report.get("status")
            if status is not None:
                if not isinstance(status, str) or status not in self.STATUSES:
                    raise PlaybackError("Invalid player status.", 400)
                if status != self._status:
                    # Freeze the projected position before changing the state,
                    # otherwise a paused player would snap back to the position
                    # from the previous report.
                    if self._status == "playing" and status != "playing":
                        self._freeze_position_locked()
                    self._status = status
                    if status == "playing":
                        self._position_updated_at = time.monotonic()

            position = report.get("position")
            if position is not None:
                if not is_finite_number(position):
                    raise PlaybackError("position must be a number.", 400)
                self._position = max(0.0, float(position))
                self._position_updated_at = time.monotonic()

            duration = report.get("duration")
            if duration is not None:
                if not is_finite_number(duration):
                    raise PlaybackError("duration must be a number.", 400)
                self._duration = max(0.0, float(duration))

            transpose = report.get("transpose")
            if transpose is not None:
                try:
                    transpose = int(transpose)
                except (TypeError, ValueError):
                    raise PlaybackError("transpose must be an integer.", 400)
                self._transpose = max(-4, min(4, transpose))

            volume = report.get("volume")
            if volume is not None:
                if not is_finite_number(volume):
                    raise PlaybackError("volume must be a number.", 400)
                self._volume = max(0.0, min(1.0, float(volume)))

            error = report.get("error")
            self._last_error = str(error)[:500] if error else None

            self._broadcast_state_locked()
            return self._snapshot_locked()

    def host_ended(self, client_id: str, last_command_id: Any = None,
                   filename: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._require_host_id_locked(client_id)
            self._host_last_seen = time.monotonic()
            self._host_offline_reported = False
            self._ack_commands_locked(last_command_id)
            # A load command may already have advanced the queue. Ignore an
            # "ended" event that belongs to the song that was just replaced.
            if filename and self._current and filename != self._current.get("filename"):
                return self._snapshot_locked()
            return self._advance_locked()

    def host_error(self, client_id: str, message: str,
                   last_command_id: Any = None,
                   filename: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._require_host_id_locked(client_id)
            self._host_last_seen = time.monotonic()
            self._host_offline_reported = False
            self._ack_commands_locked(last_command_id)
            if filename and self._current and filename != self._current.get("filename"):
                return self._snapshot_locked()
            self._status = "error"
            self._last_error = str(message or "Playback failed.")[:500]
            self._broadcast_state_locked()
            return self._snapshot_locked()

    # ------------------------------------------------------------------
    # Queue and transport controls
    # ------------------------------------------------------------------

    def add_song(self, song: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(song, dict) or not song.get("filename"):
            raise PlaybackError("A valid song is required.", 400)
        with self._lock:
            started = False
            if self._current is None and self._host_connected_locked() and not self._queue:
                self._current = dict(song)
                self._status = "loading"
                self._position = 0.0
                self._duration = 0.0
                self._transpose = 0
                self._last_error = None
                self._position_updated_at = time.monotonic()
                self._send_command_locked(
                    "load",
                    song=dict(self._current),
                    position=0.0,
                    autoplay=True,
                    transpose=0,
                )
                started = True
            else:
                self._queue.append(dict(song))
            self._broadcast_state_locked()
            return {
                "snapshot": self._snapshot_locked(),
                "started": started,
            }

    def remove_queue(self, index: Any) -> dict[str, Any]:
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise PlaybackError("Queue index must be an integer.", 400)
        with self._lock:
            if index < 0 or index >= len(self._queue):
                raise PlaybackError("Queue item not found.", 404)
            self._queue.pop(index)
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def clear_queue(self) -> dict[str, Any]:
        with self._lock:
            self._queue.clear()
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def reorder_queue(self, from_index: Any, to_index: Any) -> dict[str, Any]:
        try:
            from_index = int(from_index)
            to_index = int(to_index)
        except (TypeError, ValueError):
            raise PlaybackError("Queue indexes must be integers.", 400)
        with self._lock:
            if not (0 <= from_index < len(self._queue)):
                raise PlaybackError("Queue item not found.", 404)
            if not (0 <= to_index < len(self._queue)):
                raise PlaybackError("Queue destination not found.", 404)
            song = self._queue.pop(from_index)
            self._queue.insert(to_index, song)
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def _advance_locked(self) -> dict[str, Any]:
        if self._current is not None:
            self._current = self._queue.pop(0) if self._queue else None
        elif self._queue:
            self._current = self._queue.pop(0)
        else:
            self._current = None

        self._transpose = 0
        self._position = 0.0
        self._duration = 0.0
        self._last_error = None
        self._position_updated_at = time.monotonic()

        if self._current is None:
            self._status = "idle"
            if self._host_connected_locked():
                self._send_command_locked("stop")
        else:
            self._status = "loading"
            if self._host_connected_locked():
                self._send_command_locked(
                    "load",
                    song=dict(self._current),
                    position=0.0,
                    autoplay=True,
                    transpose=0,
                )

        self._broadcast_state_locked()
        return self._snapshot_locked()

    def skip_remote(self) -> dict[str, Any]:
        """Skip from a phone: the server advances and tells the host to load."""
        with self._lock:
            self._require_host_locked()
            if self._current is None and not self._queue:
                raise PlaybackError("There is nothing to skip.", 409)
            return self._advance_locked()

    def play(self) -> dict[str, Any]:
        with self._lock:
            self._require_host_locked()
            if self._current is None:
                if self._queue:
                    self._current = self._queue.pop(0)
                    self._status = "loading"
                    self._position = 0.0
                    self._duration = 0.0
                    self._transpose = 0
                    self._position_updated_at = time.monotonic()
                    self._send_command_locked(
                        "load",
                        song=dict(self._current),
                        position=0.0,
                        autoplay=True,
                        transpose=0,
                    )
                else:
                    raise PlaybackError("There is nothing to play.", 409)
            else:
                # Respond immediately; the host acknowledges the command with
                # its real playback state on the next report.
                self._status = "playing"
                self._position_updated_at = time.monotonic()
                self._send_command_locked("play")
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            self._require_host_locked()
            if self._current is None:
                raise PlaybackError("There is nothing playing.", 409)
            self._freeze_position_locked()
            self._status = "paused"
            self._send_command_locked("pause")
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def restart(self) -> dict[str, Any]:
        with self._lock:
            self._require_host_locked()
            if self._current is None:
                raise PlaybackError("There is nothing playing.", 409)
            self._position = 0.0
            self._position_updated_at = time.monotonic()
            self._status = "playing"
            self._send_command_locked("restart")
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def seek(self, position: Any) -> dict[str, Any]:
        if not is_finite_number(position):
            raise PlaybackError("position must be a number.", 400)
        with self._lock:
            if not self._host_connected_locked():
                raise PlaybackError("The player is offline.", 409)
            if self._current is None:
                raise PlaybackError("There is nothing playing.", 409)
            target = max(0.0, float(position))
            if self._duration > 0:
                target = min(target, self._duration)
            self._position = target
            self._position_updated_at = time.monotonic()
            self._send_command_locked("seek", position=round(target, 3))
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def set_transpose(self, semitones: Any) -> dict[str, Any]:
        try:
            semitones = int(semitones)
        except (TypeError, ValueError):
            raise PlaybackError("transpose must be an integer.", 400)
        if abs(semitones) > 4:
            raise PlaybackError("transpose must be between -4 and 4.", 400)
        with self._lock:
            self._require_host_locked()
            if self._current is None:
                raise PlaybackError("There is nothing playing.", 409)
            self._transpose = semitones
            self._send_command_locked("transpose", semitones=semitones)
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def set_volume(self, volume: Any) -> dict[str, Any]:
        if not is_finite_number(volume):
            raise PlaybackError("volume must be a number.", 400)
        volume = max(0.0, min(1.0, float(volume)))
        with self._lock:
            self._require_host_locked()
            self._volume = volume
            self._send_command_locked("volume", volume=round(volume, 3))
            self._broadcast_state_locked()
            return self._snapshot_locked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._queue.clear()
            self._current = None
            self._status = "idle"
            self._position = 0.0
            self._duration = 0.0
            self._transpose = 0
            self._last_error = None
            self._position_updated_at = time.monotonic()
            if self._host_connected_locked():
                self._send_command_locked("stop")
            self._broadcast_state_locked()
            return self._snapshot_locked()
