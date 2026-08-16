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


def write_busy_response(request, exc):
    """Inline warning for a ``data.WriteBusyError`` (R3.2), for the two POST
    routes whose write does not hold the ingestion lock (feedback, triage).

    The write did nothing, so the operator's surface must survive: same shape as
    routes/incident.py's busy path, but overriding the swap to ``beforeend``
    rather than the target, because both callers' controls swap themselves away
    on success. Appending leaves the reason form (and any typed note) and the
    pending row's Accept/Reject buttons in place, so the click can be retried.
    The exception's own message is surfaced verbatim — it is already written for
    the operator and already says nothing was saved.
    """
    resp = templates.TemplateResponse(
        request=request, name="_write_busy.html", context={"error": str(exc)})
    resp.headers["HX-Reswap"] = "beforeend"
    return resp
