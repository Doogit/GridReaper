"""Recent re-tiers audit route (R8.7 oversight; R7.12, R10.2).

A read-only, central view of every incident re-tier across all cards — the one
place an operator sees who changed which card's tier and whether any card was
cleared for customer-facing outreach (R7.12). The per-card trail lives on each
incident card (routes/incident.py + data.incident_tier_history); this aggregates
them. Reader only — this router writes nothing (the write path is
routes/incident.py, lock-guarded). UTC ISO-8601 (R10.2).
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates

router = APIRouter()


@router.get("/retiers", response_class=HTMLResponse)
def retiers(request: Request, conn=Depends(get_db)):
    ctx = {
        "nav_active": "retiers",
        "retiers": render.recent_retiers_view(data.recent_retiers(conn)),
    }
    return templates.TemplateResponse(
        request=request, name="retiers.html", context=ctx)
