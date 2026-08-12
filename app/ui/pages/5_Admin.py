"""GridSignals — Admin / Config (R8.7; R3.3, R3.5, R7.4, R10.7).

The operator's tuning surface and the app's FIRST write path into seeded config
tables. Four sections:

1. Scoring weights (R7.5) — one number box + Save per weight row. Saving edits
   scoring_weights.weight and re-runs scoring ACTIVE-ONLY, so live cards move but
   decayed/dismissed/superseded cards keep their frozen score (R8.1).
2. Decay half-lives (R7.4) — one box + Save per trigger. Half-lives are
   prioritization heuristics, not measurements (caption states this); a save
   rescopes active cards the same way.
3. Source policies (R8.7 review) — enable/disable a source, with its Gate G2
   demotion *recommendation* (report-only, R9.5) shown alongside so the operator
   sees WHY before acting. Sources are never disabled automatically.
4. License-fact staleness (R10.7, read-only) — verified_date older than 180 days
   flagged; labels state only what the field says ("verified 210 days ago" /
   "verification date unknown"), never an interpretive "unverified".

Plus a recent-changes panel reading the config_audit provenance trail (R3.3).

Every save takes the single-writer ingestion lock (R3.2) via
data.config_write_conn() INSIDE the button handler (Streamlit reruns top-to-
bottom, so the lock must never be held at render). A save that races an
ingestion/scoring run surfaces "ingestion in progress", never a crash. Invalid
input surfaces the validation error and writes nothing. UTC ISO-8601 (R10.2).
"""
import os
import sys
import html
import json

import streamlit as st

# streamlit runs a page with app/ui/pages/ on sys.path, not the repo root; add
# the project root (4 levels up) so `import app...` resolves on direct nav too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from app.db.connection import get_connection, DEFAULT_DB_PATH
from app.ui import data, theme

STALE_FACT_WINDOW_DAYS = 180

# Friendly headers for the scoring_weights.weight_kind groups (R7.5).
WEIGHT_KIND_LABELS = {
    "subsector": "Subsector fit",
    "richness": "Account richness",
    "coverage": "Coverage",
    "applicability": "Regulatory applicability",
    "scope": "Signal scope fit",
}


def _esc(value):
    return html.escape("" if value is None else str(value))


def get_conn():
    return get_connection(os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH)


def _flash():
    """Render and clear a one-shot message stashed across a save's st.rerun()."""
    msg = st.session_state.pop("_admin_flash", None)
    if not msg:
        return
    level, text = msg
    getattr(st, level)(text)


def _save(fn, *args, **kwargs):
    """Run a config-write helper under the single-writer lock, stash a flash
    message, and rerun. Returns True on success. Validation errors render inline
    (no rerun) so the bad input stays visible; a held lock becomes a warning."""
    try:
        with data.config_write_conn() as wconn:
            result = fn(wconn, *args, **kwargs)
    except RuntimeError:
        st.session_state["_admin_flash"] = (
            "warning", "Ingestion in progress — the single-writer lock is held. "
            "Try again once the run finishes.")
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))
        return False
    level = "success" if result.get("changed", True) else "info"
    st.session_state["_admin_flash"] = (level, _result_msg(fn.__name__, result))
    st.rerun()


def _enabled_label(value):
    return "Enabled" if value else "Disabled"


def _result_msg(fn_name, result):
    if not result.get("changed", True):
        # No-op save (new == old): nothing was written or rescored.
        if fn_name == "set_source_enabled":
            return f"No change — source already {_enabled_label(result['new'])}."
        return f"No change — value already {result['new']}."
    if fn_name == "set_source_enabled":
        return (f"Saved: {_enabled_label(result['old'])} → "
                f"{_enabled_label(result['new'])}.")
    if "scored" in result:
        return (f"Saved: {result['old']} → {result['new']}. "
                f"{result['scored']} active card(s) rescored, "
                f"{result['decayed']} decayed.")
    return f"Saved: {result['old']} → {result['new']}."


# -- scoring weights ---------------------------------------------------------

