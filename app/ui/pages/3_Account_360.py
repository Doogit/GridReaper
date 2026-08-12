"""GridSignals — Account 360 (R8.3 skeleton).

A per-entity view for the watchlist (~171 entities). Header: name, identifiers,
subsector, richness, a coverage badge (dark accounts read "low coverage", never
"no activity", R6.6), gov-cloud posture (R7.10), and parent/children from
entity_relationships. Two tabs only — Timeline and Signals — reusing the one
card component (components.render_signal_card). Deferred tabs (Entity Graph,
Compliance Calendar, Products) are NOT rendered as empty shells (R8 bans them);
they are omitted.

Zero account-scoped signals is the norm today (171 entities, 0 account signals):
the page stays useful from identifiers, relationships, and gov-cloud posture,
and the tabs carry an honest low-volume-by-design empty state rather than a
broken look. The app is a reader; the only write path here is feedback
(data.record_feedback, R9.1). Cards read snapshots, never live facts (R7.6).
Timestamps are UTC ISO-8601 (R10.2).
"""
import os
import html

import streamlit as st

from app.db.connection import get_connection, DEFAULT_DB_PATH
from app.ui import components, data, theme

# Entity identifier columns surfaced in the header, in display order. Rendered
# only when the entity Row actually carries a value (many entities are sparse).
_ID_FIELDS = (
    ("cik", "CIK"),
    ("lei", "LEI"),
    ("wikidata_qid", "Wikidata"),
    ("ticker", "Ticker"),
)


def get_conn():
    return get_connection(os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH)


def _entities(conn):
    """(entity_id, name) for every watchlist entity, name-ordered — the pool
    the selector searches over."""
    return conn.execute(
        "SELECT entity_id, name FROM watchlist_entities ORDER BY name"
    ).fetchall()


def _row_get(row, key):
    """sqlite3.Row has no .get(); return the value or None if column absent."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _render_header(container, header):
    entity = header["entity"]
    name = entity["name"] or entity["entity_id"]
    container.subheader(name)

    # identifiers present on this entity (skip the sparse/empty ones)
    ids = []
    for col, label in _ID_FIELDS:
        val = _row_get(entity, col)
        if val is not None and str(val).strip():
            ids.append(f"{label} {str(val).strip()}")
    ids.append(f"entity_id {entity['entity_id']}")
    container.caption(" · ".join(ids))

    subsector = (_row_get(entity, "subsector") or "unknown")
    richness = (_row_get(entity, "richness") or "unknown")
    coverage = (_row_get(entity, "coverage_flag") or "unknown")

    # coverage badge — dark accounts read "low coverage", never "no activity"
    if coverage == "dark":
        cov_badge = ("<span class='gs-badge coverage-dark'>low coverage "
                     "(dark)</span>")
    else:
        cov_badge = (f"<span class='gs-badge'>coverage: "
                     f"{html.escape(str(coverage))}</span>")
    container.markdown(
        "<div class='gs-badges'>"
        f"<span class='gs-badge scope'>{html.escape(str(subsector))}</span>"
        f"<span class='gs-badge'>richness: {html.escape(str(richness))}</span>"
        f"{cov_badge}"
        "</div>",
        unsafe_allow_html=True)

    # gov-cloud posture (R7.10)
    gov = (_row_get(entity, "gov_cloud_likelihood") or "unknown")
    tenant = (_row_get(entity, "tenant_cloud_environment") or "unknown")
    container.markdown(
        f"<div class='gs-meta'>Gov-cloud likelihood: "
        f"<strong>{html.escape(str(gov))}</strong> · tenant environment: "
        f"{html.escape(str(tenant))}</div>",
        unsafe_allow_html=True)

    # relationships from entity_relationships
    parent = header.get("parent")
    children = header.get("children") or []
    if parent is not None:
        container.markdown(
            f"<div class='gs-meta'>Parent: "
            f"{html.escape(str(parent['name']))}</div>",
            unsafe_allow_html=True)
    if children:
        names = ", ".join(html.escape(str(c["name"])) for c in children)
        container.markdown(
            f"<div class='gs-meta'>Subsidiaries: {names}</div>",
            unsafe_allow_html=True)
    if parent is None and not children:
        container.markdown(
            "<div class='gs-meta'>No mapped parent or subsidiaries.</div>",
            unsafe_allow_html=True)


def _render_timeline(container, signals):
    """Compact chronological list: date + headline + scope."""
    if not signals:
        components.render_empty_state(
            container,
            "No account-scoped signals yet for this account — low volume by "
            "design. When an external event maps to this entity it appears "
            "here, newest first.")
        return
    rows = []
    for s in signals:
        date = html.escape(str(s["event_date"] or ""))
        headline = html.escape(str(s["headline"] or ""))
        scope = html.escape(str(s["signal_scope"] or ""))
        rows.append(
            "<div class='gs-evidence'>"
            f"<span class='gs-locator'>{date}</span> &nbsp; {headline} "
            f"<span class='gs-badge scope'>{scope}</span>"
            "</div>")
    container.markdown("".join(rows), unsafe_allow_html=True)


def _render_signals(container, conn, signals, legend, on_feedback):
    """Full cards, one per account signal, reusing the feed's card component."""
    if not signals:
        components.render_empty_state(
            container,
            "No account-scoped signal cards yet — low volume by design. This "
            "account currently surfaces only through its profile above.")
        return
    for s in signals:
        detail = data.signal_detail(conn, s["signal_id"])
        if detail is None:
            continue
        components.render_signal_card(
            container, detail, legend,
            key=f"acct_{s['signal_id']}", on_feedback=on_feedback)


def main():
    st.set_page_config(page_title="GridSignals — Account 360", layout="wide")
    theme.inject(st)
    st.title("Account 360")
    st.caption("Per-account signal history, relationships, and gov-cloud posture")

    conn = get_conn()
    entities = _entities(conn)
    if not entities:
        components.render_empty_state(
            st, "No watchlist entities loaded. Run the seed loader first.")
        return

    labels = {f"{e['name']}  ·  {e['entity_id']}": e["entity_id"]
              for e in entities}
    choice = st.selectbox("Account", list(labels.keys()), index=0)
    if not choice:
        return
    entity_id = labels[choice]

    header = data.account_header(conn, entity_id)
    if header is None:
        components.render_empty_state(
            st, "That account could not be loaded.")
        return

    _render_header(st, header)

    signals = data.account_signals(conn, entity_id)
    legend = data.badge_legend(conn)

    def on_feedback(signal_id, verdict, reason_code, note):
        try:
            data.record_feedback(conn, signal_id, verdict, reason_code, note)
        except ValueError as exc:
            st.warning(str(exc))

    timeline_tab, signals_tab = st.tabs(["Timeline", "Signals"])
    _render_timeline(timeline_tab, signals)
    _render_signals(signals_tab, conn, signals, legend, on_feedback)


main()
