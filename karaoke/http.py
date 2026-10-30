"""HTTP helpers shared by the Flask blueprints."""
from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from typing import Any

from flask import current_app, request

from karaoke.config import DEFAULT_PORT, MAX_CLIENT_ID_LENGTH
from karaoke.errors import ApiError


def is_local_request() -> bool:
    """Return True when the request came from the karaoke machine itself."""
    address = request.remote_addr or ""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address.lower() in {"localhost", ""}
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


def editor_access_allowed() -> bool:
    """Return True when the local-only Sync Editor may be used."""
    return bool(current_app.config.get("ENABLE_EDITOR")) and is_local_request()


def client_id_from(data: Any) -> str:
    """Read and normalize a client_id from a JSON object or query mapping."""
    value = data.get("client_id") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ApiError("client_id is required.", 400)
    return value.strip()[:MAX_CLIENT_ID_LENGTH]


def json_body() -> dict[str, Any]:
    """Return the request JSON object or raise a consistent 400 error."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Expected a JSON body.", 400)
    return data


@lru_cache(maxsize=1)
def local_ip() -> str:
    """Best-effort LAN address for the phone join link."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Connecting a UDP socket only selects the outgoing interface; it
            # does not send any packets.
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    try:
        address = socket.gethostbyname(socket.gethostname())
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    return "127.0.0.1"


def base_url(host: str, port: int | None) -> str:
    if port in (None, "", "80", 80):
        return f"http://{host}"
    return f"http://{host}:{port}"


def request_port() -> int:
    try:
        return int(request.environ.get("SERVER_PORT") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT
