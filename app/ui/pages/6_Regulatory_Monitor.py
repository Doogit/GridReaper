"""GridSignals — Regulatory Monitor (R8.4, R7.2, D8).

A read-only view of non-graduated regulatory chatter: raw Federal Register /
NERC records that did NOT become scored signals. They are shown here VERBATIM
from the source's own record - the page never scores them, never attributes an
account, and never invents an action verb. A record graduates to the Signal
Feed only when it carries a durable compliance clock + an affected scope + a
mapped product implication + a concrete next action (D8/R7.2); everything on
this page is, by definition, missing at least one of those - so nothing here is
a signal, a card, or a score.

Reader only - this page writes nothing. Connection opens via an env-overridable
helper so hermetic AppTests point at a fixture DB.
"""
import os
import sys
import html

import streamlit as st

# streamlit runs a page with app/ui/pages/ on sys.path, not the repo root; add
# the project root (4 levels up) so `import app...` resolves on direct nav too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from app.db.connection import get_connection, DEFAULT_DB_PATH
from app.ui import components, data, theme

LIMIT = 100


def get_conn():
    return get_connection(os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH)


def _render_item(item):
    """One regulatory chatter record as a plain record (NOT a signal card):
    date + source, the source's own quoted headline, and neutral descriptive
    chips - never a scope/score badge."""
    chips = ["<span class='gs-badge'>chatter</span>"]
    if item["source_id"] == "nerc_pages":
        # nerc_pages snapshots never carry effective_on/comments_close_on, so a
        # 'no compliance clock' chip would be always-true noise - suppress the
        # clock chip and label the record with its 'page snapshot' type instead.
        chips.append(
            f"<span class='gs-badge'>{html.escape(item['doc_type_label'])}</span>")
    elif item["has_anchor"] and item["comments_close_on"]:
        chips.append("<span class='gs-badge'>comment period closes "
                     f"{html.escape(str(item['comments_close_on']))}</span>")
    elif item["has_anchor"] and item["effective_on"]:
        chips.append("<span class='gs-badge'>effective "
                     f"{html.escape(str(item['effective_on']))}</span>")
    else:
        chips.append("<span class='gs-badge'>no compliance clock</span>")

    meta = " &middot; ".join(b for b in (
        html.escape(str(item["event_date"] or "")),
        html.escape(item["source_name"] or item["source_id"]),
    ) if b)

    st.markdown(
        "<div class='gs-record'>"
        f"<div class='gs-headline'>{html.escape(item['headline'])}</div>"
        f"<div class='gs-meta'>{meta}</div>"
        f"<div class='gs-badges'>{''.join(chips)}</div>"
        "</div>",
        unsafe_allow_html=True)
    if item["url"]:
        st.markdown(f"[Source]({item['url']})")


def main():
    st.set_page_config(
        page_title="GridSignals — Regulatory Monitor", layout="wide")
    theme.inject(st)
    st.title("Regulatory Monitor")
    st.caption("Raw regulatory records that did NOT graduate to the Signal "
               "Feed — not scored signals.")
    st.info(
        "These are raw Federal Register / NERC records, **NOT** scored "
        "signals. A record graduates to the Signal Feed only when it carries a "
        "durable compliance clock, an affected scope, a mapped product "
        "implication, and a concrete next action (D8/R7.2). Everything here is "
        "shown verbatim from the source — no score, no account, no product "
        "implication.")

    conn = get_conn()
    items = data.regulatory_monitor(conn, limit=LIMIT)
    if not items:
        components.render_empty_state(
            st, "No non-graduated regulatory chatter — either nothing has been "
            "ingested from the Federal Register / NERC yet, or every record "
            "graduated to the Signal Feed.")
        return

    for item in items:
        _render_item(item)


main()
