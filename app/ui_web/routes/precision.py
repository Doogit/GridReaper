"""Feedback / Precision routes (R8.6, R9.2-R9.5, R9.12).

The operator's QA surface, ported from app/ui/pages/4_Precision.py. Every number
here is a *precision / quality* metric — judgment consistency (human useful-rate)
and evidence accuracy (automated judge accuracy) — explicitly NOT validated sales
lift (a prominent framing caption says so). The TRUST INVARIANT is carried to the
DOM: a rate is never a bare percentage; every rate ships with its n, an empty
denominator reads as an honest "n/a", and below the G1 sample floor (n<20) the
headline shows the low-n copy rather than a fabricated gauge.

Reader only — this router writes nothing. It takes ONE read,
``data.precision_report``, which owns every app.audit.precision computation the
page shows (R10.9: the view never calls the backend directly), and reshapes the
computed dicts into template-ready view dicts via render.precision_*. UTC
ISO-8601 (R10.2).
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates

router = APIRouter()


@router.get("/precision", response_class=HTMLResponse)
def precision(request: Request, conn=Depends(get_db)):
    report = data.precision_report(conn)

    ctx = {
        "nav_active": "precision",
        "headline": render.precision_headline_view(report),
        "g1": render.precision_g1_view(report["g1"]),
        "g2": render.precision_g2_view(report["g2"]),
        "spotcheck": render.precision_spotcheck_view(report["spotcheck"]),
        "useful_tables": render.precision_useful_tables(
            report["useful_by_dimension"]),
        "auto_tables": render.precision_auto_tables(
            report["auto_by_dimension"]),
        "reason_codes": report["reason_codes"],
        "disagreement": render.precision_disagreement_view(
            report["disagreement"]),
        "halflife": render.precision_halflife_view(report["halflife"]),
        "run_history": render.precision_run_history(report["runs"]),
    }
    return templates.TemplateResponse(
        request=request, name="precision.html", context=ctx)
