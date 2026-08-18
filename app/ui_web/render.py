"""Pure view helpers for the GridSignals web UI (R8.1 card anatomy).

Ported from app/ui/components.py so the FastAPI templates render the exact same
signal-card shape the retired Streamlit UI did — one card definition, one set of
trust rules. These functions are pure (no I/O): they take a
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

from app.ui import data
from app.ui_web import us_geometry


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


def tier_dom_id(signal_id):
    """Stable, CSS-selector-safe target id for the HTMX incident re-tier panel.
    Same sha256 discipline as feedback_dom_id() — a signal_id can be a URL, so
    never use it raw as a DOM id."""
    digest = sha256(str(signal_id or "").encode("utf-8")).hexdigest()[:16]
    return f"gs-tier-{digest}"


def card_key(signal_id):
    """Stable, URL-safe permalink key for one card (R8.1). A signal_id is a
    structured `{trigger}:{raw_event_id}:{scope|entity}` string whose
    raw_event_id can itself be a URL — never put it raw in a path segment. Same
    sha256 discipline as the DOM-id helpers, but with no `gs-` prefix because it
    rides in a URL path (`/card/{key}`), not a CSS selector. 64 bits is
    collision-safe at this scale; routes/card.py resolves a key back to its
    signal by scanning, so the mapping stays one source of truth here."""
    return sha256(str(signal_id or "").encode("utf-8")).hexdigest()[:16]


def safe_source_url(url):
    """Return an HTTP(S) source URL, or None for non-clickable schemes.

    Scheme guard only. The R4.1 identity rule (a name-free card must not link a
    per-victim leak-tracker permalink) is NOT applied here and must not be: it
    needs the signal's scope to tell an own-incident card from a peer card, and
    this helper is not given one. Callers pipe the URL through
    ``data.identity_safe_source_url`` first — that is the single chokepoint.
    """
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


def _non_primary_badge(snapshots, legend):
    """The non-primary provenance badge (R4.3), or None when every snapshot's
    facts are all primary (or there are none). Shared by ``_badges`` (feed
    card / signal detail) and ``account_play_rows`` (Products tab) so a play
    badged non-primary reads with IDENTICAL text/tooltip on both surfaces."""
    segments = _non_primary_segments(snapshots)
    if not segments:
        return None
    np_label = legend.get("source_quality", {}).get(
        "non-primary", {}).get("label", "non-primary")
    return {
        "cls": "gs-badge nonprimary",
        "title": ("Backed in part by non-primary licensing sources; "
                  "prices never shown (R4.3)"),
        "text": f"{np_label}: {', '.join(segments)}"}


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
    if signal["incident_evidence_level"] == "unconfirmed_early_warning":
        badges.append({
            "cls": "gs-badge unconfirmed",
            "title": ("Unverified early warning — not confirmed by the "
                      "company, a regulator, or an SEC filing (R10.5)"),
            "text": "unconfirmed"})
    if (signal["signal_scope"] in ("account", "parent")
            and signal["coverage_flag"] == "dark"):
        badges.append({"cls": "gs-badge coverage-dark", "title": None,
                       "text": "low coverage"})
    badge = _non_primary_badge(snapshots, legend)
    if badge:
        badges.append(badge)
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
    absent (entity rows are sparse). Ported from the retired Streamlit page's
    _row_get."""
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


# A review row is BY DEFINITION unadjudicated — the resolver could not settle
# the match and no human has ruled yet — so the badge is a constant, not a tier
# joined from review_queue (which carries no tier column) (R8.2, R10.5).
REVIEW_UNCONFIRMED_BADGE = {
    "cls": "gs-badge unconfirmed",
    "title": ("Unconfirmed candidate — the resolver could not settle this "
              "match and no human has adjudicated it yet (R10.5)"),
    "text": "unconfirmed candidate",
}

# ``review_queue.candidate_entity_id`` is nullable, and a NULL means a different
# question. A resolver near-match asks "is this the right company?"; a row with
# no candidate was routed to triage for another reason entirely (e.g. the R10.6
# provenance quarantine) and asks "read the reason". Rendering them identically
# is the same defect the badge exists to fix, so the label is explicit and the
# accept/reject controls — which would write an entity_match_decisions row for
# an entity that does not exist — are withheld.
REVIEW_NO_CANDIDATE_LABEL = "No candidate entity — flagged for review"


def review_pending_view(item):
    """Shape one review_pending() row for _review_pending_row.html (R8.2).

    Pure display shaping ported from app/ui/pages/2_Review_Queue._render_pending:
    candidate label, the resolver's decision-trail reason (surfaced honestly — no
    fabricated matched/rejected terms), formatted confidence, evidence snippet,
    and an http(s)-only source link. Raw ids ride in POST data, never the DOM id.
    The template autoescapes every string here.

    ``candidate_entity_id`` is nullable: a row with no candidate is labelled as
    such (never a blank name) and offers no accept/reject, because there is no
    match to accept — see REVIEW_NO_CANDIDATE_LABEL.

    Three trust guards (R10.5, R4.1): a constant unconfirmed badge so a named
    candidate cannot read as confirmed; a labelled truncation so a cut-off
    payload cannot read as the whole record; and the source link routed through
    ``data.identity_safe_source_url`` — a review row has no signal scope, so a
    ransomware.live per-victim permalink (base64 victim name) is replaced by the
    tracker index the same way a name-free sector card's is.
    """
    raw_event_id = item["raw_event_id"]
    candidate_entity_id = item["candidate_entity_id"]
    conf = item.get("confidence")
    conf_txt = f"{conf:.2f}" if isinstance(conf, (int, float)) else "n/a"
    subsector = item.get("subsector")
    raw_url = item.get("source_url")
    safe_url = data.identity_safe_source_url(raw_url)
    substituted = bool(raw_url) and safe_url != (raw_url or "").strip()
    return {
        "dom_id": review_row_dom_id(raw_event_id, candidate_entity_id),
        "raw_event_id": raw_event_id,
        "candidate_entity_id": candidate_entity_id,
        "has_candidate": bool(candidate_entity_id),
        "candidate": (item.get("candidate_name") or candidate_entity_id
                      if candidate_entity_id else REVIEW_NO_CANDIDATE_LABEL),
        "subsector": subsector or "",
        "reason": item.get("reason") or "unknown",
        "confidence": conf_txt,
        "event_date": item.get("event_date") or "n/a",
        "snippet": item.get("snippet") or "",
        "snippet_truncated": bool(item.get("snippet_truncated")),
        "snippet_note": review_snippet_note(item),
        "badges": [REVIEW_UNCONFIRMED_BADGE],
        "source_url": safe_source_url(safe_url),
        # R10.4: a substituted link must not read as a working citation for the
        # claim beside it — say that the permalink was withheld.
        "source_note": ("tracker index — per-victim permalink withheld (R4.1)"
                        if substituted else ""),
    }


def review_snippet_note(item):
    """Disclose how much smaller the shown snippet is than the raw record.

    Two independent shortfalls, either of which lets a fragment read as the
    whole record (R10.5): the character cut, and the field selection — one
    title lifted out of a payload that also carried a body and metadata. A
    short title extracted from a 1 KB record has no cut to label, so counting
    the unshown fields is what makes that case honest. Empty string when the
    snippet IS the record. Pure.
    """
    truncated = bool(item.get("snippet_truncated"))
    omitted = item.get("snippet_omitted_fields") or 0
    limit = item.get("snippet_limit")
    if truncated and omitted:
        return (f"Extract: 1 of {omitted + 1} fields in the raw record, cut at "
                f"{limit} characters — not the whole record.")
    if truncated:
        return f"Extract truncated at {limit} characters — not the whole record."
    if omitted:
        return (f"Extract: 1 of {omitted + 1} fields in the raw record — not "
                "the whole record.")
    return ""


