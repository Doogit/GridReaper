"""Explore routes: Trigger Analytics + evidence-safe Watchlist Map (R8.5).

Two tabs over the built store, Analytics default (the more immediately useful
first view — D4), mirroring the Account 360 tab pattern. Reader only: this router
writes nothing. It binds three data.py reads (explore_analytics_counts,
explore_facility_points, explore_state_density) and reshapes them via the
render.py seam (explore_analytics_view, explore_map_svg).

Trust carried to the DOM (R4.1/R8.5/R6.6): analytics counts are counts of
sourced signals, each row keeping its dimension identity; the map plots ONLY
facilities the reader already gated (>=0.85) — the gate lives in data.py, never
here — and empty data yields the base geography plus an honest note rather than
a broken surface. UTC ISO-8601 (R10.2) is not surfaced here (no timestamps).
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates

router = APIRouter()

# The two tabs; Analytics is the default (D4). An unknown ?tab= falls back to it.
TABS = ("analytics", "map")

# Tab-specific honest empty-state copy (R6.6): the Analytics tab gets its OWN
# copy, not only the map's — a store with no signals should say so on the tab an
# operator lands on first.
ANALYTICS_EMPTY = (
    "No signals yet — trigger analytics are counts of sourced signals, so this "
    "tab fills in as the pipeline classifies events. Low-volume by design until "
    "a backfill lands.")


@router.get("/explore", response_class=HTMLResponse)
def explore(request: Request, tab: str = "analytics", conn=Depends(get_db)):
    active_tab = tab if tab in TABS else "analytics"

    analytics_tables = render.explore_analytics_view(
        data.explore_analytics_counts(conn))
    facility_points = data.explore_facility_points(conn)
    state_rows = data.explore_state_density(conn)
    map_view = render.explore_map_svg(facility_points, state_rows)

    ctx = {
        "nav_active": "explore",
        "active_tab": active_tab,
        "analytics_tables": analytics_tables,
        "analytics_empty": ANALYTICS_EMPTY,
        "map": map_view,
        "density_legend": list(zip(render.MAP_DENSITY_LEGEND,
                                   ("gs-map-d0", "gs-map-d1", "gs-map-d2",
                                    "gs-map-d3", "gs-map-d4"))),
    }
    return templates.TemplateResponse(
        request=request, name="explore.html", context=ctx)
