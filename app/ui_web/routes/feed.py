"""Home signal feed routes (R8.1, R7.2, R6.6, R9.1).

The scope-separated feed: account-scoped cards first, a labeled divider, then
sector/regulatory cards (R7.2). Status filter defaults to active; decayed/
superseded/dismissed are reachable by widening it (HTMX re-renders the feed
body). Sort defaults to score (see SORT_OPTIONS); "Newest" restores the
chronological feed with keyset pagination on (event_date, signal_id) via an
HTMX "Load more" button per group. Both controls live outside #gs-feed-body and
are hx-include'd by every fragment request - including the 120s auto-refresh
poll - so a re-render never resets the operator's chosen view (R8.1).
Feedback writes go through data.record_feedback (R9.1:
not_useful requires a reason code) — enforced server-side, so a missing reason
returns the reason form with the error, never a bad row.

Reads bind the same data.py functions the Streamlit UI uses; the card shape is
assembled by render.card_view. The feed is a reader that writes only feedback
rows (not a config write — no single-writer lock needed, matching app/ui/Home.py).
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates, write_busy_response

router = APIRouter()

PAGE_SIZE = 25

# Status filter options (mirrors app/ui/Home.py). Feed defaults to active.
# 'retracted' (R3.7) is a card a corrected classifier no longer emits. It is
# listed here, and included in "all", deliberately: the filter set is an
# allow-list, so leaving it out would hide those cards behind an "all" that is
# not all - the pipeline correcting itself should be inspectable, not silent.
STATUS_FILTERS = {
    "active": ("active",),
    "decayed": ("decayed",),
    "superseded": ("superseded",),
    "dismissed": ("dismissed",),
    "retracted": ("retracted",),
    "all": ("active", "decayed", "superseded", "dismissed", "retracted"),
}


# Sort options, in display order. The feed LANDS on score: the operator's
# question on a Monday is "what deserves action this week", and a date-keyset
# feed cannot answer it - the highest-scoring card sits wherever its event date
# puts it, and at status=all the retracted backlog buries it pages down. A
# score ranking is capped at PAGE_SIZE per group rather than paged, because the
# keyset is chronological by construction (data.feed_page); "Newest" is one
# control away and keeps the full paged archive reachable.
SORT_OPTIONS = (("score", "Top score"), ("recent", "Newest"))
DEFAULT_SORT = "score"


def _choice(status):
    return status if status in STATUS_FILTERS else "active"


def _sort(value):
    return value if value in dict(SORT_OPTIONS) else DEFAULT_SORT


def _group(conn, scope_group, statuses, after_key, legend, choice, sort):
    """One page of cards for a scope group + the next-page cursor (or None).
    The cursor exists only under sort='recent' (score order is a capped ranking,
    not a keyset) and only when a further page actually exists (probe), so the
    Load more button never dead-ends."""
    rows = data.feed_page(conn, scope_group=scope_group, after_key=after_key,
                          limit=PAGE_SIZE, statuses=statuses, order=sort)
    cards = []
    for row in rows:
        detail = data.signal_detail(conn, row["signal_id"])
        if detail is not None:
            cards.append(render.card_view(detail, legend))
    nxt = None
    if sort == "recent" and len(rows) == PAGE_SIZE:
        last = rows[-1]
        probe = data.feed_page(
            conn, scope_group=scope_group,
            after_key=(last["event_date"], last["signal_id"]), limit=1,
            statuses=statuses)
        if probe:
            nxt = {"scope": scope_group, "status": choice,
                   "after_date": last["event_date"], "after_id": last["signal_id"]}
    return cards, nxt


def _feed_context(conn, choice, sort):
    statuses = STATUS_FILTERS[choice]
    legend = data.badge_legend(conn)
    account_cards, account_next = _group(conn, "account", statuses, None,
                                         legend, choice, sort)
    sector_cards, sector_next = _group(conn, "sector", statuses, None,
                                       legend, choice, sort)
    return {
        "status": choice,
        "status_options": list(STATUS_FILTERS),
        "sort": sort,
        "sort_options": SORT_OPTIONS,
        "page_size": PAGE_SIZE,
        "account_cards": account_cards, "account_next": account_next,
        "sector_cards": sector_cards, "sector_next": sector_next,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request, status: str = "active", sort: str = DEFAULT_SORT,
         conn=Depends(get_db)):
    ctx = _feed_context(conn, _choice(status), _sort(sort))
    ctx["nav_active"] = "home"
    return templates.TemplateResponse(request=request, name="feed.html", context=ctx)


@router.get("/feed", response_class=HTMLResponse)
def feed_body(request: Request, status: str = "active",
              sort: str = DEFAULT_SORT, conn=Depends(get_db)):
    """Feed body only — the status and sort controls (and the 120s poll) swap
    this into #gs-feed-body. Both are hx-include'd on every one of those
    requests, so a poll never resets the view the operator chose."""
    ctx = _feed_context(conn, _choice(status), _sort(sort))
    return templates.TemplateResponse(request=request, name="_feed_body.html", context=ctx)


@router.get("/feed/more", response_class=HTMLResponse)
def feed_more(request: Request, scope: str = "account", status: str = "active",
              after_date: str = "", after_id: str = "", conn=Depends(get_db)):
    """Next keyset page for one group; the Load more button swaps itself with
    this partial (more cards + a fresh button, or nothing at the end). Keyset
    paging is chronological, so this route is always sort='recent' — the button
    is only ever rendered in that view."""
    scope_group = scope if scope in data.SCOPE_GROUPS else "account"
    choice = _choice(status)
    after_key = (after_date, after_id) if after_date and after_id else None
    cards, nxt = _group(conn, scope_group, STATUS_FILTERS[choice], after_key,
                        data.badge_legend(conn), choice, "recent")
    return templates.TemplateResponse(
        request=request, name="_cards.html", context={"cards": cards, "next": nxt})


@router.get("/feedback/reason", response_class=HTMLResponse)
def feedback_reason(request: Request, signal_id: str):
    """Reveal the reason form when a card's 'Not useful' is clicked."""
    return templates.TemplateResponse(
        request=request, name="_feedback_reason.html",
        context={"signal_id": signal_id, "reason_codes": data.REASON_CODES,
                 "target_id": render.feedback_dom_id(signal_id), "error": None})


@router.post("/feedback", response_class=HTMLResponse)
def feedback(request: Request, signal_id: str = Form(...), verdict: str = Form(...),
             reason_code: str = Form(""), note: str = Form(""),
             conn=Depends(get_db)):
    """Record one feedback row. record_feedback enforces the reason-required
    rule (R9.1); on ValueError the reason form comes back with the error and
    nothing is written. A WriteBusyError (R3.2 — the writer lock stayed held) is
    an inline warning appended beside the controls, never a 500: the write did
    nothing, so the buttons/form stay put and the click can be retried."""
    try:
        data.record_feedback(conn, signal_id, verdict, reason_code or None, note)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request, name="_feedback_reason.html",
            context={"signal_id": signal_id, "reason_codes": data.REASON_CODES,
                     "target_id": render.feedback_dom_id(signal_id),
                     "error": str(exc)})
    except data.WriteBusyError as exc:
        return write_busy_response(request, exc)
    return templates.TemplateResponse(
        request=request, name="_feedback_done.html", context={})
