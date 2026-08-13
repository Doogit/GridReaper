"""Per-card permalink route (R8.1 card addressability).

A dedicated, read-only view of one signal card at a stable URL — the deep-link
target the Recent re-tiers panel (and any share/bookmark) needs, since cards
otherwise render only in bulk with no per-card address. The card renders through
the exact same render.card_view / _card.html as the feed, so its feedback and
re-tier affordances work here too (agent-native parity: anything doable on the
feed card is doable on its permalink).

The URL key is a sha256 of the signal_id (render.card_key) because signal_ids
are structured strings whose raw_event_id can be a URL — unsafe as a raw path
segment. sha256 is one-way, so a key is resolved back to its signal by scanning
data.all_signal_ids and re-hashing (small N, read-only). Unknown key -> 404 with
the honest empty copy, never a fabricated card. Reader only — writes nothing.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates

router = APIRouter()


def _resolve(conn, card_key):
    """The signal_id whose render.card_key matches, or None. Scans because the
    key is a one-way hash (see module docstring)."""
    for signal_id in data.all_signal_ids(conn):
        if render.card_key(signal_id) == card_key:
            return signal_id
    return None


@router.get("/card/{card_key}", response_class=HTMLResponse)
def card_permalink(request: Request, card_key: str, conn=Depends(get_db)):
    signal_id = _resolve(conn, card_key)
    detail = data.signal_detail(conn, signal_id) if signal_id else None
    if detail is None:
        return templates.TemplateResponse(
            request=request, name="card.html",
            context={"c": None, "nav_active": None}, status_code=404)
    card = render.card_view(detail, data.badge_legend(conn))
    return templates.TemplateResponse(
        request=request, name="card.html",
        context={"c": card, "nav_active": None})
