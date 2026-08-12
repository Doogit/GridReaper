"""FastAPI application for the GridSignals web UI (R8.9 UI port — Chunk 0 scaffold).

Wires Jinja2 templates and static assets and renders one placeholder page through
the shared base layout, plus a health route. The real pages (feed, account,
review, precision, regulatory, admin) land in later chunks. The UI stays a
read-only reader over app/ui/data.py (see deps.py); the backend stays stdlib-only.

Run (dev):
    uvicorn app.ui_web.app:app --reload

Build CSS (Tailwind standalone binary — no npm, no node_modules):
    tailwindcss -i app/ui_web/tailwind.input.css -o app/ui_web/static/app.css --minify
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "static"
_TEMPLATES = _HERE / "templates"

app = FastAPI(title="GridSignals", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=_STATIC), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    """Liveness probe. Returns 200 'ok'."""
    return "ok"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Placeholder landing page, rendered through the shared base layout."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"nav_active": "home"},
    )