def duplicate_candidate_view(pair):
    """Shape one duplicate_candidates() pair for _review_duplicates.html (R8.2).

    Both signals are identified by ``card_key`` (sha256), never by raw
    ``signal_id``: a signal_id embeds its raw_event_id, which for a press source
    IS the article URL (KTD2). The operator opens each card and judges; this row
    proposes and states its basis, and offers no merge or dismiss action,
    because a duplicate ruling is the operator's alone. Pure.
    """
    bases = pair.get("bases") or []
    gap = pair.get("day_gap")
    return {
        "key_a": card_key(pair["signal_a"]),
        "key_b": card_key(pair["signal_b"]),
        "headline_a": pair.get("headline_a") or "(untitled)",
        "headline_b": pair.get("headline_b") or "(untitled)",
        "event_date_a": pair.get("event_date_a") or "n/a",
        "event_date_b": pair.get("event_date_b") or "n/a",
        # both sides score separately and both stay on the feed until the
        # operator rules, so the double-count is stated where it is decided
        "score_a": fmt_score(pair.get("score_a")),
        "score_b": fmt_score(pair.get("score_b")),
        "entity": pair.get("entity_name") or pair.get("entity_id") or "(no entity)",
        "trigger": pair.get("trigger_name") or pair.get("trigger_id") or "(none)",
        "scope_a": scope_label(pair.get("scope_a")),
        "scope_b": scope_label(pair.get("scope_b")),
        "bases": [data.duplicate_basis_label(b, gap) for b in bases],
    }


