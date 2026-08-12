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

from hashlib import sha256
from urllib.parse import urlsplit


# Score bands for the severity strip (R8.1); mirrors components.SEVERITY_BANDS.
SEVERITY_BANDS = ("critical", "high", "moderate", "low")

_SCOPE_LABELS = {
    "account": "Account",
    "parent": "Account (parent)",
    "sector": "Sector",
    "subsector": "Subsector",
    "regulatory_calendar": "Regulatory",
}

# Account 360 header identifier columns, in display order (R8.3). Rendered only
# when the entity row actually carries a value — many entities are sparse.
# Mirrors _ID_FIELDS in app/ui/pages/3_Account_360.py.
_ID_FIELDS = (
    ("cik", "CIK"),
    ("lei", "LEI"),
    ("wikidata_qid", "Wikidata"),
    ("ticker", "Ticker"),
)


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


def feedback_dom_id(signal_id):
    """Stable, CSS-selector-safe target id for HTMX feedback swaps.

    Real signal ids include raw event ids, and those can be URLs. Keep the raw
    signal_id in POST data, but never use it as a DOM id or selector fragment.
    """
    digest = sha256(str(signal_id or "").encode("utf-8")).hexdigest()[:16]
    return f"gs-fb-{digest}"


def safe_source_url(url):
    """Return an HTTP(S) source URL, or None for non-clickable schemes."""
    value = (url or "").strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    return value


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


