"""In-app digest view (R8.8, R6.6).

Mirrors the last SAVED digest file inside the app (KTD6 nav: after Regulatory,
before Admin). The digest is explicitly a point-in-time artifact a human sent, so
this route serves the newest saved ``digest-*.html`` rather than re-rendering
live (design finding D2) — and stamps its generation time prominently so the
in-app view reads as a snapshot, not a live feed. No saved digest yet → honest
"no digest generated yet" copy (R6.6), never a 500.

The route is a read-only reader: it reads the file the ``app.digest`` CLI wrote;
it does not itself build a digest. It reuses the digest BODY within base.html by
extracting the saved document's inner ``<body>`` markup + its as-of timestamp, so
the in-app view is byte-for-byte the artifact that was distributed.
"""
import glob
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup

from app.db.connection import DEFAULT_DB_PATH
from app.digest import _digest_dir
from app.ui_web.templating import templates

router = APIRouter()

_BODY_RE = re.compile(r"<body[^>]*>(?P<body>.*)</body>", re.IGNORECASE | re.DOTALL)
# The header line the digest template writes: "Generated at <iso> (UTC)".
_ASOF_RE = re.compile(r"Generated at (?P<asof>[^<(]+?) \(UTC\)")


def _latest_digest_path():
    """Newest saved dated digest file, or None if none exists. Reads the newest
    by filename (``digest-YYYY-MM-DD.html`` sorts lex- and chrono-graphically),
    ignoring the ``digest-latest.html`` alias so the dated artifact is canonical."""
    db_path = os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH
    dated = sorted(glob.glob(os.path.join(_digest_dir(db_path), "digest-2*.html")))
    return dated[-1] if dated else None


@router.get("/digest", response_class=HTMLResponse)
def digest(request: Request):
    path = _latest_digest_path()
    ctx = {"nav_active": "digest"}
    if path is None:
        ctx["body"] = None
        ctx["as_of"] = None
        return templates.TemplateResponse(
            request=request, name="digest_page.html", context=ctx)
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    body_match = _BODY_RE.search(html)
    asof_match = _ASOF_RE.search(html)
    # The saved file's inner body is trusted markup we produced (autoescaped at
    # generation); wrap it in base.html unchanged so the in-app view is the exact
    # distributed artifact.
    ctx["body"] = Markup(body_match.group("body")) if body_match else None
    ctx["as_of"] = asof_match.group("asof").strip() if asof_match else None
    return templates.TemplateResponse(
        request=request, name="digest_page.html", context=ctx)
