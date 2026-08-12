"""GridSignals — Feedback / Precision (R8.6, R9.3-R9.5, R9.12).

The operator's QA surface. Every number here is a *precision / quality* metric —
judgment consistency (human useful-rate) and evidence accuracy (automated judge
accuracy). It is explicitly NOT validated sales lift (PRD §1): at MVP feedback
verdicts are the operator's own self-ratings, so a high useful-rate means "the
operator kept finding these cards worth acting on", never "these closed deals".
The page states this in a prominent caption (trust line, persona Renn).

The computation lives in app.audit.precision (pure, tested); this page only
fetches rows (app.ui.data.precision_*) and renders them. The TRUST INVARIANT is
carried through to the DOM: a rate is never shown as a bare percentage — every
rate ships with its sample size, and a rate over an empty denominator renders as
an honest "n/a" / low-n message, never a fabricated 0% or a fake gauge. Below
the G1 sample floor (n<20) the headline shows the low-n message instead of a
gauge. With 0 audit verdicts the audit sections say so and point at the runner.

Sections (each with an honest empty/low-n state): framing caption, headline
useful-rate + auto-accuracy, G1 status (account triggers, with sector /
regulatory / unconfirmed reported SEPARATELY so they can't inflate account
precision — R9.4), G2 per-source demotion *recommendations* (report-only,
R9.5), useful-rate + auto-accuracy tables by dimension (R9.3), reason-code
distribution (R9.2), judge-vs-human disagreement, half-life effectiveness, and
the audit run history (R9.12 skip/budget legibility).

Reader only — this page writes nothing. UTC ISO-8601 (R10.2). Connection opens
via an env-overridable helper so hermetic AppTests point at a fixture DB.
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
from app.ui import data, theme
from app.audit import precision

# G1's rated-card floor: below this we never draw a gauge, only the low-n copy.
MIN_SAMPLE = precision.G1_MIN_RATED  # 20

# The R9.3 dimensions and a friendly column header for each dimension value col.
DIMENSION_LABELS = {
    "trigger": "Trigger",
    "source": "Source",
    "signal_scope": "Signal scope",
    "incident_evidence_level": "Incident tier",
}


def _esc(value):
    return html.escape("" if value is None else str(value))


def get_conn():
    return get_connection(os.environ.get("GRIDSIGNALS_DB") or DEFAULT_DB_PATH)


def _pct(rate):
    """A rate as a percent string, or the honest 'n/a' when it is None."""
    return "n/a" if rate is None else f"{rate:.0%}"


def _rate_with_n(rate, n, unit="rated"):
    """'72% (n=25)' when populated; 'n/a (0 rated)' when empty. The n ALWAYS
    travels with the rate — the trust invariant carried to the DOM."""
    if rate is None:
        return f"n/a ({n} {unit})"
    return f"{rate:.0%} (n={n})"


def _empty(msg):
    st.markdown(f"<div class='gs-empty'>{_esc(msg)}</div>",
                unsafe_allow_html=True)


# -- framing + headline ------------------------------------------------------

def _render_framing():
    st.caption(
        "These are precision / QA metrics — judgment consistency (human "
        "useful-rate) and evidence accuracy (automated judge accuracy). They "
        "are NOT validated sales lift: at MVP, feedback verdicts are the "
        "operator's own self-ratings, so a high useful-rate means the operator "
        "kept finding cards worth acting on, never that they closed deals. "
        "Every rate is shown with its sample size; an empty denominator reads "
        "as 'not enough evidence yet', never 0%.")


def _render_headline(feedback_rows, audit_rows):
    st.subheader("Precision headline")
    ur = precision.useful_rate_overall(feedback_rows)
    aa = precision.auto_accuracy(audit_rows, "trigger")["overall"]

    col1, col2 = st.columns(2)
    # Human useful-rate: only draw a gauge above the G1 sample floor; below it,
    # show the low-n message so we never present a fake percentage.
    with col1:
        if ur["total"] >= MIN_SAMPLE and ur["rate"] is not None:
            col1.metric("Human useful-rate", _pct(ur["rate"]),
                        help=f"positives over {ur['total']} rated cards")
            st.caption(f"n={ur['total']} rated cards")
        else:
            col1.metric("Human useful-rate", "n/a")
            _empty(
                f"not enough rated cards yet (n={ur['total']}<{MIN_SAMPLE}) — "
                "no gauge until at least the G1 floor of rated feedback.")
    # Auto-accuracy: 0 verdicts is the current DB state — say so and point at
    # the runner, never a 0%.
    with col2:
        if aa["scored"] > 0 and aa["accuracy"] is not None:
            col2.metric("Auto-accuracy (judge)", _pct(aa["accuracy"]),
                        help=f"pass over {aa['scored']} scored checks")
            st.caption(f"n={aa['scored']} scored checks "
                       "(entity_match + evidence_support)")
        else:
            col2.metric("Auto-accuracy (judge)", "n/a")
            _empty(
                "0 audit verdicts yet — run `python -m app.audit.judge` to "
                "score entity_match + evidence_support.")


# -- G1 (account gate) -------------------------------------------------------

def _render_g1(feedback_rows, audit_rows):
    st.subheader("Gate G1 — account-level precision")
    st.caption(
        "Stage 1 -> 2 gate (R9.4): on BOTH primary account triggers, human "
        "useful-rate >= 60% AND auto-accuracy >= 80%, over >= 30 days AND "
        ">= 20 rated account-specific cards. ONLY account-specific cards "
        "(entity present, account/parent scope) count here.")
    g1 = precision.g1_status(feedback_rows, audit_rows)

    for tid, m in g1["triggers"].items():
        meets = m["meets"]
        badge = "MEETS" if meets else "BLOCKED"
        st.markdown(
            f"**{_esc(tid)}** — <span class='gs-badge'>{badge}</span>",
            unsafe_allow_html=True)
        st.markdown(
            "<div class='gs-meta'>"
            f"useful-rate: {_esc(_rate_with_n(m['useful_rate'], m['useful_n']))}"
            f" &middot; auto-accuracy: "
            f"{_esc(_rate_with_n(m['auto_accuracy'], m['auto_n'], 'scored'))}"
            f" &middot; span: {m['days_span']:.1f}d</div>",
            unsafe_allow_html=True)
        if not meets:
            reasons = "; ".join(m["reasons"]) or "insufficient evidence"
            st.markdown(
                f"<div class='gs-gov-caution'>blocked: {_esc(reasons)}</div>",
                unsafe_allow_html=True)

    if g1["eligible"]:
        st.markdown(
            "<div class='gs-meta'>Overall: <b>ELIGIBLE</b> — both primary "
            "triggers meet the gate.</div>", unsafe_allow_html=True)
    else:
        st.markdown(
            "<div class='gs-meta'>Overall: <b>not eligible</b> — see blocked "
            "reasons above.</div>", unsafe_allow_html=True)

    # Reported SEPARATELY so sector / regulatory / unconfirmed cards can never
    # be read as account precision (R9.4).
    rep = g1["reported_separately"]
    fb, au = rep["feedback"], rep["auto"]
    st.markdown(
        "<div class='gs-divider'>reported separately (not account precision)"
        "</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='gs-meta'>Sector / regulatory-calendar / unconfirmed "
        "early-warning cards — kept out of the account gate so they cannot "
        "inflate or hide it. "
        f"useful-rate: {_esc(_rate_with_n(fb['rate'], fb['total']))} &middot; "
        f"auto-accuracy: "
        f"{_esc(_rate_with_n(au['accuracy'], au['scored'], 'scored'))}</div>",
        unsafe_allow_html=True)


# -- G2 (per-source, report-only) --------------------------------------------

def _render_g2(feedback_rows):
    st.subheader("Gate G2 — per-source demotion recommendations")
    st.caption(
        "Report-only (R9.5): < 40% precision over a source's rated cards is a "
        "recommendation to REVIEW (consider store-only / disable), never an "
        "auto-action — sources are never disabled automatically. A "
        "recommendation needs >= 20 rated cards; below that the sample is too "
        "noisy to act on.")
    g2 = precision.g2_status(feedback_rows)
    if not g2:
        _empty("0 rated cards attributed to any source yet — nothing to "
               "evaluate for demotion.")
        return
    for sid, m in g2.items():
        flag = "DEMOTE?" if m["demote_recommended"] else "ok"
        label = _esc(sid if sid is not None else "(no source)")
        st.markdown(
            f"**{label}** — <span class='gs-badge'>{flag}</span> "
            f"<span class='gs-meta'>precision "
            f"{_esc(_rate_with_n(m['precision'], m['n']))}</span>",
            unsafe_allow_html=True)
        st.markdown(
            f"<div class='gs-meta'>{_esc(m['note'])}</div>",
            unsafe_allow_html=True)
        if m["reason_codes"]:
            codes = ", ".join(
                f"{_esc(code)}={n}" for code, n in m["reason_codes"].items())
            st.markdown(
                f"<div class='gs-meta'>reason codes: {codes}</div>",
                unsafe_allow_html=True)


# -- dimension tables --------------------------------------------------------

def _render_useful_tables(feedback_rows):
    st.subheader("Useful-rate by dimension")
    st.caption("Human useful-rate sliced by trigger / source / signal-scope / "
               "incident tier (R9.3). Each cell carries its sample size.")
    any_rows = False
    for dimension in precision.DIMENSIONS:
        sliced = precision.useful_rate(feedback_rows, dimension)
        # 'overall' is always present; a dimension with no rated rows has only
        # 'overall' and we skip its table (the headline already shows overall).
        values = [k for k in sliced if k != "overall"]
        if not values:
            continue
        any_rows = True
        st.markdown(f"**{_esc(DIMENSION_LABELS[dimension])}**")
        table = []
        for value in values:
            b = sliced[value]
            table.append({
                DIMENSION_LABELS[dimension]: value
                if value is not None else "(none)",
                "useful-rate": _rate_with_n(b["rate"], b["total"]),
            })
        st.table(table)
    if not any_rows:
        _empty("0 rated cards yet — no per-dimension useful-rate to show. Rate "
               "cards useful / not-useful from the feed to populate this.")


def _render_auto_tables(audit_rows):
    st.subheader("Auto-accuracy by dimension")
    st.caption("Automated judge accuracy (entity_match + evidence_support, "
               "pass over pass+fail) sliced the same way (R9.3). Each cell "
               "carries its sample size.")
    any_rows = False
    for dimension in precision.DIMENSIONS:
        sliced = precision.auto_accuracy(audit_rows, dimension)
        values = [k for k in sliced if k != "overall"]
        if not values:
            continue
        any_rows = True
        st.markdown(f"**{_esc(DIMENSION_LABELS[dimension])}**")
        table = []
        for value in values:
            b = sliced[value]
            table.append({
                DIMENSION_LABELS[dimension]: value
                if value is not None else "(none)",
                "auto-accuracy": _rate_with_n(
                    b["accuracy"], b["scored"], "scored"),
            })
        st.table(table)
    if not any_rows:
        _empty("0 audit verdicts yet — run `python -m app.audit.judge` to "
               "populate per-dimension auto-accuracy.")


# -- reason codes ------------------------------------------------------------

def _render_reason_codes(feedback_rows):
    st.subheader("Reason-code distribution")
    st.caption("Why cards were marked not-useful (R9.2), most common first.")
    dist = precision.reason_code_distribution(feedback_rows)
    if not dist:
        _empty("0 not-useful ratings yet — no reason codes to distribute.")
        return
    st.table([{"reason code": d["reason_code"], "count": d["count"]}
              for d in dist])


# -- judge vs human ----------------------------------------------------------

def _render_disagreement(audit_rows, feedback_rows):
    st.subheader("Judge vs human")
    st.caption(
        "A QA signal, not ground truth: the judge rates objective facts, the "
        "human rates usefulness — some disagreement is expected. Only signals "
        "with BOTH sides are comparable.")
    d = precision.judge_human_disagreement(audit_rows, feedback_rows)
    if d["comparable"] == 0:
        _empty("nothing comparable yet — a signal needs both an "
               "entity_match/evidence_support verdict AND human feedback.")
        return
    st.markdown(
        "<div class='gs-meta'>"
        f"comparable: {d['comparable']} &middot; agree: {d['agree']} &middot; "
        f"disagree: {d['disagree']} &middot; disagreement rate: "
        f"{_esc(_rate_with_n(d['disagreement_rate'], d['comparable']))}</div>",
        unsafe_allow_html=True)
    st.table([{"signal": it["signal_id"], "judge": it["judge"],
               "human": it["human"]} for it in d["items"]])


# -- half-life ---------------------------------------------------------------

def _render_halflife(halflife_rows):
    st.subheader("Half-life effectiveness")
    st.caption(
        "Descriptive aid (R9.3), not a recommendation: each trigger's "
        "configured half-life next to its observed useful-rate and mean stored "
        "score-decay, so you can eyeball whether the decay looks calibrated.")
    hl = precision.half_life_effectiveness(halflife_rows)
    if not hl:
        _empty("no signals yet — nothing to relate half-life against.")
        return
    table = []
    for row in hl:
        mean_decay = row["mean_score_decay"]
        table.append({
            "trigger": row["trigger_name"] or row["trigger_id"] or "(none)",
            "half-life (d)": row["decay_half_life_days"]
            if row["decay_half_life_days"] is not None else "n/a",
            "useful-rate": _rate_with_n(row["useful_rate"], row["rated"]),
            "mean score-decay": "n/a" if mean_decay is None
            else f"{mean_decay:.2f} (n={row['decay_samples']})",
            "cards": row["cards"],
        })
    st.table(table)


# -- audit run history -------------------------------------------------------

def _render_run_history(run_rows):
    st.subheader("Audit run history")
    st.caption(
        "Every audit-judge invocation (R9.12) — so a skip (no API key / over "
        "budget / nothing to sample) is recorded, not a silent gap.")
    if not run_rows:
        _empty("0 audit runs yet — run `python -m app.audit.judge` to score "
               "cards and populate this history.")
        return
    table = []
    for r in run_rows:
        note = r["skipped_reason"] or r["error_state"] or ""
        table.append({
            "started": r["started_at"],
            "status": r["status"],
            "sampled": r["signals_sampled"],
            "verdicts": r["verdicts_written"],
            "budget $": f"{r['budget_spent']:.4f}"
            if r["budget_spent"] is not None else "0",
            "note": note,
        })
    st.table(table)


def main():
    st.set_page_config(page_title="GridSignals — Precision", layout="wide")
    theme.inject(st)
    st.title("Feedback / Precision")
    _render_framing()

    conn = get_conn()
    feedback_rows = data.precision_feedback_rows(conn)
    audit_rows = data.precision_audit_rows(conn)
    halflife_rows = data.precision_halflife_rows(conn)
    run_rows = data.audit_run_rows(conn)

    _render_headline(feedback_rows, audit_rows)
    st.divider()
    _render_g1(feedback_rows, audit_rows)
    st.divider()
    _render_g2(feedback_rows)
    st.divider()
    _render_useful_tables(feedback_rows)
    st.divider()
    _render_auto_tables(audit_rows)
    st.divider()
    _render_reason_codes(feedback_rows)
    st.divider()
    _render_disagreement(audit_rows, feedback_rows)
    st.divider()
    _render_halflife(halflife_rows)
    st.divider()
    _render_run_history(run_rows)


main()
