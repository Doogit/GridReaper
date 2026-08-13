"""Feedback / Precision routes (R8.6, R9.2-R9.5, R9.12).

The operator's QA surface, ported from app/ui/pages/4_Precision.py. Every number
here is a *precision / quality* metric — judgment consistency (human useful-rate)
and evidence accuracy (automated judge accuracy) — explicitly NOT validated sales
lift (a prominent framing caption says so). The TRUST INVARIANT is carried to the
DOM: a rate is never a bare percentage; every rate ships with its n, an empty
denominator reads as an honest "n/a", and below the G1 sample floor (n<20) the
headline shows the low-n copy rather than a fabricated gauge.

Reader only — this router writes nothing. It binds the same four data.py reads
the Streamlit page uses (precision_feedback_rows, precision_audit_rows,
precision_halflife_rows, audit_run_rows) plus the pure app.audit.precision
functions, and reshapes them into template-ready view dicts via render.precision_*
(the sanctioned render.py seam). UTC ISO-8601 (R10.2).
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
    feedback_rows = data.precision_feedback_rows(conn)
    audit_rows = data.precision_audit_rows(conn)
    halflife_rows = data.precision_halflife_rows(conn)
    run_rows = data.audit_run_rows(conn)

    ctx = {
        "nav_active": "precision",
        "headline": render.precision_headline_view(feedback_rows, audit_rows),
        "g1": render.precision_g1_view(feedback_rows, audit_rows),
        "g2": render.precision_g2_view(feedback_rows, audit_rows),
        "spotcheck": render.precision_spotcheck_view(audit_rows, feedback_rows),
        "useful_tables": render.precision_useful_tables(feedback_rows),
        "auto_tables": render.precision_auto_tables(audit_rows),
        "reason_codes": render.precision_reason_codes(feedback_rows),
        "disagreement": render.precision_disagreement_view(
            audit_rows, feedback_rows),
        "halflife": render.precision_halflife_view(halflife_rows),
        "run_history": render.precision_run_history(run_rows),
    }
    return templates.TemplateResponse(
        request=request, name="precision.html", context=ctx)
