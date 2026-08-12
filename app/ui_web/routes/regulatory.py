"""Regulatory Monitor route (R8.4, R7.2, D8).

A read-only view of non-graduated regulatory chatter: raw Federal Register /
NERC records that did NOT become scored signals. They are shown here VERBATIM
from the source's own record — the page never scores them, never attributes an
account, and never invents an action verb. A record graduates to the Signal
Feed only when it carries a durable compliance clock + an affected scope + a
mapped product implication + a concrete next action (D8/R7.2); everything on
this page is, by definition, missing at least one of those — so nothing here is
a signal, a card, or a score. This is a DISTINCT LIST SURFACE, not the signal
card (never reuses _card.html).

Gating is preserved from app/ui/pages/6_Regulatory_Monitor.py: the list is
data.regulatory_monitor, which returns only regulatory raw_events the current
regulatory parser evaluated with zero emitted signals and no signals row — i.e.
only EVALUATED, non-graduated chatter. The empty state is honest: regulatory
chatter is intentionally low-signal, so an empty page reads 'by design', never
'broken' (R6.6). Reader only — this route writes nothing.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates

router = APIRouter()

LIMIT = 100

# Verbatim from the Streamlit page's empty state so the honest low-signal framing
# reads identically across both surfaces.
EMPTY_MESSAGE = (
    "No non-graduated regulatory chatter — either nothing has been ingested "
    "from the Federal Register / NERC yet, or every record graduated to the "
    "Signal Feed.")


@router.get("/regulatory", response_class=HTMLResponse)
def regulatory(request: Request, conn=Depends(get_db)):
    items = data.regulatory_monitor(conn, limit=LIMIT)
    ctx = {
        "records": render.regulatory_records(items),
        "empty_message": EMPTY_MESSAGE,
        "nav_active": "regulatory",
    }
    return templates.TemplateResponse(
        request=request, name="regulatory.html", context=ctx)
