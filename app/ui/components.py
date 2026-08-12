"""Shared card rendering for the GridSignals UI (R8.1).

The API is fixed in Phase A so B2 (Review Queue) and B3 (Account 360) consume
these read-only; B1 implements the bodies during the Signal Feed build. Card
HTML uses the classes defined in app/ui/theme.py, injected once per page.

The card is driven by a data.signal_detail() dict (its 'signal', 'evidence',
and 'snapshots' keys). Both the feed and Account 360 render cards from that same
shape, so there is one card and one code path. Snapshot text is read verbatim
from license_play_snapshots (R7.6); non-primary facts are badged but never carry
price text to the DOM (R4.3/R7.11). Outreach is rendered ONLY from
outreach_safe_text and ONLY when the signal is customer_facing_allowed.
"""
import html

import streamlit as st

from app.ui import data

# Score bands for the severity strip (R8.1). Base strengths are 4-5: a fresh
# account card lands ~4+ (base x fits ~= base), a fresh sector card ~2.75, and
# the decay floor that flips a signal to 'decayed' is 1.0. Thresholds are set so
# a fresh account signal reads critical, a fresh sector signal high, a partly
# decayed signal moderate, and a near-floor signal low. None score -> low.
SEVERITY_BANDS = ("critical", "high", "moderate", "low")


def severity_band(score):
    """Map a signal ``score`` to one of SEVERITY_BANDS for the severity strip
    (R8.1). None (not yet scored) reads low, never critical."""
    if score is None:
        return "low"
    if score >= 4:
        return "critical"
    if score >= 2.75:
        return "high"
    if score >= 1.5:
        return "moderate"
    return "low"


def _fmt_score(score):
    if score is None:
        return "unscored"
    return f"{score:.2f}".rstrip("0").rstrip(".")


def _decay_ceiling(signal):
    """The fresh (undecayed) score ceiling for the decay bar: base_strength x
    account_fit x scope_fit using the persisted fit components when present. If
    the fit components are NULL (not yet rescored), fall back to base_strength
    alone so the bar still shows current score against a meaningful ceiling."""
    base = signal["base_strength"]
    if base is None:
        return None
    ceiling = float(base)
    acct = signal["score_account_fit"]
    scope = signal["score_scope_fit"]
    if acct is not None:
        ceiling *= float(acct)
    if scope is not None:
        ceiling *= float(scope)
    return ceiling if ceiling > 0 else None


