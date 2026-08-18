"""Account 360 routes (R8.3, R6.6, R7.6, R7.10, R9.1).

A per-entity view for the watchlist: an entity selector, a header (identifiers,
subsector, richness, coverage badge, gov-cloud posture, parent/children linked
to their own account pages), and five tabs — Timeline, Signals, Products,
Compliance Calendar and Entity Graph. The Signals tab reuses the feed's card
(_card.html) and its feedback flow verbatim, driven by render.card_view over
data.account_signals (same row shape as feed_page).

The three later tabs read three tables populated to very different depths, and
each says which: Products reads the account's OWN license play snapshots (R7.6)
and is empty for every account today because no signal carries an entity_id;
Compliance Calendar reads regulatory_obligations matched by subsector class
(never account-keyed) and is the one tab with live rows; Entity Graph lists
sourced entity_relationships edges rather than drawing a graph, because one edge
exists in the whole store. Every empty state names its reason (R6.6).

Zero account-scoped signals is the norm today (dark accounts, R6.6): the page
stays useful from identifiers, relationships, and gov-cloud posture, and both
tabs carry an honest low-volume-by-design empty state rather than a broken look.
Cards read snapshots, never live facts (R7.6). The account page paginates
nothing (the retired Streamlit page it replaced showed the full account history
too), so — unlike the feed — there is no keyset load-more here.

Reads bind the same data.py functions the retired Streamlit page used. The only
write path is feedback (routes/feed.py owns /feedback + /feedback/reason, shared
by the reused card), so this router adds no write of its own. The entity-selector
list is a direct active-entity query, matching what the retired Streamlit page
did — data.py exposes no active-only entity read, and per the plan the
UI does not mutate data.py to add one (flagged in the PR).
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates

router = APIRouter()

# Account tabs show the active history only, matching the retired Streamlit
# page's data.account_signals default; decayed/superseded rows keep their
# frozen scores but are not surfaced here.
STATUSES = ("active",)

# R6.6: an unknown, stale, or soft-disabled entity_id must render this honest
# not-found panel naming the id that was actually requested — never silently
# substitute a different company at HTTP 200 (the wrong-company fallback bug).
NOT_FOUND_MESSAGE = (
    'No account matches id "{requested_id}". It may have been removed from '
    "the watchlist. Pick an account from the selector above.")

EMPTY_WATCHLIST_MESSAGE = "No watchlist entities loaded. Run the seed loader first."

# /account/body always answers HTTP 200 (stock htmx does not swap the DOM on a
# non-2xx response), so its empty-state markup carries this data-state instead
# — a caller/agent parsing the fragment can tell "account not found" apart
# from "watchlist is empty" apart from any other load failure without parsing
# the English message. See _empty.html.
NOT_FOUND_STATE = "not-found"
EMPTY_WATCHLIST_STATE = "empty-watchlist"
LOAD_ERROR_STATE = "load-error"


def _entities(conn):
    """(entity_id, name) for every ACTIVE watchlist entity, name-ordered — the
    pool the selector searches over. Ported from the retired Streamlit page's
    _entities:
    soft-disabled entities (R8.7) drop out of the selector; their existing cards
    still render via the card queries' name lookup."""
    return conn.execute(
        "SELECT entity_id, name FROM watchlist_entities WHERE active = 1 "
        "ORDER BY name").fetchall()


def _resolve(entities, entity_id):
    """Resolve a requested entity_id against a NON-EMPTY active watchlist.

    * a known active entity_id resolves to itself.
    * an EMPTY entity_id (the bare /account route, no id requested) resolves
      to the selector's own deterministic default: the first, name-ordered
      entity. This is the SELECTOR's default, not a fallback for a bad id.
    * a NON-EMPTY entity_id that names no active entity resolves to None: an
      unknown, stale, or soft-disabled id must render as not-found, never as
      a silent substitution of a different company.

    Callers must guarantee ``entities`` is non-empty — an empty watchlist is a
    distinct state handled before this is called (EMPTY_WATCHLIST_MESSAGE).
    """
    valid = {e["entity_id"] for e in entities}
    if entity_id in valid:
        return entity_id
    if entity_id:
        return None
    return entities[0]["entity_id"]