def disagreement_item_view(item):
    """Shape one judge_human_disagreements() item for the triage queue (R8.2).

    Judge and human verdicts side by side on a card the operator can open.
    Keyed by ``card_key`` for the same reason duplicates are. Pure.
    """
    return {
        "key": card_key(item["signal_id"]),
        "headline": item.get("headline") or "(untitled)",
        "event_date": item.get("event_date") or "n/a",
        "entity": item.get("entity_name") or item.get("entity_id") or "(no entity)",
        "trigger": item.get("trigger_name") or "(none)",
        "source_id": item.get("source_id") or "(no source)",
        "judge": item.get("judge") or "n/a",
        "human": item.get("human") or "n/a",
        "scope": scope_label(item.get("signal_scope")),
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
# -- Precision / QA view helpers (R8.6, R9.2-R9.5, R9.12, R10.9) -------------
#
# Ported from app/ui/pages/4_Precision.py. These helpers FORMAT, they do not
# compute: the computation dicts arrive already built by
# ``data.precision_report`` — R10.9 keeps app.audit.precision on the backend
# side of the boundary, and the view reads it through the data seam like
# everything else. What is left here is the TRUST INVARIANT carried to the DOM —
# a rate is NEVER emitted as a bare percentage; every rate ships with its n, and
# a None rate reads as an honest "n/a", never a fabricated 0%. Below the G1
# sample floor (``report["min_rated"]``, n<20) the headline shows the low-n
# message instead of a gauge. The template autoescapes every string here.

# The R9.3 dimensions and a friendly column header for each (mirrors the page).
PRECISION_DIMENSION_LABELS = {
    "trigger": "Trigger",
    "source": "Source",
    "signal_scope": "Signal scope",
    "incident_evidence_level": "Incident tier",
}


def _pct(rate):
    """A rate as a percent string, or the honest 'n/a' when it is None."""
    return "n/a" if rate is None else f"{rate:.0%}"


def rate_with_n(rate, n, unit="rated"):
    """'72% (n=25)' when populated; 'n/a (0 rated)' when empty. The n ALWAYS
    travels with the rate — the trust invariant carried to the DOM."""
    if rate is None:
        return f"n/a ({n} {unit})"
    return f"{rate:.0%} (n={n})"


def precision_headline_view(report):
    """Headline useful-rate + auto-accuracy, each n-gated. A gauge value is only
    supplied above the G1 sample floor / with real data; below it, ``gauge`` is
    None so the template renders the low-n / empty copy, never a fake percent."""
    ur = report["useful_overall"]
    aa = report["auto_overall"]
    min_rated = report["min_rated"]

    if ur["total"] >= min_rated and ur["rate"] is not None:
        useful = {"gauge": _pct(ur["rate"]), "n": ur["total"],
                  "low_n": None}
    else:
        useful = {"gauge": None, "n": ur["total"], "low_n": (
            f"not enough rated cards yet (n={ur['total']}<"
            f"{min_rated}) — no gauge until at least the G1 floor "
            "of rated feedback.")}

    if aa["scored"] > 0 and aa["accuracy"] is not None:
        auto = {"gauge": _pct(aa["accuracy"]), "n": aa["scored"], "empty": None}
    else:
        auto = {"gauge": None, "n": aa["scored"], "empty": (
            "0 audit verdicts yet — run `python -m app.audit.judge` to score "
            "entity_match + evidence_support.")}
    return {"useful": useful, "auto": auto}


def precision_g1_view(g1):
    """Gate G1 per primary account trigger + the reported-separately block
    (R9.4). Rates are pre-formatted with their n; sector / regulatory /
    unconfirmed cards are reported apart so they can't inflate account
    precision.

    The overall line is three-way (KTD1): ``waived`` (the recorded operator
    ruling — a disclosure, never a pass), ``eligible``, or blocked. Under a
    waiver the per-trigger BLOCKED badges and blocked reasons render UNCHANGED:
    the waiver discloses that the gate was not passed, it never hides the
    conditions it failed."""
    triggers = []
    for tid, m in g1["triggers"].items():
        triggers.append({
            "trigger_id": tid,
            "meets": m["meets"],
            "badge": "MEETS" if m["meets"] else "BLOCKED",
            "useful": rate_with_n(m["useful_rate"], m["useful_n"]),
            "auto": rate_with_n(m["auto_accuracy"], m["auto_n"], "scored"),
            "span": f"{m['days_span']:.1f}d",
            "blocked": (None if m["meets"] else
                        ("; ".join(m["reasons"]) or "insufficient evidence")),
        })
    rep = g1["reported_separately"]
    fb, au = rep["feedback"], rep["auto"]
    waiver = g1["waiver"]
    return {
        "triggers": triggers,
        "eligible": g1["eligible"],
        "waived": g1["state"] == "waived",
        "waiver_reason": waiver["reason"] if waiver else None,
        "waiver_lift": waiver["lift_condition"] if waiver else None,
        "reported_useful": rate_with_n(fb["rate"], fb["total"]),
        "reported_auto": rate_with_n(au["accuracy"], au["scored"], "scored"),
    }


def precision_g2_view(g2):
    """Gate G2 per-source demotion *recommendations* — report-only (R9.5), with
    the R9.11 disagreement gate already overlaid by ``data.precision_report``
    (KTD3).

    A source with >20% judge-human disagreement over enough comparable evidence
    has its demote recommendation WITHHELD ("judge verdicts withheld from
    demotion — revise rubric"); below the comparable floor the gate reads "below
    floor" and NEVER blocks; no comparable signals reads "n/a". Every gate ships
    with its disagreement rate AND comparable ``n`` so an operator can see
    dormant-vs-active. Empty list when no rated cards are attributed to any
    source yet.
    """
    out = []
    for sid, m in g2.items():
        out.append({
            "source_id": sid if sid is not None else "(no source)",
            "flag": "DEMOTE?" if m["demote_recommended"] else "ok",
            "precision": rate_with_n(m["precision"], m["n"]),
            "note": m["note"],
            "reason_codes": [f"{code}={n}"
                             for code, n in m["reason_codes"].items()],
            "gate": m["gate"],
            "gate_note": m["gate_note"],
            "disagreement": rate_with_n(m["disagreement_rate"],
                                        m["comparable"]),
        })
    return out


def precision_spotcheck_view(sc):
    """Monthly audit spot-check tracker (R9.11) — report-only. Shows how many
    audited signals a human reviewed this month against the R9.11 target, with
    honest "below floor" copy below the target and its window. Reuses existing
    feedback-on-audited-signal rows (no new storage)."""
    return {
        "reviewed": sc["reviewed"],
        "audited": sc["audited"],
        "target": sc["target"],
        "met": sc["met"],
        "window": sc["window"],
        "status": "met" if sc["met"] else "below floor",
        "summary": (
            f"{sc['reviewed']} of {sc['target']} target this month "
            f"({sc['window']}, over {sc['audited']} audited)"),
    }


def _dimension_tables(by_dimension, value_key, empty_key):
    """Shared shaper for the useful-rate / auto-accuracy per-dimension tables:
    one table per dimension that actually has sliced values (the headline
    already carries 'overall'). Each cell keeps its own n."""
    tables = []
    for dimension, sliced in by_dimension.items():
        values = [k for k in sliced if k != "overall"]
        if not values:
            continue
        label = PRECISION_DIMENSION_LABELS[dimension]
        table_rows = []
        for value in values:
            b = sliced[value]
            table_rows.append({
                "value": value if value is not None else "(none)",
                "rate": rate_with_n(b[value_key], b[empty_key],
                                    "rated" if empty_key == "total"
                                    else "scored"),
            })
        tables.append({"label": label, "rows": table_rows})
    return tables


def precision_useful_tables(useful_by_dimension):
    """Human useful-rate sliced by trigger / source / scope / incident tier
    (R9.3); empty list when nothing is rated yet."""
    return _dimension_tables(useful_by_dimension, "rate", "total")


def precision_auto_tables(auto_by_dimension):
    """Automated judge accuracy sliced the same way (R9.3); empty list when
    there are no scored verdicts yet."""
    return _dimension_tables(auto_by_dimension, "accuracy", "scored")


def precision_disagreement_view(d):
    """Judge-vs-human agreement over comparable signals (a QA signal, not ground
    truth). ``comparable`` == 0 → the template shows the honest empty copy."""
    return {
        "comparable": d["comparable"],
        "agree": d["agree"],
        "disagree": d["disagree"],
        "rate": rate_with_n(d["disagreement_rate"], d["comparable"]),
        # keyed "pairs" not "items": Jinja resolves ``d.items`` to the dict
        # method, so the template can't dot-access an "items" key.
        "pairs": d["items"],
    }


def precision_halflife_view(hl):
    """Per-trigger configured half-life vs observed useful-rate + mean stored
    score-decay (R9.3, descriptive aid). Empty list when there are no signals
    yet."""
    out = []
    for row in hl:
        mean_decay = row["mean_score_decay"]
        out.append({
            "trigger": row["trigger_name"] or row["trigger_id"] or "(none)",
            "half_life": (row["decay_half_life_days"]
                          if row["decay_half_life_days"] is not None else "n/a"),
            "useful": rate_with_n(row["useful_rate"], row["rated"]),
            "mean_decay": ("n/a" if mean_decay is None else
                           f"{mean_decay:.2f} (n={row['decay_samples']})"),
            "cards": row["cards"],
        })
    return out


def precision_run_history(run_rows):
    """Audit-run history rows (R9.12) — status, sample/verdict counts, budget,
    and a skip/error note so a skip is recorded, not a silent gap."""
    out = []
    for r in run_rows:
        out.append({
            "started": r["started_at"],
            "status": r["status"],
            "sampled": r["signals_sampled"],
            "verdicts": r["verdicts_written"],
            "budget": (f"{r['budget_spent']:.4f}"
                       if r["budget_spent"] is not None else "0"),
            "note": r["skipped_reason"] or r["error_state"] or "",
        })
    return out


# -- Admin / config (Chunk 6) ------------------------------------------------
#
# Pure display helpers ported from app/ui/pages/5_Admin.py so the admin route +
# templates render the exact same result messages and provenance labels the
# Streamlit page did. Every string returned here is autoescaped by the template.

# Friendly headers for the scoring_weights.weight_kind groups (R7.5); ported
# from WEIGHT_KIND_LABELS in the retired Streamlit Admin page.
WEIGHT_KIND_LABELS = {
    "subsector": "Subsector fit",
    "richness": "Account richness",
    "coverage": "Coverage",
    "applicability": "Regulatory applicability",
    "scope": "Signal scope fit",
}

# config_audit "field" sentinels for row-level lifecycle edits -> operator-facing
# labels (render only; config_audit still stores the sentinel). Ported from
# _FIELD_LABELS in the retired Streamlit page.
_AUDIT_FIELD_LABELS = {
    "__add__": "added",
    "__remove__": "removed",
    "__reset__": "reset to seed",
}


def weight_kind_label(kind):
    return WEIGHT_KIND_LABELS.get(kind, kind)


def _enabled_label(value):
    return "Enabled" if value else "Disabled"


def _active_label(value):
    return "Active" if value else "Disabled"


def result_message(fn_name, result):
    """Operator-facing flash text for one config-write result (ported verbatim
    from the Streamlit page's _result_msg). ``fn_name`` is the data.py helper
    name; ``result`` is its return dict."""
    if fn_name == "update_tuning":
        # A batch reports a COUNT, not an old -> new pair: the operator saved a
        # tier, and only the values they actually moved were written.
        if not result["changed"]:
            return (f"No change — all {result['submitted']} value(s) were "
                    "already set.")
        return (f"Saved {result['changed']} of {result['submitted']} value(s). "
                "Active cards rescored.")
    if not result.get("changed", True):
        if fn_name == "set_source_enabled":
            return f"No change — source already {_enabled_label(result['new'])}."
        if fn_name == "set_entity_active":
            return f"No change — entity already {_active_label(result['new'])}."
        if fn_name == "retier_incident":
            return f"No change — tier already {result['new_level']}."
        return f"No change — value already {result['new']}."
    if fn_name == "retier_incident":
        # State the outreach consequence explicitly (R7.12): re-tiering up
        # unblocks customer-facing outreach; re-tiering to unconfirmed suppresses it.
        verb = ("outreach now suppressed" if result["new_cfa"] == 0
                else "outreach now allowed")
        return (f"Re-tiered: {result['old_level']} → {result['new_level']} "
                f"({verb}).")
    if fn_name == "add_watchlist_entity":
        return f"Added entity {result['entity_id']}."
    if fn_name == "remove_operator_entity":
        return (f"Removed entity {result['entity_id']} and any operator "
                "aliases / collision terms.")
    if fn_name == "reset_entity_to_seed":
        return (f"Reset {result['entity_id']} to its seed values "
                "(re-enabled; operator aliases / collision terms cleared).")
    if fn_name == "add_alias":
        return f"Added alias “{result['alias']}”."
    if fn_name == "remove_alias":
        return f"Removed alias “{result['alias']}”."
    if fn_name == "add_collision_term":
        return f"Added collision term “{result['term']}”."
    if fn_name == "remove_collision_term":
        return f"Removed collision term “{result['term']}”."
    if fn_name == "add_license_fact":
        return f"Added license fact {result['fact_id']}."
    if fn_name == "add_source":
        return f"Added source {result['source_id']}."
    if fn_name == "remove_source":
        return f"Removed source {result['source_id']}."
    if fn_name == "set_source_enabled":
        return (f"Saved: {_enabled_label(result['old'])} → "
                f"{_enabled_label(result['new'])}.")
    if fn_name == "set_entity_active":
        return (f"Saved: {_active_label(result['old'])} → "
                f"{_active_label(result['new'])}.")
    if "scored" in result:
        return (f"Saved: {result['old']} → {result['new']}. "
                f"{result['scored']} active card(s) rescored, "
                f"{result['decayed']} decayed.")
    return f"Saved: {result['old']} → {result['new']}."


def result_level(fn_name, result):
    """The flash level for a successful config write: 'info' for a no-op save
    (nothing written), 'success' otherwise — ported from the retired Streamlit
    page."""
    return "success" if result.get("changed", True) else "info"


def humanize_audit_field(field):
    return _AUDIT_FIELD_LABELS.get(field, field)


def humanize_audit_pk(table_name, pk):
    """scoring_weights stores a JSON composite pk; render it 'kind · key' so the
    provenance table reads cleanly. Other tables use a bare pk already. Ported
    from _humanize_pk in the retired Streamlit page."""
    if table_name == "scoring_weights":
        from json import loads
        try:
            d = loads(pk)
            return f"{d['weight_kind']} · {d['key']}"
        except (ValueError, KeyError, TypeError):
            return pk
    return pk


def audit_view(rows):
    """Provenance rows for the Recent config changes table, humanizing the pk +
    field sentinels. Pure; the template autoescapes every string."""
    return [{
        "when": r["ts"],
        "table": r["table_name"],
        "row": humanize_audit_pk(r["table_name"], r["pk"]),
        "field": humanize_audit_field(r["field"]),
        "old_new": f"{r['old_value']} → {r['new_value']}",
        "reason": r["reason"] or "",
    } for r in rows]


def recent_retiers_view(rows):
    """Rows for the central Recent re-tiers audit table (R8.7 oversight), newest
    first. One data.recent_retiers row -> the card it re-tiered (headline +
    account, an em-dash account for a sector-scoped peer incident with no
    account), the tier change stated ``old -> new`` (R10.5), and the outreach
    consequence spelled out (R7.12) the same way result_message does it: a
    re-tier that clears the card for customer-facing outreach reads "cleared for
    outreach" (and is flagged), one that suppresses it reads "suppressed", a
    lateral confirmed/corroborated move an em-dash. Pure; the template
    autoescapes every string."""
    out = []
    for r in rows:
        if r["gate_raised"]:
            outreach = "cleared for outreach"
        elif r["old_cfa"] == 1 and r["new_cfa"] == 0:
            outreach = "suppressed"
        else:
            outreach = "—"
        out.append({
            "when": r["ts"],
            "headline": r["headline"] or "",
            # R8.1 deep link: the audit row links back to the card it re-tiered.
            "permalink": f"/card/{card_key(r['signal_id'])}",
            "account": r["entity_name"] or "—",
            "change": f"{r['old_level']} → {r['new_level']}",
            "outreach": outreach,
            "gate_raised": r["gate_raised"],
            "editor": r["editor"] or "",
            "reason": r["reason"] or "",
        })
    return out


def staleness_view(stale):
    """Stale license-fact rows for the R10.7 list, with literal field-stating age
    labels ('verified N days ago' / 'verification date unknown' — never an
    interpretive 'unverified'). Mirrors _render_staleness."""
    out = []
    for fact in stale:
        age = fact.get("age_days")
        age_txt = (f"verified {age} days ago" if age is not None
                   else "verification date unknown")
        out.append({
            "product": fact.get("product_name") or fact.get("product_id"),
            "sku_or_plan": fact.get("sku_or_plan"),
            "segment": fact.get("segment"),
            "age_txt": age_txt,
        })
    return out


def timeline_rows(signals):
    """Compact chronological rows for the Account 360 Timeline tab: date,
    headline, and a scope label per account signal (newest first, as ordered by
    data.account_signals). Pure — the template escapes the strings."""
    return [{"date": str(s["event_date"] or ""),
             "headline": s["headline"] or "",
             "scope_label": scope_label(s["signal_scope"])}
            for s in signals]


def account_play_rows(plays, legend):
    """Rows for the Account 360 Products tab (R8.3, R7.6, R4.3).

    Display shaping only, over data.account_license_plays. The stored snapshot
    text is passed through verbatim — it is what the card showed, frozen — and
    no price ever appears because the data layer never returns one. Each row
    carries the SAME non-primary provenance badge ``_badges`` builds for the
    feed card (identical text/tooltip), computed from that play's OWN facts
    via ``_non_primary_badge`` — a play badged non-primary on the feed reads
    identically here.

    The discovery question is rendered once (R7.6): the frozen snapshot text
    sometimes already contains it verbatim, and when it does the snapshot wins
    — ``discovery_question`` comes back empty so the template's existing
    ``{% if %}`` naturally drops the separate line rather than duplicating it.
    """
    out = []
    for p in plays:
        display_text = p["display_text"] or ""
        discovery_question = p["discovery_question"] or ""
        if discovery_question and discovery_question in display_text:
            discovery_question = ""
        badge = _non_primary_badge([p], legend)
        out.append({"signal_id": p["signal_id"],
                    "card_key": card_key(p["signal_id"]),
                    "play_id": p["play_id"],
                    "product": p["product_name"] or p["product_id"] or p["play_id"],
                    "headline": p["headline"] or "",
                    "event_date": str(p["event_date"] or ""),
                    "display_text": display_text,
                    "discovery_question": discovery_question,
                    "generation_version": str(p["generation_version"] or ""),
                    "badges": [badge] if badge else []})
    return out


# Every derived obligation leaves compliance_date NULL — FERC states compliance
# dates in the order body, which the fetched metadata does not carry. The tab
# says so in these words rather than showing a blank cell the reader could mistake
# for "no deadline", and NOTHING here derives a date from effective_date (R4.1).
COMPLIANCE_DATE_UNKNOWN = "not in the fetched record"

CALENDAR_CAPTION = (
    "Effective dates of regulatory obligations that apply to this account. A "
    "rule is matched on the account's subsector class and may also exclude "
    "named accounts within that class. This is an effective-date calendar, not "
    "a deadline calendar: compliance dates are stated in the order body, which "
    "is not in the fetched record, so none is shown or derived. Matching is a "
    "regulated-class filter — it does not assert that this account is a "
    "registered entity."
)

# The empty tab has two different reasons and must not conflate them: nothing in
# the store binds this account's class, or something binds the class and names
# this account in its exclusion clause. The second wording states what the
# exclusion means — not a plausible owner or operator of the regulated assets —
# and never that the account is unregistered, which the store does not know.
CALENDAR_EMPTY = (
    "No regulatory obligations apply to this account's subsector class. "
    "Obligations are derived from stored regulatory signals that carry a "
    "durable compliance clock; when one applies to this class it appears here, "
    "soonest effective date first."
)
CALENDAR_EMPTY_EXCLUDED = (
    "No regulatory obligations apply to this account. Ones that apply to its "
    "subsector class exclude this account by name — it is not a plausible "
    "owner or operator of the regulated assets. The coverage line above counts "
    "them."
)


def account_calendar_view(calendar):
    """Assemble the Account 360 Compliance Calendar tab (R8.3, R7.2, R4.1).

    Takes data.account_obligations. Returns the account's matched subsector,
    the shaped obligation rows, a coverage line that discloses how many of the
    store's obligations were checked, how many could not be evaluated and how
    many bind this account's class but exclude this account by name — an empty
    tab must read as "checked 3, none apply to this class", never as an
    unexplained blank — and the empty-state message that matches which of those
    two reasons emptied it, so the template stays presentation-only.
    """
    rows = []
    for o in calendar["obligations"]:
        rows.append({
            "obligation_id": o["obligation_id"],
            "regulator": o["regulator"] or "",
            "rule_name": o["rule_name"] or "",
            "effective_date": str(o["effective_date"] or ""),
            "in_effect": bool(o["in_effect"]),
            "status_label": "in effect" if o["in_effect"] else "upcoming",
            "compliance_date": (str(o["compliance_date"])
                                if o["compliance_date"]
                                else COMPLIANCE_DATE_UNKNOWN),
            "affected_scope": o["affected_scope"] or "",
            "products": [name for _, name in o["product_names"]],
            "source_url": safe_source_url(o["source_url"]),
            "signal_id": o["signal_id"] or "",
            "card_key": card_key(o["signal_id"]) if o["signal_id"] else "",
        })
    total = calendar["total"]
    coverage = (f"Checked {total} stored obligation"
                f"{'' if total == 1 else 's'}; "
                f"{len(rows)} apply to subsector "
                f"'{calendar['subsector'] or 'unknown'}'.")
    if calendar["unscoped"]:
        coverage += (f" {calendar['unscoped']} could not be evaluated "
                     "(unrecognized applicability rule) and are not shown.")
    excluded = calendar["excluded_by_entity"]
    if excluded:
        coverage += (f" {excluded} apply to that subsector but exclude this "
                     "account by name (not a plausible owner or operator of "
                     "the regulated assets) and are not shown.")
    return {"subsector": calendar["subsector"] or "unknown",
            "rows": rows, "coverage": coverage,
            "empty_message": CALENDAR_EMPTY_EXCLUDED if excluded
                             else CALENDAR_EMPTY}


_RELATIONSHIP_DIRECTION_LABELS = {"parent": "Parent of this account",
                                  "child": "Subsidiary of this account"}


def account_relationship_rows(relationships):
    """Rows for the Account 360 Entity Graph tab (R8.3).

    A sourced relationship LIST, not a drawn graph: entity_relationships holds
    one edge in the whole store, so there is no measured structure to draw and
    inventing one would be a fabricated claim (R4.1). Each row carries its own
    source and verification date so an unverified edge is visible as such.
    """
    return [{"direction": r["direction"],
             "direction_label": _RELATIONSHIP_DIRECTION_LABELS.get(
                 r["direction"], r["direction"]),
             "entity_id": r["entity_id"],
             "name": r["name"] or r["entity_id"],
             "subsector": str(r["subsector"] or "unknown"),
             "relationship_type": r["relationship_type"] or "",
             "source": r["source"] or "not recorded",
             "verified_at": r["verified_at"] or "not verified"}
            for r in relationships]


def provenance_view(signal, provenance):
    """The R3.7 reproducibility block: which raw event, which classifier
    version, which fetcher version, which scoring config, and when scored.

    The raw event reference goes through data.identity_safe_raw_event_ref, so a
    non-account card shows a sha256 digest and never the stored id — on this
    corpus that id embeds the source article URL, whose slug names the breached
    company, and this block renders on the feed, Account 360 and /card/ alike.

    A signal scored before migration 0011 has no scoring_config_version; it
    reads "pre-versioning" rather than being backfilled with today's token,
    which would be a fabricated provenance claim.
    """
    ref = data.identity_safe_raw_event_ref(
        _row_get(signal, "raw_event_id"), _row_get(signal, "signal_scope"))
    prov = provenance or {}
    return {
        "raw_event_ref": ref["ref"],
        "raw_event_hashed": ref["hashed"],
        "classifier_version": (prov.get("classifier_parser_version")
                               or prov.get("extraction_version")),
        "fetch_version": prov.get("fetch_parser_version"),
        "scoring_config_version": (_row_get(signal, "scoring_config_version")
                                   or "pre-versioning"),
        "scored_at": _row_get(signal, "scored_at") or "never scored",
    }


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

    incident_level = signal["incident_evidence_level"]
    unconfirmed = (incident_level == "unconfirmed_early_warning")
    is_incident = incident_level is not None
    # A confirmed/corroborated incident is cleared for customer-facing outreach.
    # Surfaced as a persistent positive marker so the up-tiered state is legible
    # on the resting card, mirroring the persistent "Outreach withheld" line on
    # the unconfirmed side (the two tiers were otherwise asymmetric: only the
    # restrictive state left a durable trace).
    incident_cleared = (is_incident and not unconfirmed
                        and bool(signal["customer_facing_allowed"]))

    classes = ["gs-card", f"sev-{band}"]
    if status == "superseded":
        classes.append("status-superseded")
    elif status == "decayed":
        classes.append("status-decayed")
    if unconfirmed:
        classes.append("tier-unconfirmed")

    # who + score moved out of the meta line into the card header row (the
    # entity name and the severity-colored score ring); the meta keeps the
    # trigger, date, and any non-active status.
    who = signal["entity_name"] or scope_label(signal["signal_scope"])
    # The source name is a discriminator, not decoration: both security_rss peer
    # paths mint a FIXED template headline, so several peer cards are identical
    # strings and only the reporting outlet tells them apart. Shown on every card
    # uniformly — honest provenance, and no conditional to get wrong. Falls back
    # to the raw source_id when the policy row carries no display name.
    meta_bits = [b for b in (
        signal["trigger_name"] or "",
        str(signal["event_date"] or ""),
        _row_get(signal, "source_name") or _row_get(signal, "source_id") or "",
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
        # R8.1 per-card permalink: the card is addressable at /card/{card_key}
        # (a dedicated read-only view) and anchorable in-page as #card-{card_key}.
        "card_key": card_key(signal["signal_id"]),
        "feedback_dom_id": feedback_dom_id(signal["signal_id"]),
        # R8.7 incident evidence-tier editor: an incident card (any tier) shows a
        # re-tier affordance targeting this CSS-safe id; non-incident cards don't.
        "is_incident": is_incident,
        "incident_level": incident_level,
        "tier_dom_id": tier_dom_id(signal["signal_id"]),
        # persistent marker that a confirmed/corroborated incident is cleared for
        # customer-facing outreach (mirrors the unconfirmed "withheld" line).
        "incident_cleared_note": (
            f"{incident_level.capitalize()} incident — cleared for "
            "customer-facing outreach." if incident_cleared else None),
        "card_class": " ".join(classes),
        "headline": signal["headline"] or "",
        "meta_bits": meta_bits,
        # card header row: severity badge (band class + label) + entity/scope
        # identity + score ring. The ring arc is a severity gauge — the score
        # clamped onto the 0-5 band scale (bands at 1.5/2.75/4) — distinct
        # from the decay bar (score vs fresh ceiling). None stays None so an
        # unscored signal renders plain text, never a fake-full ring.
        "who": who,
        "severity_band": band,
        "severity_label": band.capitalize(),
        "score_display": fmt_score(signal["score"]),
        "ring_pct": (round(min(max(float(signal["score"]), 0.0) / 5.0, 1.0) * 100)
                     if signal["score"] is not None else None),
        "decay": decay,
        "breakdown": score_breakdown(signal),
        "badges": _badges(signal, snapshots, legend),
        "chips": _chips(snapshots),
        "evidence": [{"text": ev["evidence_text"] or "",
                      "locator": ev["evidence_locator"] or ""}
                     for ev in evidence],
        # R4.1: a name-free sector card must not link a per-victim leak-site
        # permalink — ransomware.live's /id/ path base64-encodes the victim
        # name. The gate lives in data.py so no template change can reopen it.
        "source_url": safe_source_url(data.identity_safe_source_url(
            signal["source_url"], signal["signal_scope"])),
        # R10.5: an unverified early warning leads with a verification-first
        # warning; no confirmation exists yet from any authoritative source.
        "unconfirmed_warning": (
            "No company, regulator, or SEC confirmation yet — unverified "
            "early warning." if unconfirmed else None),
        # only the safe text, only when cleared — never the withheld text
        "outreach": outreach_text if show_outreach else None,
        # verification-first: an unconfirmed card withholds the opener even
        # before any play/snapshot exists (R7.12).
        "outreach_withheld": (
            (not show_outreach) and (bool(snapshots) or unconfirmed)),
        # R3.7: everything needed to reproduce this score, victim-name-free.
        "provenance": provenance_view(signal, detail.get("provenance")),
    }


def regulatory_chips(item):
    """Neutral descriptive chips for ONE regulatory-chatter record (R8.4/D8).

    Ported verbatim from app/ui/pages/6_Regulatory_Monitor._render_item so the
    two surfaces read identically: always a 'chatter' chip, then either the
    nerc_pages 'page snapshot' type label (its snapshots carry no compliance
    clock, so the always-true 'no compliance clock' chip is suppressed), a
    comment-period / effective anchor chip, or 'no compliance clock'. Never a
    scope/score badge — this is not a signal card. Returns plain strings; the
    template escapes them.
    """
    chips = ["chatter"]
    if item["source_id"] == "nerc_pages":
        chips.append(item["doc_type_label"])
    elif item["has_anchor"] and item["comments_close_on"]:
        chips.append(f"comment period closes {item['comments_close_on']}")
    elif item["has_anchor"] and item["effective_on"]:
        chips.append(f"effective {item['effective_on']}")
    else:
        chips.append("no compliance clock")
    return chips


def regulatory_records(items):
    """Shape data.regulatory_monitor() rows into template view dicts (R8.4).

    Pure display shaping: the source's own quoted headline, a neutral date +
    source meta line, the descriptive chips, and an HTTP(S)-only source link
    (non-clickable schemes drop out via safe_source_url — XSS guard). The
    template autoescapes every string.
    """
    views = []
    for item in items:
        meta = " · ".join(b for b in (
            str(item["event_date"] or ""),
            item["source_name"] or item["source_id"],
        ) if b)
        views.append({
            "headline": item["headline"] or "",
            "meta": meta,
            "chips": regulatory_chips(item),
            "source_url": safe_source_url(item["url"]),
        })
    return views


# -- Explore: Trigger Analytics + evidence-safe Watchlist Map (R8.5, R4.1) ----
#
# Two view shapers for the Explore page. Analytics reshapes data's three sliced
# counts into template-ready table dicts; the map builds a self-contained inline
# <svg> (baked US-states geometry from us_geometry + KTD5 projection).
#
# TRUST INVARIANTS carried to the DOM:
#   * R4.1: every analytics row keeps its dimension identity (trigger/scope/tier
#     key + label) so a count is inspectable back to the same sourced signals the
#     cards render — it is a count of sourced rows, never a bare number.
#   * R8.5 evidence gate: the map draws a facility <circle> ONLY for a point the
#     reader already gated (>=0.85). This shaper never re-derives a point; it
#     renders exactly what data.explore_facility_points returned. AK/HI/offshore
#     points that project outside the continental bounds are omitted (never
#     mis-placed on a wrong state), via us_geometry.project_or_none.
#   * Volume != confidence (D5): the choropleth uses a FIXED density scale with a
#     palette DELIBERATELY DISTINCT from the evidence-tier / scope badge palette
#     (the "gs-map-d0..d4" classes below, styled apart in app.css), so a dense
#     state is never misread as a high-confidence one.
#   * R6.6: empty input -> the base geography still renders with an honest
#     "no facility-level evidence yet" note, never a broken/blank surface.
#
# The <svg> string is emitted with manual html.escape on every interpolated
# value (state names, entity names, counts) because the template renders it with
# |safe — the escaping that Jinja does elsewhere is done here by hand instead,
# in exactly one place, so upstream text can never break out of an attribute or
# a <title>.

from html import escape as _esc

# Fixed choropleth density buckets (upper-bound inclusive; the last is open).
# Deliberately coarse and fixed so the shade means the same thing on every load
# and is not a per-request relative scale. Class names map to app.css swatches
# that are a distinct hue ramp from the badge palette (D5).
_MAP_DENSITY_BUCKETS = ((0, "gs-map-d0"), (2, "gs-map-d1"), (6, "gs-map-d2"),
                        (14, "gs-map-d3"))
_MAP_DENSITY_TOP = "gs-map-d4"
# Human labels for the fixed scale legend (parallel to the buckets above).
MAP_DENSITY_LEGEND = ("0", "1–2", "3–6", "7–14", "15+")


def map_density_class(count):
    """Map a per-state signal count to its FIXED-scale choropleth class (D5).
    A count of 0 reads the base 'gs-map-d0' shade (an empty state, not a broken
    one). The scale is absolute, not relative to the busiest state, so the same
    count always reads the same shade."""
    n = count or 0
    for upper, cls in _MAP_DENSITY_BUCKETS:
        if n <= upper:
            return cls
    return _MAP_DENSITY_TOP


def explore_analytics_view(counts):
    """Reshape data.explore_analytics_counts into template-ready tables (R8.5,
    R4.1). Returns a list of {'label','dimension','rows':[{key,label,count}]}
    tables, one per dimension that has any rows; each row keeps its dimension
    identity so the count is inspectable back to its sourced signals. Empty
    dimensions drop out; a wholly-empty store yields an empty list (the route /
    template then shows the tab's honest empty-state copy, R6.6)."""
    dimensions = (
        ("trigger", "Trigger"),
        ("scope", "Signal scope"),
        ("incident_tier", "Incident tier"),
    )
    tables = []
    for key, label in dimensions:
        rows = counts.get(key) or []
        if not rows:
            continue
        tables.append({
            "dimension": key,
            "label": label,
            "rows": [{"key": r["key"], "label": r["label"] or r["key"],
                      "count": r["count"]} for r in rows],
        })
    return tables


# -- Explore: Ransomware Activity (R8.5, R4.1, R6.6) -------------------------
#
# Display shaping ONLY. Every privacy decision in this panel was already made in
# data.ransomware_activity (singleton crews withheld there, marginals never
# cross-tabbed); this shaper adds no data and re-derives nothing, so there is no
# second place a leak could be introduced. Bar widths are percentages of the
# largest row in the SAME list — a relative bar for reading rank at a glance,
# never compared across the two lists.

# Kept out of the template so the framing is reviewable in one place with the
# rest of the trust copy, matching the route-level empty-state constants.
RANSOMWARE_CAPTION = (
    "Situational awareness only — leak-site listings are unverified claims by "
    "ransomware crews (R10.5), counted here as threat activity. Nothing on this "
    "tab is scored, attributed to a watchlist account, or implies an account is "
    "affected. Only the watchlist-industry rows below mint sector-peer cards.")

# The single most-misread thing on this panel: "Industries hit" counts the
# industries of leak-site victims WORLDWIDE, not the composition of our own
# watchlist (which is energy-only, by construction). The operator read the
# table as the watchlist during the persona pass, so the subject is stated on
# the page rather than left to inference.
RANSOMWARE_SUBJECT_NOTE = (
    "These are ransomware victims worldwide, not GridSignals watchlist "
    "companies — the watchlist is energy-only, and appears here solely as the "
    "highlighted row. Industry is the tracker's own tag on each victim.")


def _delta_word(n):
    """Signed change, always carrying its sign so it cannot be read as a count.
    Zero says so in words rather than printing '+0'."""
    if n > 0:
        return f"+{n}"
    if n < 0:
        return str(n)
    return "no change"


def _span_label(start, end):
    """A one-day half is a date, not a range: '2026-08-13 → 2026-08-13' invites
    the reader to hunt for the difference between two identical dates."""
    return start if start == end else f"{start} → {end}"


def _ransomware_baseline_note(activity):
    """Prior-vs-current sentence for the panel, with BOTH n's (R8.5, R6.6).

    A bare delta on a corpus this thin is unreadable, so the note always states
    the two counts and the two date ranges they cover, and names the days held
    out of the comparison. When the corpus is too short it says "no comparable
    prior window" and stops — an honest absence beats a fabricated rise, and the
    partial boundary days that would fabricate it are named on the page.
    """
    b = activity.get("baseline") or {}
    boundary = b.get("excluded_boundary") or []
    if not b.get("available"):
        covered = b.get("covered_days") or 0
        note = (
            "No comparable prior window: the stored corpus covers "
            f"{covered} {'day' if covered == 1 else 'days'}, and both its "
            "oldest and newest day are partial — the oldest is clipped by the "
            "feed's fixed record cap, the newest by the time of the ingest run. "
            "A comparison needs a whole day on each side of those, so no "
            "comparison is reported rather than a number the corpus cannot "
            "support.")
        return note
    half = b["half_days"]
    current_span = _span_label(b["current_start"], b["current_end"])
    prior_span = _span_label(b["prior_start"], b["prior_end"])
    if b.get("subject_available"):
        subject = (
            f"watchlist industry: {b['current_peer']} listings "
            f"({current_span}) against {b['prior_peer']} ({prior_span}), "
            f"{_delta_word(b['peer_delta'])}.")
    else:
        # Zero on both sides is not "no change" — see the subject floor note in
        # app/ui/data.py. Say the comparison is unsupported and still show the
        # two zeroes, so the reader can see WHY rather than take it on trust.
        # "no change" is reserved for a measured zero (_delta_word), so it is
        # deliberately not reused here — the two would be indistinguishable.
        subject = (
            "watchlist industry: too few listings to compare — none in "
            f"{current_span} and none in {prior_span}, so no comparison is "
            "possible on that row.")
    note = (
        f"Change vs prior window — {subject} "
        f"All listings: {b['current_total']} against {b['prior_total']}, "
        f"{_delta_word(b['total_delta'])}. Equal {half}-day halves.")
    if boundary:
        note += (
            f" {' and '.join(boundary)} "
            f"{'is' if len(boundary) == 1 else 'are'} excluded as partial days: "
            "the oldest day the feed returns is clipped by its record cap and "
            "the newest by the time of the ingest run, so counting either would "
            "invent a trend out of how the data was fetched.")
    middle = b.get("excluded_middle") or []
    if middle:
        note += (f" {', '.join(middle)} is held out as well, so the two halves "
                 "are the same length.")
    return note


def _bar_pct(count, top):
    """Row width as a whole percent of the largest row in its own list."""
    if not top or count <= 0:
        return 0
    return max(2, round(100 * count / top))


def _ransomware_rows(rows):
    """Attach a relative bar width to a count list, largest row first."""
    top = max((r["count"] for r in rows), default=0)
    return [dict(r, bar=_bar_pct(r["count"], top)) for r in rows]


def ransomware_activity_view(activity):
    """Shape data.ransomware_activity into a template view dict (R8.5, R4.1).

    Adds the derived window label, the relative bar widths, and the honest notes
    for what is deliberately NOT shown — the crews withheld under the
    single-listing privacy floor and the listings the tracker never classified.
    ``empty`` drives the R6.6 empty state; ``peer_note`` states the peer count in
    the same breath as the total so the panel never reads as if the whole feed
    were peer activity.
    """
    total = activity.get("total") or 0
    start = activity.get("window_start") or ""
    end = activity.get("window_end") or ""
    days = activity.get("window_days") or 0

    window = ""
    if start and end:
        span = f"{days} day" if days == 1 else f"{days} days"
        window = f"{start} → {end} · {span}"

    withheld = activity.get("crews_withheld") or 0
    withheld_listings = activity.get("crews_withheld_listings") or 0
    crew_note = ""
    if withheld:
        # States the gate as it actually is — a distinct-VICTIM floor, not a
        # listing floor. One victim can appear as several listings when the
        # tracker revises a record, and such a crew is still withheld.
        crew_note = (
            f"{withheld} further "
            f"{'crew is' if withheld == 1 else 'crews are'} not named here: "
            f"{'it is' if withheld == 1 else 'each is'} tied to a single victim "
            f"({withheld_listings} "
            f"{'listing' if withheld_listings == 1 else 'listings'} in total), "
            "and a crew that has named itself after its one victim would "
            "identify that company (R4.1). They are still counted in the total.")

    unclassified = activity.get("unclassified") or 0
    industry_note = ""
    if unclassified:
        industry_note = (
            f"{unclassified} of {total} listings carry no industry from the "
            "tracker and are counted as unclassified rather than dropped — an "
            "unknown industry is not evidence of a match, so none of them mint "
            "a peer card.")

    peers = activity.get("peer_listings") or 0
    peer_note = (
        f"{peers} of {total} listings are in the watchlist's own industry"
        f"{' — the only rows that mint sector-peer cards.' if peers else '.'}")

    # The unclassified bucket gets a BAR, not just a footnote. It is routinely
    # the largest bucket in the corpus (17 of 100 today, against 15 for the
    # biggest named industry), and a chart that omits its largest bar while
    # showing the tracker's own catch-all "Other" tells a skimmer — or a
    # screenshot — that the top named industry is the most-targeted one. It is
    # never flagged is_peer: an unknown industry is not evidence of a match.
    industries = list(activity.get("industries") or [])
    if unclassified:
        industries.append({"label": "No industry given", "count": unclassified,
                           "is_peer": False, "rank": 0, "unclassified": True})
        # Re-sorted with is_peer as the PRIMARY key, matching data.py: inserting
        # the unclassified bar must not undo the subject-first order.
        industries.sort(key=lambda r: (not r.get("is_peer"), -r["count"],
                                       r["label"]))

    # World volume rank of the watchlist's own industry — stated as context
    # under the subject, never as the panel's headline (R8.5).
    peer_rank = activity.get("peer_rank") or 0
    industry_rows = activity.get("industry_rows") or 0
    subject_rank_note = ""
    if peer_rank and industry_rows:
        subject_rank_note = (
            f"By raw volume that is rank {peer_rank} of {industry_rows} "
            "industries worldwide — world ranking is context here, not the "
            "finding.")

    baseline = activity.get("baseline") or {}
    # The lede's delta is withheld unless the WATCHLIST row itself has a
    # listing on one side of the split (data._ransomware_baseline). The note
    # carries the full phrase so the template never has to compose one.
    subject_delta = (_delta_word(baseline["peer_delta"])
                     if baseline.get("subject_available") else "")
    if subject_delta:
        subject_delta_note = f"{subject_delta} vs prior window"
    elif baseline.get("available"):
        subject_delta_note = "too few watchlist listings to compare"
    else:
        subject_delta_note = "no prior window"

    return {
        "total": total,
        "subject_count": peers,
        "subject_delta": subject_delta,
        "subject_delta_note": subject_delta_note,
        "subject_rank_note": subject_rank_note,
        "baseline_available": bool(baseline.get("available")),
        "baseline_note": _ransomware_baseline_note(activity),
        "window": window,
        "source_name": activity.get("source_name") or "",
        "source_url": safe_source_url(activity.get("source_url")),
        "source_line": _ransomware_source_line(activity),
        "stale": activity.get("source_state") in ("stale", "error",
                                                  "never_run"),
        "crews": _ransomware_rows(activity.get("crews") or []),
        "crew_total": activity.get("crew_total") or 0,
        "crew_note": crew_note,
        "industries": _ransomware_rows(industries),
        "industry_note": industry_note,
        "unclassified": unclassified,
        "peer_listings": peers,
        "peer_note": peer_note,
        "caption": RANSOMWARE_CAPTION,
        "subject_note": RANSOMWARE_SUBJECT_NOTE,
        "empty": total == 0,
    }


# How a source's run state reads on this panel. 'ok' still states the ingest
# time — the window is derived from event dates alone, so without this a feed
# that stalled a month ago renders exactly like a live one.
_RANSOMWARE_FRESHNESS = {
    "ok": "last ingested",
    "stale": "STALE — last ingested",
    "error": "INGEST ERROR — last succeeded",
    "never_run": "never ingested",
    "disabled": "source disabled — last ingested",
}


def _ransomware_source_line(activity):
    """Attribution + freshness + coverage for the panel byline (R10.4, R6.6).

    Credit is a licence condition here (ransomware.live is CC-BY-4.0), so the
    source is named on the populated panel and not only in its empty state.

    Coverage is stated alongside it because the window label alone is
    misleading: a corpus assembled from ONE run of a rolling recent feed is a
    snapshot of whatever the feed was holding at that moment, not a series
    accumulated day by day, and the baseline delta below is only as good as that
    provenance.
    """
    name = activity.get("source_name") or ""
    state = activity.get("source_state") or "never_run"
    last = activity.get("last_success_at") or ""
    prefix = _RANSOMWARE_FRESHNESS.get(state, "last ingested")
    if state == "never_run" or not last:
        line = f"Source: {name} · never ingested".strip()
    else:
        line = f"Source: {name} · {prefix} {last}"
    runs = activity.get("run_count") or 0
    if runs == 1:
        line += " · coverage is a single ingest run of a rolling feed"
    elif runs > 1:
        line += f" · coverage accumulated over {runs} ingest runs"
    return line


def _state_density(state_rows):
    """Sum per-state signal density from data.explore_state_density rows by
    projecting each gated facility to its state. Returns {usps: total_count}.
    A facility that projects outside the continental bounds is skipped (its
    density is not attributed to any state — AK/HI limitation, documented).
    The same owner is counted at most once per state, so multiple facilities for
    one entity in Texas do not multiply that entity's signal volume."""
    # Precompute state path bounding boxes once so we can attribute a projected
    # point to the state whose bbox contains it (point-in-bbox, matching the
    # spike's regression method — the paths are simple enough that bbox is a
    # sufficient, cheap containment test at this scale).
    density = {}
    seen_owner_states = set()
    for r in state_rows:
        xy = us_geometry.project_or_none(r["longitude"], r["latitude"])
        if xy is None:
            continue
        usps = _state_for_point(*xy)
        if usps is None:
            continue
        owner_state = (usps, r.get("entity_id"))
        if owner_state in seen_owner_states:
            continue
        seen_owner_states.add(owner_state)
        density[usps] = density.get(usps, 0) + (r["signal_count"] or 0)
    return density


# State path bounding boxes, computed once from the baked geometry. Used to
# attribute a projected facility point to a state for the choropleth density.
def _is_float(tok):
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _bbox_of_path(d):
    """Bounding box (x0, y0, x1, y1) of an SVG path d-string. The d-string is a
    stream of ``M``/``L``/``Z`` commands with ``x y`` coordinate pairs; the
    numeric tokens alternate x, y, x, y, so even-index numbers are xs and
    odd-index are ys. Returns None for a path with no coordinates."""
    nums = [float(t) for t in d.replace(",", " ").split() if _is_float(t)]
    xs, ys = nums[0::2], nums[1::2]
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


_STATE_BBOXES = tuple(
    (usps, _bbox_of_path(d))
    for usps, _name, d in us_geometry.STATE_PATHS
    if _bbox_of_path(d) is not None)


def _state_for_point(x, y):
    """The USPS code of the smallest state bbox containing (x, y), or None.
    Smallest-bbox-wins so an eastern-seaboard point in overlapping bboxes is
    attributed to the tightest (usually correct) state."""
    best = None
    best_area = None
    for usps, (x0, y0, x1, y1) in _STATE_BBOXES:
        if x0 <= x <= x1 and y0 <= y <= y1:
            area = (x1 - x0) * (y1 - y0)
            if best_area is None or area < best_area:
                best, best_area = usps, area
    return best


def explore_map_svg(facility_points, state_rows):
    """Build the self-contained inline Watchlist Map <svg> (R8.5, R6.6, KTD5).

    ``facility_points`` = data.explore_facility_points (already >=0.85 gated —
    this shaper renders exactly those, gating nothing itself). ``state_rows`` =
    data.explore_state_density (for the choropleth). Returns
    {'svg': <markup>, 'has_facilities': bool, 'empty_note': str|None}. Every
    state <path> and facility <circle> carries a native SVG <title> (zero-JS,
    CSP-safe) with its identity + count / subsector / entity. A facility outside
    the continental bounds (AK/HI/offshore) is omitted, never mis-projected.
    Empty facility input still renders the base geography plus an honest note.
    """
    density = _state_density(state_rows)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{us_geometry.VIEWBOX}" class="gs-map" role="img" '
        f'aria-label="US watchlist map: signal density by state, gated '
        f'facilities as points">']
    # States, each shaded by its fixed-scale density and titled with its count.
    for usps, name, d in us_geometry.STATE_PATHS:
        count = density.get(usps, 0)
        cls = map_density_class(count)
        plural = "signal" if count == 1 else "signals"
        title = f"{name}: {count} {plural}"
        parts.append(
            f'<path class="gs-map-state {cls}" data-state="{_esc(usps, True)}" '
            f'd="{d}"><title>{_esc(title)}</title></path>')
    # Gated facility points only. Project each; omit any that fall outside the
    # continental bounds so an AK/HI/offshore facility never lands on a wrong
    # state (KTD5 / us_geometry.project_or_none).
    drawn = 0
    for f in facility_points:
        xy = us_geometry.project_or_none(f.get("longitude"), f.get("latitude"))
        if xy is None:
            continue
        x, y = xy
        entity = f.get("entity_name") or f.get("entity_id") or "(unlinked owner)"
        subsector = f.get("subsector") or "unknown subsector"
        fac_name = f.get("facility_name") or f.get("facility_id") or "facility"
        title = f"{fac_name} — {entity} · {subsector}"
        parts.append(
            f'<circle class="gs-map-facility" cx="{x:.2f}" cy="{y:.2f}" '
            f'r="4"><title>{_esc(title)}</title></circle>')
        drawn += 1
    parts.append("</svg>")
    empty_note = None
    if drawn == 0:
        # R6.6: honest low-evidence state — the base geography still renders, but
        # we say plainly there is no facility-level evidence to plot yet, rather
        # than implying an empty map means "no activity".
        empty_note = ("No facility-level evidence yet — only facilities with a "
                      "high-confidence owner match (≥0.85) are plotted, and none "
                      "qualify today. The state shading above reflects signal "
                      "volume, not facility coverage.")
    return {"svg": "".join(parts), "has_facilities": drawn > 0,
            "empty_note": empty_note}
