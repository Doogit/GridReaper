"""FastAPI application for the GridSignals web UI (R8.9 UI port).

Mounts static assets, registers the page routers, and exposes a health route.
Chunk 1 wires the Home signal feed (routes/feed.py: scope-separated cards,
status filter, keyset load-more, feedback); Chunk 2 adds Account 360
(routes/account.py: entity selector, header, Timeline + Signals tabs reusing the
feed card). Later chunks add review, precision, regulatory, and admin routers.
The UI stays a read-only reader over app/ui/data.py (see deps.py); the backend
stays stdlib-only.

Run (dev):
    uvicorn app.ui_web.app:app --reload

Build CSS (Tailwind standalone binary — no npm, no node_modules):
    tailwindcss -i app/ui_web/tailwind.input.css -o app/ui_web/static/app.css --minify
The committed static/app.css is currently hand-authored (custom R8.9 card CSS,
which is a component system rather than utility classes); the standalone Tailwind
compile is wired for when utility-based layout is introduced.
"""
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.ui_web.routes import account, feed, review
from app.ui_web.templating import STATIC_DIR

app = FastAPI(title="GridSignals", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(feed.router)
app.include_router(account.router)
app.include_router(review.router)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    """Liveness probe. Returns 200 'ok'."""
    return "ok"