def render_signal_card(container, detail, legend, *, key, on_feedback=None):
    """Render one signal card (full R8.1 anatomy) into a Streamlit container.

    detail   a data.signal_detail() dict (signal row + evidence + snapshots).
    legend   data.badge_legend(), so badge labels/tooltips are never hardcoded.
    key      unique Streamlit widget-key prefix for this card's controls.
    on_feedback  callback(signal_id, verdict, reason_code, note); None disables
                 the feedback controls.
    """
    signal = detail["signal"]
    evidence = detail.get("evidence", [])
    snapshots = detail.get("snapshots", [])
    status = signal["status"]
    band = severity_band(signal["score"])

    card_classes = ["gs-card", f"sev-{band}"]
    if status == "superseded":
        card_classes.append("status-superseded")
    elif status == "decayed":
        card_classes.append("status-decayed")

    parts = [f"<div class='{' '.join(card_classes)}'>"]

    # headline + meta line
    parts.append(f"<div class='gs-headline'>{html.escape(signal['headline'] or '')}</div>")
    who = signal["entity_name"] or _scope_label(signal["signal_scope"])
    meta_bits = [
        html.escape(signal["trigger_name"] or ""),
        html.escape(str(signal["event_date"] or "")),
        html.escape(who or ""),
        f"score {_fmt_score(signal['score'])}",
    ]
    if status != "active":
        meta_bits.append(html.escape(status))
    parts.append("<div class='gs-meta'>" + " &middot; ".join(b for b in meta_bits if b) + "</div>")

    # decay bar: current score vs fresh ceiling (base x fits, persisted when set)
    ceiling = _decay_ceiling(signal)
    score = signal["score"]
    if ceiling and score is not None:
        fill = max(0.0, min(1.0, float(score) / ceiling)) * 100
        parts.append(
            f"<div class='gs-decay-wrap'><div class='gs-decay-fill' "
            f"style='width:{fill:.0f}%'></div></div>")
        parts.append(
            f"<div class='gs-meta'>strength {_fmt_score(score)} of "
            f"{_fmt_score(ceiling)} fresh ceiling</div>")

    # badges: confidence, evidence (with legend tooltip), scope, coverage
    badges = []
    conf = signal["confidence"]
    if conf is not None:
        badges.append(f"<span class='gs-badge'>confidence {float(conf):.2f}</span>")
    eq = signal["evidence_quality"]
    if eq:
        eq_legend = legend.get("evidence_quality", {}).get(eq, {})
        eq_label = eq_legend.get("label") or eq
        eq_desc = eq_legend.get("description") or eq_label
        badges.append(
            f"<span class='gs-badge' title='{html.escape(eq_desc)}'>"
            f"{html.escape(eq_label)}</span>")
    scope_label = _scope_label(signal["signal_scope"])
    badges.append(f"<span class='gs-badge scope'>{html.escape(scope_label)}</span>")
    # coverage badge only on account cards flagged dark
    if signal["signal_scope"] in ("account", "parent") and signal["coverage_flag"] == "dark":
        badges.append("<span class='gs-badge coverage-dark'>low coverage</span>")
    if badges:
        parts.append("<div class='gs-badges'>" + "".join(badges) + "</div>")

    # product/play chips + gov-cloud caution + non-primary fact chips per snapshot
    for snap in snapshots:
        chip_label = snap.get("product_name") or snap.get("product_id") or "play"
        path = snap.get("recommended_path")
        chip_text = chip_label + (f": {path}" if path else "")
        parts.append(f"<span class='gs-chip'>{html.escape(chip_text)}</span>")
        gov_line = _gov_caution_line(snap.get("display_text"))
        if gov_line:
            parts.append(f"<div class='gs-gov-caution'>{html.escape(gov_line)}</div>")
        np_label = legend.get("source_quality", {}).get(
            "non-primary", {}).get("label", "non-primary")
        for fact in snap.get("facts", []):
            if fact["source_quality"] == "non-primary":
                seg = fact["segment"] or ""
                text = f"{np_label}: {seg}".strip().rstrip(":")
                parts.append(
                    f"<span class='gs-badge nonprimary'>{html.escape(text)}</span>")

    parts.append("</div>")  # /gs-card
    container.markdown("".join(parts), unsafe_allow_html=True)

    # expandable evidence (native widget, outside the HTML block)
    if evidence:
        exp = container.expander("Evidence")
        for ev in evidence:
            exp.markdown(
                f"<div class='gs-evidence'>{html.escape(ev['evidence_text'] or '')}"
                f"<div class='gs-locator'>{html.escape(ev['evidence_locator'] or '')}</div>"
                f"</div>", unsafe_allow_html=True)
        src = signal["source_url"]
        if src:
            exp.markdown(f"[Source]({src})")

    # outreach draft: only from outreach_safe_text, only when customer-facing
    outreach = _first_outreach(snapshots)
    if signal["customer_facing_allowed"] and outreach:
        container.markdown(
            f"<div class='gs-evidence'><b>Outreach draft</b><br>"
            f"{html.escape(outreach)}</div>", unsafe_allow_html=True)
    elif snapshots:
        container.caption(
            "Outreach withheld — verification-first: not cleared for "
            "customer-facing use.")

    # feedback controls
    if on_feedback is not None:
        _render_feedback(container, signal["signal_id"], key, on_feedback)


def _render_feedback(container, signal_id, key, on_feedback):
    col_useful, col_not = container.columns(2)
    if col_useful.button("Useful", key=f"{key}_useful"):
        _safe_feedback(container, on_feedback, signal_id, "useful", None, "")
    if col_not.button("Not useful", key=f"{key}_notuseful_btn"):
        st.session_state[f"{key}_show_reason"] = True
    if st.session_state.get(f"{key}_show_reason"):
        reason = container.selectbox(
            "Reason", options=data.REASON_CODES, key=f"{key}_reason")
        note = container.text_input("Note (optional)", key=f"{key}_note")
        if container.button("Submit", key=f"{key}_submit"):
            _safe_feedback(container, on_feedback, signal_id, "not_useful",
                           reason, note)
            st.session_state[f"{key}_show_reason"] = False


def _safe_feedback(container, on_feedback, signal_id, verdict, reason, note):
    try:
        on_feedback(signal_id, verdict, reason, note)
        container.success("Feedback recorded.")
    except ValueError as exc:
        container.error(str(exc))


def _first_outreach(snapshots):
    for snap in snapshots:
        text = (snap.get("outreach_safe_text") or "").strip()
        if text:
            return text
    return ""


def _gov_caution_line(display_text):
    """Return the 'Gov-cloud caution' line from a snapshot display_text, or None.
    Only surfaced when the snapshot itself carries one (R7.6/R7.9)."""
    if not display_text:
        return None
    for line in display_text.splitlines():
        if "gov-cloud caution" in line.lower():
            return line.strip()
    return None


def _scope_label(scope):
    return {
        "account": "Account",
        "parent": "Account (parent)",
        "sector": "Sector",
        "subsector": "Subsector",
        "regulatory_calendar": "Regulatory",
    }.get(scope, scope or "")


def render_feed_divider(container, label):
    """The labeled sector/regulatory divider between account-scoped cards and the
    broader cards (R7.2), so sector cards never visually mix into account cards."""
    container.markdown(
        f"<div class='gs-divider'>{html.escape(label)}</div>",
        unsafe_allow_html=True)


def render_empty_state(container, message):
    """An honest empty-state panel (R6.6): a quiet surface reads as 'low volume
    by design', never as broken."""
    container.markdown(
        f"<div class='gs-empty'>{html.escape(message)}</div>",
        unsafe_allow_html=True)