def _render_weights(conn, reason):
    st.subheader("Scoring weights")
    st.caption(
        "Account-fit and scope-fit multipliers (R7.5). Saving a weight re-runs "
        "scoring on active cards only — decayed and dismissed cards keep their "
        "frozen score. Unknown keys already fall back to a neutral 1.0.")
    rows = conn.execute(
        "SELECT weight_kind, key, weight FROM scoring_weights "
        "ORDER BY weight_kind, key").fetchall()
    if not rows:
        st.markdown("<div class='gs-empty'>No scoring weights seeded.</div>",
                    unsafe_allow_html=True)
        return
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["weight_kind"], []).append(r)
    for kind, kind_rows in by_kind.items():
        st.markdown(f"**{_esc(WEIGHT_KIND_LABELS.get(kind, kind))}** "
                    f"<span class='gs-meta'>({_esc(kind)})</span>",
                    unsafe_allow_html=True)
        for r in kind_rows:
            box_col, btn_col = st.columns([4, 1])
            new_val = box_col.number_input(
                f"{kind} · {r['key']}", value=float(r["weight"]),
                min_value=0.0, step=0.05, format="%.2f",
                help="Multiplier; typical 0.5–1.5, and 1.0 is neutral. "
                     "Unknown/blank keys already fall back to 1.0.",
                key=f"w_{kind}_{r['key']}")
            if btn_col.button("Save", key=f"wsave_{kind}_{r['key']}"):
                _save(data.update_weight, kind, r["key"], new_val, reason=reason)


# -- decay half-lives --------------------------------------------------------

def _render_half_lives(conn, reason):
    st.subheader("Decay half-lives")
    st.caption(
        "Per-trigger decay half-life in days (R7.4). These are prioritization "
        "heuristics, not measurements — tuning one re-prioritizes active cards, "
        "it does not claim a measured decay rate.")
    rows = conn.execute(
        "SELECT trigger_id, name, decay_half_life_days FROM triggers "
        "ORDER BY trigger_id").fetchall()
    if not rows:
        st.markdown("<div class='gs-empty'>No triggers seeded.</div>",
                    unsafe_allow_html=True)
        return
    for r in rows:
        box_col, btn_col = st.columns([4, 1])
        new_val = box_col.number_input(
            f"{r['name']} ({r['trigger_id']}) — half-life days",
            value=int(r["decay_half_life_days"]), min_value=1, max_value=3650,
            step=1,
            help="Days for a card's decay factor to halve. Typical 30–600 "
                 "(new-CISO ~90d, regulation ~540–730d). Heuristic, not measured.",
            key=f"hl_{r['trigger_id']}")
        if btn_col.button("Save", key=f"hlsave_{r['trigger_id']}"):
            _save(data.update_half_life, r["trigger_id"], new_val, reason=reason)


# -- source policies ---------------------------------------------------------

def _render_sources(conn, reason):
    st.subheader("Source policies")
    st.caption(
        "Enable or disable a source (R8.7). Gate G2 (R9.5) recommends demotion "
        "when a source's precision falls below 40% over enough rated cards — a "
        "recommendation to REVIEW, never an auto-action. Full per-dimension "
        "rates and judge-vs-human disagreement are on the Precision page.")
    rows = data.source_policy_rows(conn)
    if not rows:
        st.markdown("<div class='gs-empty'>No sources configured.</div>",
                    unsafe_allow_html=True)
        return
    for r in rows:
        sid = r["source_id"]
        st.markdown(
            f"**{_esc(r['name'])}** "
            f"<span class='gs-meta'>(`{_esc(sid)}`) · state: "
            f"{_esc(r['state'])} · evidence rank {_esc(r['evidence_rank'])}"
            "</span>", unsafe_allow_html=True)
        g2 = r.get("g2")
        if g2:
            flag = "DEMOTE?" if g2["demote_recommended"] else "ok"
            st.markdown(
                f"<div class='gs-meta'>G2 <span class='gs-badge'>{flag}</span> "
                f"{_esc(g2['note'])}</div>", unsafe_allow_html=True)
            # Reason codes at the decision point, not one page away (persona pass).
            codes = g2.get("reason_codes") or {}
            if codes:
                codes_txt = ", ".join(
                    f"{_esc(code)}={n}" for code, n in codes.items())
                st.markdown(
                    f"<div class='gs-meta'>reason codes: {codes_txt}</div>",
                    unsafe_allow_html=True)
        else:
            st.markdown(
                "<div class='gs-meta'>G2: no rated feedback for this source yet "
                "— rate its cards useful / not-useful to build a signal.</div>",
                unsafe_allow_html=True)
        tog_col, btn_col = st.columns([4, 1])
        enabled = tog_col.toggle(
            "Enabled", value=bool(r["enabled"]), key=f"src_{sid}")
        if btn_col.button("Save", key=f"srcsave_{sid}"):
            _save(data.set_source_enabled, sid, enabled, reason=reason)


