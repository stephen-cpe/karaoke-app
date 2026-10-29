"""Flask blueprints for the karaoke application."""
from __future__ import annotations

from flask import Flask

from karaoke.routes.library import library
from karaoke.routes.logs import logs
from karaoke.routes.pages import pages
from karaoke.routes.session import session


def register_blueprints(app: Flask) -> None:
    """Register every route group. Pages owns the final static catch-all."""
    app.register_blueprint(session)
    app.register_blueprint(library)
    app.register_blueprint(logs)
    app.register_blueprint(pages)