def _row_get(row, key):
    """sqlite3.Row has no .get(); return the value or None if the column is
    absent (entity rows are sparse). Mirrors the Streamlit page's _row_get."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def account_header_view(header):
    """Assemble the Account 360 header (R8.3) from data.account_header().

    Pure display shaping ported from app/ui/pages/3_Account_360._render_header:
    the sparse identifier line, the subsector/richness/coverage badges (dark
    accounts read 'low coverage', never 'no activity', R6.6), gov-cloud posture
    (R7.10), and parent/child rows carrying entity_id so the template can link
    to the related account. The template autoescapes every string here.
    """
    entity = header["entity"]
    name = entity["name"] or entity["entity_id"]

    identifiers = []
    for col, label in _ID_FIELDS:
        val = _row_get(entity, col)
        if val is not None and str(val).strip():
            identifiers.append(f"{label} {str(val).strip()}")
    identifiers.append(f"entity_id {entity['entity_id']}")

    coverage = _row_get(entity, "coverage_flag") or "unknown"
    if coverage == "dark":
        coverage_badge = {"cls": "gs-badge coverage-dark",
                          "text": "low coverage (dark)"}
    else:
        coverage_badge = {"cls": "gs-badge",
                          "text": f"coverage: {coverage}"}

    parent = header.get("parent")
    children = header.get("children") or []
    return {
        "name": name,
        "entity_id": entity["entity_id"],
        "identifiers": identifiers,
        "subsector": str(_row_get(entity, "subsector") or "unknown"),
        "richness": str(_row_get(entity, "richness") or "unknown"),
        "coverage_badge": coverage_badge,
        "gov_cloud": str(_row_get(entity, "gov_cloud_likelihood") or "unknown"),
        "tenant_env": str(_row_get(entity, "tenant_cloud_environment")
                          or "unknown"),
        "parent": ({"entity_id": parent["entity_id"], "name": parent["name"]}
                   if parent is not None else None),
        "children": [{"entity_id": c["entity_id"], "name": c["name"]}
                     for c in children],
    }


# Review Queue source-state presentation (R10.3/G2). Mirrors SOURCE_STATE_DISPLAY
# in app/ui/pages/2_Review_Queue.py — each state visually distinct so error vs
# never-run vs stale are never confused. The web UI uses a CSS class per state
# instead of an emoji so the styling lives in app.css, not the string.
_SOURCE_STATE_DISPLAY = {
    "ok": ("state-ok", "OK"),
    "stale": ("state-stale", "STALE"),
    "never_run": ("state-never", "NEVER RUN"),
    "error": ("state-error", "ERROR"),
    "disabled": ("state-disabled", "DISABLED"),
}

STALE_FACT_WINDOW_DAYS = 180


def review_row_dom_id(raw_event_id, candidate_entity_id):
    """Stable, CSS-selector-safe row id for the HTMX accept/reject swap.

    Raw event ids can be URLs; keep them in POST data only, never as a DOM id or
    selector fragment. Same sha256 discipline as feedback_dom_id().
    """
    key = f"{raw_event_id or ''}|{candidate_entity_id or ''}"
    digest = sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"gs-rq-{digest}"


def review_pending_view(item):
    """Shape one review_pending() row for _review_pending_row.html (R8.2).

    Pure display shaping ported from app/ui/pages/2_Review_Queue._render_pending:
    candidate label, the resolver's decision-trail reason (surfaced honestly — no
    fabricated matched/rejected terms), formatted confidence, evidence snippet,
    and an http(s)-only source link. Raw ids ride in POST data, never the DOM id.
    The template autoescapes every string here.
    """
    raw_event_id = item["raw_event_id"]
    candidate_entity_id = item["candidate_entity_id"]
    conf = item.get("confidence")
    conf_txt = f"{conf:.2f}" if isinstance(conf, (int, float)) else "n/a"
    subsector = item.get("subsector")
    return {
        "dom_id": review_row_dom_id(raw_event_id, candidate_entity_id),
        "raw_event_id": raw_event_id,
        "candidate_entity_id": candidate_entity_id,
        "candidate": item.get("candidate_name") or candidate_entity_id,
        "subsector": subsector or "",
        "reason": item.get("reason") or "unknown",
        "confidence": conf_txt,
        "event_date": item.get("event_date") or "n/a",
        "snippet": item.get("snippet") or "",
        "source_url": safe_source_url(item.get("source_url")),
    }


def source_health_view(row, state):
    """Shape one source_health() row + its source_state() label for the template.

    Mirrors _render_source_health: state class + label, source name/id, verbatim
    upstream error text (only when errored, R10.3), last success ('never' when
    none), and ttl. Pure — the template escapes every string.
    """
    state_cls, state_label = _SOURCE_STATE_DISPLAY.get(
        state, ("state-disabled", state.upper()))
    return {
        "state_cls": state_cls,
        "state_label": state_label,
        "name": row["name"],
        "source_id": row["source_id"],
        "error_state": row["last_error_state"] if state == "error" else None,
        "last_success": row["last_success_at"] or "never",
        "ttl": row["ttl"],
    }


def stale_fact_view(fact):
    """Shape one stale_facts() row for the template (R10.7).

    Mirrors _render_stale_facts: product label, sku/segment/source-quality meta,
    verified date ('never' when unknown) and an age string ('Nd old' or
    'unverified'). Pure — the template escapes every string.
    """
    age = fact.get("age_days")
    return {
        "product": fact.get("product_name") or fact.get("product_id"),
        "sku_or_plan": fact.get("sku_or_plan") or "",
        "segment": fact.get("segment") or "",
        "source_quality": fact.get("source_quality") or "",
        "verified_date": fact.get("verified_date") or "never",
        "age": f"{age}d old" if age is not None else "unverified",
    }


def timeline_rows(signals):
    """Compact chronological rows for the Account 360 Timeline tab: date,
    headline, and a scope label per account signal (newest first, as ordered by
    data.account_signals). Pure — the template escapes the strings."""
    return [{"date": str(s["event_date"] or ""),
             "headline": s["headline"] or "",
             "scope_label": scope_label(s["signal_scope"])}
            for s in signals]


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
        "feedback_dom_id": feedback_dom_id(signal["signal_id"]),
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
        "source_url": safe_source_url(signal["source_url"]),
        # only the safe text, only when cleared — never the withheld text
        "outreach": outreach_text if show_outreach else None,
        "outreach_withheld": (not show_outreach) and bool(snapshots),
    }
