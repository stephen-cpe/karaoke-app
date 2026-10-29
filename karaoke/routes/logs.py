"""Browser-error relay endpoint."""
from __future__ import annotations

import json
import logging as stdlib_logging

from flask import Blueprint, jsonify

from karaoke.config import MAX_LOG_MESSAGE_LENGTH
from karaoke.http import json_body
from karaoke.logging import logger
from utils import single_line

logs = Blueprint("logs", __name__)


@logs.post("/api/log")
def api_log():
    data = json_body()
    if not data.get("message"):
        return jsonify({"error": "message is required"}), 400

    level = {
        "debug": stdlib_logging.DEBUG,
        "info": stdlib_logging.INFO,
        "warning": stdlib_logging.WARNING,
        "error": stdlib_logging.ERROR,
    }.get(str(data.get("level", "error")).lower(), stdlib_logging.ERROR)

    message = single_line(data["message"], MAX_LOG_MESSAGE_LENGTH)
    details = data.get("details")
    if details is not None:
        try:
            details_text = json.dumps(details, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            details_text = repr(details)
        message += " | " + single_line(details_text, MAX_LOG_MESSAGE_LENGTH)

    logger.log(level, "[browser] %s", message)
    return "", 204
