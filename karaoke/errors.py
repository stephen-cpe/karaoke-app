"""Small exception types shared by the HTTP layer."""
from __future__ import annotations


class ApiError(Exception):
    """An API request failed with a client-visible message and status code."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