def _account_context(conn, entity_id, requested_id=""):
    """Header view + timeline rows + signal cards for one account; an empty
    body context (account=None) when the entity cannot be loaded, or the
    not-found panel (naming ``requested_id``) when ``entity_id`` is None."""
    if entity_id is None:
        return {"selected": "", "not_found": True, "requested_id": requested_id,
                "account": None, "timeline": [], "cards": [],
                "empty_message": NOT_FOUND_MESSAGE.format(
                    requested_id=requested_id),
                "empty_state": NOT_FOUND_STATE}
    header = data.account_header(conn, entity_id)
    if header is None:
        return {"selected": entity_id, "not_found": False, "account": None,
                "timeline": [], "cards": [], "empty_message":
                "That account could not be loaded.",
                "empty_state": LOAD_ERROR_STATE}
    signals = data.account_signals(conn, entity_id, statuses=STATUSES)
    legend = data.badge_legend(conn)
    cards = []
    for s in signals:
        detail = data.signal_detail(conn, s["signal_id"])
        if detail is not None:
            cards.append(render.card_view(detail, legend))
    return {
        "selected": entity_id,
        "not_found": False,
        "account": render.account_header_view(header),
        "timeline": render.timeline_rows(signals),
        "cards": cards,
        "play_rows": render.account_play_rows(
            data.account_license_plays(conn, entity_id, statuses=STATUSES),
            legend),
        "calendar": render.account_calendar_view(
            data.account_obligations(conn, entity_id)),
        "calendar_caption": render.CALENDAR_CAPTION,
        "relationship_rows": render.account_relationship_rows(
            data.account_relationships(conn, entity_id)),
    }


def _empty_watchlist_context():
    return {"selected": "", "not_found": False, "requested_id": "",
            "account": None, "timeline": [], "cards": [], "empty_message":
            EMPTY_WATCHLIST_MESSAGE, "empty_state": EMPTY_WATCHLIST_STATE}


def _page_context(conn, entity_id):
    """Full-page context: the selector pool plus the resolved account body."""
    entities = _entities(conn)
    if not entities:
        ctx = _empty_watchlist_context()
        ctx["entities"] = entities
        return ctx
    ctx = _account_context(
        conn, _resolve(entities, entity_id), requested_id=entity_id)
    ctx["entities"] = entities
    return ctx


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, entity_id: str = "", conn=Depends(get_db)):
    """Full-page route: an unknown/stale/soft-disabled entity_id is a genuine
    404 (mirroring card.py's permalink precedent) — a caller/agent can rely on
    the status code, not just the placeholder copy. An empty watchlist is a
    different condition (nothing seeded, not a bad lookup) and stays 200."""
    ctx = _page_context(conn, entity_id)
    ctx["nav_active"] = "account"
    status_code = 404 if ctx.get("not_found") else 200
    return templates.TemplateResponse(
        request=request, name="account.html", context=ctx,
        status_code=status_code)


@router.get("/account/body", response_class=HTMLResponse)
def account_body(request: Request, entity_id: str = "", conn=Depends(get_db)):
    """Header + tabs only — the entity selector swaps this into #gs-account-body
    when a different account is picked (the selector itself stays in place, so
    its native selection persists without a re-render). Always HTTP 200 (even
    an unknown/stale id) — base.html loads stock htmx.min.js with no error-swap
    extension, so a 404 here would leave the previously selected account
    rendered with no feedback."""
    entities = _entities(conn)
    if not entities:
        return templates.TemplateResponse(
            request=request, name="_account_body.html",
            context=_empty_watchlist_context())
    ctx = _account_context(
        conn, _resolve(entities, entity_id), requested_id=entity_id)
    return templates.TemplateResponse(
        request=request, name="_account_body.html", context=ctx)