# -- license-fact staleness (read-only) --------------------------------------

def _render_staleness(conn):
    stale = data.stale_facts(conn, days=STALE_FACT_WINDOW_DAYS)
    st.subheader(f"License-fact staleness ({len(stale)})")
    st.caption(
        f"License facts whose verified_date is older than {STALE_FACT_WINDOW_DAYS} "
        "days (R10.7). Read-only here — editing license facts is a later chunk.")
    if not stale:
        st.markdown(
            "<div class='gs-empty'>0 stale facts — every license fact was "
            "verified within the window.</div>", unsafe_allow_html=True)
        return
    for fact in stale:
        age = fact.get("age_days")
        # Literal, field-stating labels only (never an interpretive "unverified").
        age_txt = (f"verified {age} days ago" if age is not None
                   else "verification date unknown")
        product = fact.get("product_name") or fact.get("product_id")
        st.markdown(
            f"**{_esc(product)}** <span class='gs-meta'>"
            f"{_esc(fact.get('sku_or_plan'))} · {_esc(fact.get('segment'))} · "
            f"{_esc(age_txt)}</span>", unsafe_allow_html=True)


# -- recent config changes (provenance) --------------------------------------

def _humanize_pk(table_name, pk):
    """scoring_weights stores a JSON composite pk; render it as 'kind · key'
    so the provenance table reads cleanly (persona pass). Other tables use a
    bare pk already."""
    if table_name == "scoring_weights":
        try:
            d = json.loads(pk)
            return f"{d['weight_kind']} · {d['key']}"
        except (ValueError, KeyError, TypeError):
            return pk
    return pk


def _render_audit(conn):
    st.subheader("Recent config changes")
    st.caption("Provenance trail (R3.3): every Admin edit, newest first.")
    tail = data.config_audit_tail(conn, limit=25)
    if not tail:
        st.markdown(
            "<div class='gs-empty'>No config edits yet.</div>",
            unsafe_allow_html=True)
        return
    st.table([{
        "when": r["ts"],
        "table": r["table_name"],
        "row": _humanize_pk(r["table_name"], r["pk"]),
        "field": r["field"],
        "old → new": f"{r['old_value']} → {r['new_value']}",
        "reason": r["reason"] or "",
    } for r in tail])


def main():
    st.set_page_config(page_title="GridSignals — Admin", layout="wide")
    theme.inject(st)
    st.title("Admin / Config")
    st.caption(
        "Tune scoring and source policies. Every edit is recorded to an audit "
        "trail and re-prioritizes live cards; seed files stay untouched.")
    _flash()

    conn = get_conn()
    try:
        st.caption(
            "Your edits persist across `python -m app.db.load_seeds` (the seed CSVs "
            "are never touched); a fresh rebuild-from-seeds restores the pristine "
            "defaults. Every save is logged in Recent config changes below.")
        reason = st.text_input(
            "Reason for the next edit (optional)", value="", key="admin_reason",
            help="Attached to the audit-trail entry of whatever you save next.")

        _render_weights(conn, reason)
        st.divider()
        _render_half_lives(conn, reason)
        st.divider()
        _render_sources(conn, reason)
        st.divider()
        _render_staleness(conn)
        st.divider()
        _render_audit(conn)
    finally:
        conn.close()


main()
