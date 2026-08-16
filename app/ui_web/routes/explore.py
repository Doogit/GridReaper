"""Explore routes: Trigger Analytics + Watchlist Map + Ransomware Activity (R8.5).

Three tabs over the built store, Analytics default (the more immediately useful
first view — D4), mirroring the Account 360 tab pattern. Reader only: this router
writes nothing. It binds four data.py reads (explore_analytics_counts,
explore_facility_points, explore_state_density, ransomware_activity) and
reshapes them via the render.py seam (explore_analytics_view, explore_map_svg,
ransomware_activity_view).

Trust carried to the DOM (R4.1/R8.5/R6.6): analytics counts are counts of
sourced signals, each row keeping its dimension identity; the map plots ONLY
facilities the reader already gated (>=0.85) — the gate lives in data.py, never
here — and empty data yields the base geography plus an honest note rather than
a broken surface. UTC ISO-8601 (R10.2) is not surfaced here (no timestamps).

The Ransomware tab is the one NON-signal surface here: aggregate counts over the
leak-site feed, deliberately scoreless and account-free (the R8.4 Regulatory
Monitor's "situational, not a card" framing). Its R4.1 protections — singleton
crews withheld, marginals never cross-tabbed — are enforced in data.py, so this
route only chooses the tab and passes the shaped view through.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.ui import data
from app.ui_web import render
from app.ui_web.deps import get_db
from app.ui_web.templating import templates

router = APIRouter()

# The three tabs; Analytics is the default (D4). An unknown ?tab= falls back to it.
TABS = ("analytics", "map", "ransomware")

# Tab-specific honest empty-state copy (R6.6): the Analytics tab gets its OWN
# copy, not only the map's — a store with no signals should say so on the tab an
# operator lands on first.
ANALYTICS_EMPTY = (
    "No signals yet — trigger analytics are counts of sourced signals, so this "
    "tab fills in as the pipeline classifies events. Low-volume by design until "
    "a backfill lands.")

# The Ransomware tab counts raw leak-site listings, not signals, so its empty
# state is about ingestion — "no signals yet" would be the wrong diagnosis on
# this tab (R6.6).
RANSOMWARE_EMPTY = (
    "No leak-site listings stored yet — this tab counts ransomware.live records "
    "as they are ingested, not signals, so it fills in on the next ingest run. "
    "ransomware.live is a rolling recent feed; the window it covers is stated "
    "with the counts once there is data.")


@router.get("/explore", response_class=HTMLResponse)
def explore(request: Request, tab: str = "analytics", conn=Depends(get_db)):
    # Casefolded: a shared '?tab=Ransomware' link silently landing the reader on
    # Analytics reads as a broken link, not as a fallback.
    requested = (tab or "").strip().lower()
    active_tab = requested if requested in TABS else "analytics"

    memo = {}
    analytics = data.analytics_counts(conn, memo=memo)
    analytics_tables = render.explore_analytics_view(analytics["counts"])
    facility_points = data.explore_facility_points(conn)
    state_rows = data.explore_state_density(conn)
    map_view = render.explore_map_svg(facility_points, state_rows)

    ransomware = render.ransomware_activity_view(
        data.ransomware_activity(conn))

    ctx = {
        "nav_active": "explore",
        "active_tab": active_tab,
        "analytics_tables": analytics_tables,
        "analytics_freshness": analytics,
        "analytics_empty": ANALYTICS_EMPTY,
        "ransomware": ransomware,
        "ransomware_empty": RANSOMWARE_EMPTY,
        "map": map_view,
        "density_legend": list(zip(render.MAP_DENSITY_LEGEND,
                                   ("gs-map-d0", "gs-map-d1", "gs-map-d2",
                                    "gs-map-d3", "gs-map-d4"))),
    }
    return templates.TemplateResponse(
        request=request, name="explore.html", context=ctx)
