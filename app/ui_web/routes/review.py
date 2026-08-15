"""Review Queue / Triage routes (R8.2, R10.3/G2, R10.7).

The operator's triage surface, ported from app/ui/pages/2_Review_Queue.py. Three
sections, each with an honest empty state:

1. Pending entity matches — resolver candidates surfaced with the decision-trail
   ``reason`` (not a fabricated matched/rejected term list — not persisted for
   review candidates), confidence, an evidence snippet, and an http(s)-only
   source link. Accept/Reject POSTs to /review/triage, which writes a human
   ``entity_match_decisions`` row + the ``review_queue`` disposition via
   ``data.triage_decision`` (the only write path here) and HTMX-swaps the row
   out for a 'decision recorded' line. Approving records the decision only;
   creating a signal from an approved match is deferred this chunk.
2. Duplicate candidates — pairs of active signals that may be the same event
   (shared entity + trigger within ``data.DUPLICATE_NEAR_DAYS``, or an identical
   raw ``content_hash``). PROPOSALS ONLY: no merge, no dismiss, no auto-anything
   — a duplicate ruling is the operator's, so the row states its basis and links
   both cards by ``card_key``.
3. Judge / human disagreements — the SAME computation the Precision page
   reports (``precision.judge_human_disagreement``), surfaced as work rather
   than as a rate, so the two can never diverge. An empty queue distinguishes
   'nothing comparable yet' from 'the judge and you agreed'.
4. Source health — every source policy classified error / never_run / stale /
   disabled / ok via ``data.source_state``; error text shown verbatim (R10.3).
5. Stale license facts — read-only, verified more than 180 days ago (R10.7);
   the count is in the section header.

A reader except for ``data.triage_decision``. Display shaping lives in
render.py (pure); the template stays layout-only. UTC ISO-8601 (R10.2).
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates

router = APIRouter()


def _review_context(conn):
    """Pending rows + classified source health + stale facts for the page."""
    pending = [render.review_pending_view(item)
               for item in data.review_pending(conn)]
    sources = [render.source_health_view(row, data.source_state(row))
               for row in data.source_health(conn)]
    stale = [render.stale_fact_view(fact)
             for fact in data.stale_facts(conn, days=render.STALE_FACT_WINDOW_DAYS)]
    duplicates = [render.duplicate_candidate_view(pair)
                  for pair in data.duplicate_candidates(conn)]
    disagreement = data.judge_human_disagreements(conn)
    return {
        "pending": pending,
        "duplicates": duplicates,
        "duplicate_near_days": data.DUPLICATE_NEAR_DAYS,
        "disagreements": [render.disagreement_item_view(item)
                          for item in disagreement["items"]],
        "disagreement_comparable": disagreement["comparable"],
        "sources": sources,
        "stale": stale,
        "stale_count": len(stale),
        "stale_window_days": render.STALE_FACT_WINDOW_DAYS,
    }


@router.get("/review", response_class=HTMLResponse)
def review(request: Request, conn=Depends(get_db)):
    ctx = _review_context(conn)
    ctx["nav_active"] = "review"
    return templates.TemplateResponse(
        request=request, name="review.html", context=ctx)


@router.post("/review/triage", response_class=HTMLResponse)
def triage(request: Request, raw_event_id: str = Form(...),
           candidate_entity_id: str = Form(...), accept: bool = Form(...),
           conn=Depends(get_db)):
    """Record one human triage decision (R8.2): triage_decision writes BOTH the
    entity_match_decisions row and the review_queue disposition in one
    transaction. The pending row is swapped out for a confirmation line."""
    data.triage_decision(conn, raw_event_id, candidate_entity_id, accept=accept)
    return templates.TemplateResponse(
        request=request, name="_triage_done.html",
        context={"accepted": accept})
