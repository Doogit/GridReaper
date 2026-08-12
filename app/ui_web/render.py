"""Pure view helpers for the GridSignals web UI (R8.1 card anatomy).

Ported from app/ui/components.py so the FastAPI templates render the exact same
signal-card shape the Streamlit UI does — one card definition, one set of trust
rules. These functions are pure (no I/O, no Streamlit): they take a
data.signal_detail() dict + data.badge_legend() and return plain data the Jinja
templates escape and lay out. Invariants preserved from components.py:

  * price never reaches the card (signal_detail's fact projection omits
    price_note by construction; nothing here reintroduces it) — R4.3/R7.11.
  * outreach is surfaced ONLY from outreach_safe_text and ONLY when the signal
    is customer_facing_allowed (R4.3); withheld otherwise.
  * non-primary licensing provenance is badged once, deduped by segment (R4.3).

Templates apply Jinja autoescaping to every string here, so this module returns
raw text (no manual html.escape) — the escaping lives in exactly one place.
"""

# Score bands for the severity strip (R8.1); mirrors components.SEVERITY_BANDS.
SEVERITY_BANDS = ("critical", "high", "moderate", "low")

_SCOPE_LABELS = {
    "account": "Account",
    "parent": "Account (parent)",
    "sector": "Sector",
    "subsector": "Subsector",
    "regulatory_calendar": "Regulatory",
}


def severity_band(score):
    """Map a signal ``score`` to a severity band (R8.1). None reads low."""
    if score is None:
        return "low"
    if score >= 4:
        return "critical"
    if score >= 2.75:
        return "high"
    if score >= 1.5:
        return "moderate"
    return "low"


def fmt_score(score):
    if score is None:
        return "unscored"
    return f"{score:.2f}".rstrip("0").rstrip(".")


def scope_label(scope):
    return _SCOPE_LABELS.get(scope, scope or "")


def decay_ceiling(signal):
    """Fresh (undecayed) score ceiling for the decay bar: base_strength x
    account_fit x scope_fit, using persisted fit components when present."""
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


def score_breakdown(signal):
    """R7.3 explainability line 'score 2.34 = 5 x 0.85 x 1.00 x 0.55'
    (base x decay x account-fit x scope-fit). None until rescored."""
    comps = (signal["score_base"], signal["score_decay"],
             signal["score_account_fit"], signal["score_scope_fit"])
    if signal["score"] is None or any(c is None for c in comps):
        return None
    base, decay, acct, scope = (float(c) for c in comps)
    return (f"score {fmt_score(signal['score'])} = {fmt_score(base)} "
            f"× {decay:.2f} × {acct:.2f} × {scope:.2f}")


def gov_caution_line(display_text):
    """The 'gov-cloud caution' line from a snapshot display_text, or None."""
    if not display_text:
        return None
    for line in display_text.splitlines():
        if "gov-cloud caution" in line.lower():
            return line.strip()
    return None


def first_outreach(snapshots):
    for snap in snapshots:
        text = (snap.get("outreach_safe_text") or "").strip()
        if text:
            return text
    return ""


def _non_primary_segments(snapshots):
    return sorted({(f["segment"] or "").strip()
                   for snap in snapshots for f in snap.get("facts", [])
                   if f["source_quality"] == "non-primary"
                   and (f["segment"] or "").strip()})


def _badges(signal, snapshots, legend):
    badges = []
    conf = signal["confidence"]
    if conf is not None:
        badges.append({"cls": "gs-badge", "title": None,
                       "text": f"confidence {float(conf):.2f}"})
    eq = signal["evidence_quality"]
    if eq:
        eq_legend = legend.get("evidence_quality", {}).get(eq, {})
        eq_label = eq_legend.get("label") or eq
        badges.append({"cls": "gs-badge",
                       "title": eq_legend.get("description") or eq_label,
                       "text": eq_label})
    badges.append({"cls": "gs-badge scope", "title": None,
                   "text": scope_label(signal["signal_scope"])})
    if (signal["signal_scope"] in ("account", "parent")
            and signal["coverage_flag"] == "dark"):
        badges.append({"cls": "gs-badge coverage-dark", "title": None,
                       "text": "low coverage"})
    segments = _non_primary_segments(snapshots)
    if segments:
        np_label = legend.get("source_quality", {}).get(
            "non-primary", {}).get("label", "non-primary")
        badges.append({
            "cls": "gs-badge nonprimary",
            "title": ("Backed in part by non-primary licensing sources; "
                      "prices never shown (R4.3)"),
            "text": f"{np_label}: {', '.join(segments)}"})
    return badges


def _chips(snapshots):
    chips = []
    for snap in snapshots:
        label = snap.get("product_name") or snap.get("product_id") or "play"
        path = snap.get("recommended_path")
        chips.append({"text": label + (f": {path}" if path else ""),
                      "gov_caution": gov_caution_line(snap.get("display_text"))})
    return chips


def card_view(detail, legend):
    """Assemble everything _card.html needs from a signal_detail() dict.

    Concentrates the card's display logic in one pure, testable place (the
    render.py seam the plan sanctions) so the template stays layout-only.
    """
    signal = detail["signal"]
    evidence = detail.get("evidence", [])
    snapshots = detail.get("snapshots", [])
    status = signal["status"]
    band = severity_band(signal["score"])

    classes = ["gs-card", f"sev-{band}"]
    if status == "superseded":
        classes.append("status-superseded")
    elif status == "decayed":
        classes.append("status-decayed")

    who = signal["entity_name"] or scope_label(signal["signal_scope"])
    meta_bits = [b for b in (
        signal["trigger_name"] or "",
        str(signal["event_date"] or ""),
        who or "",
        f"score {fmt_score(signal['score'])}",
    ) if b]
    if status != "active":
        meta_bits.append(status)

    ceiling = decay_ceiling(signal)
    score = signal["score"]
    decay = None
    if ceiling and score is not None:
        fill = max(0.0, min(1.0, float(score) / ceiling)) * 100
        decay = {"fill_pct": round(fill), "strength": fmt_score(score),
                 "ceiling": fmt_score(ceiling)}

    outreach_text = first_outreach(snapshots)
    show_outreach = bool(signal["customer_facing_allowed"]) and bool(outreach_text)

    return {
        "signal_id": signal["signal_id"],
        "card_class": " ".join(classes),
        "headline": signal["headline"] or "",
        "meta_bits": meta_bits,
        "decay": decay,
        "breakdown": score_breakdown(signal),
        "badges": _badges(signal, snapshots, legend),
        "chips": _chips(snapshots),
        "evidence": [{"text": ev["evidence_text"] or "",
                      "locator": ev["evidence_locator"] or ""}
                     for ev in evidence],
        "source_url": signal["source_url"],
        # only the safe text, only when cleared — never the withheld text
        "outreach": outreach_text if show_outreach else None,
        "outreach_withheld": (not show_outreach) and bool(snapshots),
    }
