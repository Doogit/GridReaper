"""GridSignals — Signal Feed (home). R8.1, R7.2, R6.6, R9.1.

The scope-separated signal feed: account-scoped cards first, then a labeled
sector/regulatory divider, then sector cards (R7.2 — sector cards never mix into
account cards). Status filter defaults to active; decayed/superseded/dismissed
are reachable by widening it (feed hides them by default). Keyset pagination on
(event_date, signal_id) with a per-scope "Load more" (page size 25). Feedback
writes go through data.record_feedback (R9.1: not_useful requires a reason code).
The feed body is wrapped in a 120s st.fragment (R8.1) when available.

The app is a reader: it writes only feedback rows. Cards read license_play
snapshots via data.signal_detail, never live license_facts (R7.6).
"""
import os
import sys

import streamlit as st

# `streamlit run app/ui/Home.py` puts app/ui/ on sys.path, not the repo root,
# so `import app...` needs the project root (3 levels up) added explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.db.connection import get_connection, DEFAULT_DB_PATH
from app.ui import components, data, theme

PAGE_SIZE = 25
STATUS_FILTERS = {
    "active": ("active",),
    "decayed": ("decayed",),
    "superseded": ("superseded",),
    "dismissed": ("dismissed",),
    "all": ("active", "decayed", "superseded", "dismissed"),
}


def get_conn():
    """Open the reader connection, env-overridable so hermetic AppTests can point
    it at a fixture DB (do NOT cache_resource without keying on the path)."""
    return get_connection(os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH)


def _feedback_cb(conn):
    def cb(signal_id, verdict, reason_code, note):
        # record_feedback raises ValueError on missing reason (R9.1); let the
        # card surface it via st.error, never write a bad row.
        data.record_feedback(conn, signal_id, verdict, reason_code, note)
    return cb


def _render_group(conn, scope_group, statuses, legend, on_feedback):
    """Render one scope group up to its current page limit, with a Load more
    button that advances the keyset cursor by the last rendered row's key."""
    limit_key = f"limit_{scope_group}"
    limit = st.session_state.get(limit_key, PAGE_SIZE)
    rows = []
    after_key = None
    # walk pages up to the accumulated limit so Load more is stable across reruns
    remaining = limit
    while remaining > 0:
        page = data.feed_page(conn, scope_group=scope_group, after_key=after_key,
                              limit=min(PAGE_SIZE, remaining), statuses=statuses)
        rows.extend(page)
        if len(page) < min(PAGE_SIZE, remaining):
            break
        last = page[-1]
        after_key = (last["event_date"], last["signal_id"])
        remaining -= len(page)

    if scope_group == "account" and not rows:
        components.render_empty_state(
            st, "No account-scoped signals in this window — low-volume by "
            "design.")
        return

    for row in rows:
        detail = data.signal_detail(conn, row["signal_id"])
        if detail is None:
            continue
        components.render_signal_card(
            st, detail, legend, key=f"card_{row['signal_id']}",
            on_feedback=on_feedback)

    # Load more: only if the last page came back full (a further page may exist)
    if rows:
        probe = data.feed_page(
            conn, scope_group=scope_group,
            after_key=(rows[-1]["event_date"], rows[-1]["signal_id"]),
            limit=1, statuses=statuses)
        if probe:
            if st.button("Load more", key=f"more_{scope_group}"):
                st.session_state[limit_key] = limit + PAGE_SIZE
                st.rerun()


def _render_feed(conn):
    legend = data.badge_legend(conn)
    choice = st.session_state.get("status_choice", "active")
    statuses = STATUS_FILTERS[choice]
    on_feedback = _feedback_cb(conn)

    # Label both sections so the (often empty) account section reads as a
    # deliberately-empty labeled section, not a stray warning box (R7.2/R6.6).
    components.render_feed_divider(st, "Account signals")
    _render_group(conn, "account", statuses, legend, on_feedback)
    components.render_feed_divider(st, "Sector & regulatory")
    _render_group(conn, "sector", statuses, legend, on_feedback)


def main():
    st.set_page_config(page_title="GridSignals — Signal Feed", layout="wide")
    theme.inject(st)
    st.title("GridSignals")
    st.caption("Signal Feed — external events scored into Microsoft security "
               "license plays.")

    st.selectbox(
        "Status", options=list(STATUS_FILTERS.keys()), key="status_choice",
        help="Feed defaults to active; widen to see decayed / superseded / "
             "dismissed signals.")

    conn = get_conn()
    frag = getattr(st, "fragment", None)
    if frag is not None:
        @st.fragment(run_every=120)
        def _feed():
            _render_feed(conn)
        _feed()
    else:  # graceful degradation on a streamlit without st.fragment
        _render_feed(conn)


main()
