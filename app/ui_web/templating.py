"""Shared Jinja2 environment for the GridSignals web UI.

One Jinja2Templates instance (autoescape on for .html) so app.py and the route
modules render through the same environment. Autoescaping is the XSS guard: all
upstream text (entity names, snippets, error text) is escaped by the template,
never by hand — the escaping lives in exactly one place.
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent
STATIC_DIR = _HERE / "static"
TEMPLATES_DIR = _HERE / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
