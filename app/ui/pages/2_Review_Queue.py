"""GridSignals — Review Queue / Triage (R8.2, R10.3/G2, R10.7).

The operator's triage surface. Three sections, each with an honest empty state:

1. Pending entity matches — candidates the resolver could not decide, surfaced
   with the resolver's decision-trail ``reason`` (not a fabricated matched/
   rejected term list — those are not persisted for review candidates), its
   confidence, an evidence snippet, and a source link. Accept/Reject writes a
   human ``entity_match_decisions`` row + the ``review_queue`` disposition via
   ``data.triage_decision`` (the only write path here). Approving records the
   decision only; creating a signal from an approved match is deferred this
   chunk (documented on the page).
2. Source health — every source policy classified error / never_run / stale /
   disabled / ok via ``data.source_state``, error state shown verbatim (R10.3).
3. Stale license facts — read-only list of facts whose verification is older
   than 180 days (R10.7); the count is shown in the section header.

This page is a reader except for ``data.triage_decision``. UTC ISO-8601 (R10.2).
Connection opens via an env-overridable helper so hermetic AppTests can point it
at a fixture DB.
"""
import os
import sys

import streamlit as st

# streamlit runs a page with app/ui/pages/ on sys.path, not the repo root; add
# the project root (4 levels up) so `import app...` resolves on direct nav too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from app.db.connection import get_connection, DEFAULT_DB_PATH
from app.ui import data, theme

# Operator-legible status presentation for source_state() (R10.3/G2). Each state
# must be visually distinct so error vs never-run vs stale are not confused.
SOURCE_STATE_DISPLAY = {
    "ok": ("🟢", "OK"),
    "stale": ("🟡", "STALE"),
    "never_run": ("🔵", "NEVER RUN"),
    "error": ("🔴", "ERROR"),
    "disabled": ("⚪", "DISABLED"),
}

STALE_FACT_WINDOW_DAYS = 180


def get_conn():
    return get_connection(os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH)


def _render_pending(conn):
    st.subheader("Pending entity matches")
    st.caption(
        "Approving records the human decision only — creating a signal from an "
        "approved match is deferred this chunk (R8.2).")
    pending = data.review_pending(conn)
    if not pending:
        st.markdown(
            "<div class='gs-empty'>0 pending — the resolver needed no human "
            "help. Every event this window matched (or was rejected) with "
            "enough confidence to skip review.</div>",
            unsafe_allow_html=True)
        return

    for item in pending:
        rowid = item["rowid"]
        subsector = f" · {item['subsector']}" if item.get("subsector") else ""
        conf = item.get("confidence")
        conf_txt = f"{conf:.2f}" if isinstance(conf, (int, float)) else "n/a"
        with st.container(border=True):
            st.markdown(
                f"<div class='gs-headline'>{item.get('candidate_name') or item['candidate_entity_id']}"
                f"<span class='gs-meta'>{subsector}</span></div>",
                unsafe_allow_html=True)
            # The resolver's decision-trail label, surfaced honestly (no
            # fabricated matched/rejected terms — not persisted for candidates).
            st.markdown(
                f"<div class='gs-meta'>reason: <code>{item.get('reason') or 'unknown'}</code>"
                f" · confidence {conf_txt} · event {item.get('event_date') or 'n/a'}</div>",
                unsafe_allow_html=True)
            if item.get("snippet"):
                st.markdown(
                    f"<div class='gs-evidence'>{item['snippet']}</div>",
                    unsafe_allow_html=True)
            if item.get("source_url"):
                st.markdown(f"[source]({item['source_url']})")
            accept_col, reject_col = st.columns(2)
            if accept_col.button("Accept", key=f"accept_{rowid}"):
                data.triage_decision(
                    conn, item["raw_event_id"], item["candidate_entity_id"],
                    accept=True)
                st.rerun()
            if reject_col.button("Reject", key=f"reject_{rowid}"):
                data.triage_decision(
                    conn, item["raw_event_id"], item["candidate_entity_id"],
                    accept=False)
                st.rerun()


def _render_source_health(conn):
    st.subheader("Source health")
    st.caption("Ingestion status per source (R10.3): error / never-run / stale "
               "vs ttl / disabled / ok.")
    rows = data.source_health(conn)
    if not rows:
        st.markdown(
            "<div class='gs-empty'>No sources configured yet.</div>",
            unsafe_allow_html=True)
        return
    for row in rows:
        state = data.source_state(row)
        emoji, label = SOURCE_STATE_DISPLAY.get(state, ("⚪", state.upper()))
        st.markdown(
            f"**{emoji} {label}** · {row['name']} "
            f"<span class='gs-meta'>(`{row['source_id']}`)</span>",
            unsafe_allow_html=True)
        if state == "error" and row["last_error_state"]:
            # Verbatim error text — the operator needs the raw upstream failure.
            st.markdown(
                f"<div class='gs-gov-caution'>{row['last_error_state']}</div>",
                unsafe_allow_html=True)
        last_success = row["last_success_at"] or "never"
        st.markdown(
            f"<div class='gs-meta'>last success: {last_success} · "
            f"ttl: {row['ttl']}s</div>",
            unsafe_allow_html=True)


def _render_stale_facts(conn):
    stale = data.stale_facts(conn, days=STALE_FACT_WINDOW_DAYS)
    st.subheader(f"Stale license facts ({len(stale)})")
    st.caption(f"License facts unverified for more than {STALE_FACT_WINDOW_DAYS} "
               "days (R10.7). Read-only — refresh happens in the licensing pass.")
    if not stale:
        st.markdown(
            "<div class='gs-empty'>0 stale facts — every license fact was "
            "verified within the window.</div>",
            unsafe_allow_html=True)
        return
    for fact in stale:
        age = fact.get("age_days")
        age_txt = f"{age}d old" if age is not None else "unverified"
        segment = fact.get("segment") or ""
        sku = fact.get("sku_or_plan") or ""
        st.markdown(
            f"**{fact.get('product_name') or fact['product_id']}** "
            f"<span class='gs-meta'>{sku} · {segment} · "
            f"{fact.get('source_quality') or ''} · verified "
            f"{fact.get('verified_date') or 'never'} · {age_txt}</span>",
            unsafe_allow_html=True)


def main():
    st.set_page_config(page_title="GridSignals — Review Queue", layout="wide")
    theme.inject(st)
    st.title("Review Queue")
    st.caption("Triage pending matches, watch source health, spot stale facts.")

    conn = get_conn()
    _render_pending(conn)
    st.divider()
    _render_source_health(conn)
    st.divider()
    _render_stale_facts(conn)


main()
