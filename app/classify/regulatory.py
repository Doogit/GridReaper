"""Regulatory classifier: Federal Register + NERC page diffs (R4.1, R7.1,
R7.2, R8.4, R9.4).

Precision over recall (R9.4): the stored Federal Register corpus is mostly
routine FERC business (hydro licenses, rate filings, meeting notices), so
every rule below is a strict conjunction and borderline rules were dropped
rather than loosened.

federal_register accept rules (agency matched by slug, terms matched
case-insensitively over title + abstract):

  trigger                 type            required terms
  ----------------------  --------------  ---------------------------------
  tsa_security_directive  Rule /          TSA agency + a security-directive
                          Proposed Rule   term (TSA_TERMS) + a pipeline/LNG
                                          term (app/classify/tsa_sd.py,
                                          R4.1 - TSA_TERMS alone also matches
                                          rail/aviation/general surface-
                                          transportation security, which
                                          app/obligations.py's APPLICABILITY
                                          would otherwise mismap to the
                                          lng/midstream subsectors)
  nerc_cip_revision       Rule /          FERC agency + "reliability
                          Proposed Rule   standard" + CIP context ("critical
                                          infrastructure protection", a
                                          CIP-### id, an RM/RD docket, or a
                                          CFR 18 Part 40 reference)
  nerc_enforcement        Notice only     FERC agency + an enforcement term
                                          AND a CIP/reliability term (strict
                                          conjunction)

Durable compliance clock (R7.2/R8.4): every Federal Register candidate also
requires an anchor - effective_on set, comments_close_on set, or explicit
compliance-obligation wording in the abstract. Anchorless reliability
chatter is skipped (that belongs to a later Regulatory Monitor chunk, not
the signal feed).

Scope: effective_on set -> sector (the compliance clock is running for the
affected class); otherwise -> regulatory_calendar (upcoming milestone).
Headlines are "{agency} {doc-type label}: {document title}" - the label
comes from the document's own type field and the title is quoted, so the
headline never claims an action (adopts/directs/approves) the payload does
not itself state, and same-day sibling documents stay distinguishable.
nerc_enforcement does not allow regulatory_calendar, so it always emits
sector; additionally, when the title's leading "Company Name; Notice of..."
segment carries a clear corporate-suffix token (Inc/LLC/Corp/...), an
account-scoped candidate is emitted with that segment as entity_name_hint -
the framework resolves it against the watchlist or drops/queues it (R6.2).
Sector and regulatory_calendar headlines are affected-class statements,
never account-specific.

nerc_pages: each snapshot is diffed at the token level
(difflib.SequenceMatcher over text.split() - the stored text is one long
whitespace-collapsed line, so line diffs are useless) against the
immediately preceding snapshot of the same page_url. The first-ever
snapshot of a page emits nothing. Added/replaced token regions mentioning a
CIP standard id (CIP-###) yield one nerc_cip_revision sector candidate per
changed standard; evidence is the changed region plus a little context
(locator "page_diff"). Note: the framework's deterministic signal id
collapses multiple sector candidates from one snapshot into one signal -
the first changed standard wins.

Dropped for precision (deliberate, not gaps):
  - TSA Notices (real security directives occasionally surface as notices
    of availability, but TSA Notices are dominated by fee/PRA chatter).
  - TSA rulemakings that match TSA_TERMS but name no pipeline/LNG facility
    (rail security, aviation, general surface-transportation security) -
    app/classify/tsa_sd.py's pipeline/LNG gate (R8, U7 of the combo-engine
    plan).
  - FERC "Notice of Filing" of NERC standard petitions (the Notice type is
    reserved for enforcement's strict keyword conjunction).
  - Any reliability/security doc with no date anchor or compliance wording.

Run: python -m app.classify.regulatory [--source X] [--limit N] [--force]
"""
import difflib
import json
import re
import sys

from app.classify import runner as classify_runner
from app.classify.tsa_sd import is_pipeline_lng_relevant

CLASSIFIER_ID = "regulatory"
# 1.0 -> 1.1 (R8, U7): tsa_security_directive now also requires a pipeline/
# LNG term (app/classify/tsa_sd.py) - the version bump lets a --force run
# self-correct any stored signal the old, looser rule would have minted
# (there are none against the real corpus as of this change, but the bump
# keeps signal_evidence.extraction_version honest per the REFRESH mechanism
# in app/classify/runner.py, same as ransomware's 1.0 -> 1.1 precedent).
PARSER_VERSION = "regulatory/1.1"

FERC_SLUG = "federal-energy-regulatory-commission"
TSA_SLUG = "transportation-security-administration"
RULEMAKING_TYPES = {"Rule", "Proposed Rule"}

TSA_TERMS = ("security directive", "pipeline security", "rail security",
             "surface transportation security", "cybersecurity requirements")
RELIABILITY_PHRASES = ("critical infrastructure protection",
                       "reliability standard")

_ENFORCEMENT_RE = re.compile(
    r"\b(enforcement|civil penalt(?:y|ies)|penalt(?:y|ies)|violations?|"
    r"settlements?)\b", re.IGNORECASE)
_CIP_TOKEN_RE = re.compile(r"\bCIP\b")            # case-sensitive on purpose
_CIP_STD_RE = re.compile(r"\bCIP-(\d{3})\b")
_COMPLIANCE_RE = re.compile(
    r"must comply|required to comply|compliance (?:date|deadline|"
    r"obligations?)", re.IGNORECASE)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# A leading title segment only counts as a company name with one of these.
_CORP_SUFFIXES = {"inc", "incorporated", "llc", "lc", "llp", "lp", "ltd",
                  "limited", "corp", "corporation", "company", "co", "plc"}

FR_CONFIDENCE = 0.8
ACCOUNT_CONFIDENCE = 0.75
MAX_HEADLINE_CHARS = 140
DIFF_CONFIDENCE = 0.65
DIFF_CONTEXT_TOKENS = 8
MAX_SNIPPET_CHARS = 400
MAX_ABSTRACT_SENTENCES = 2


def _headline(agency, doc_type, title):
    """'{agency} {type label}: {title}' - claims only what the payload
    states (the type field), keeps sibling documents distinguishable."""
    label = {"Rule": "final rule", "Proposed Rule": "proposed rule",
             "Notice": "notice"}.get(doc_type, doc_type or "document")
    text = f"{agency} {label}: {title}"
    if len(text) > MAX_HEADLINE_CHARS:
        text = text[:MAX_HEADLINE_CHARS - 1].rstrip() + "…"
    return text


def _agency_slugs(payload):
    return {a.get("slug") for a in payload.get("agencies") or []
            if isinstance(a, dict)}


def _has_reliability_term(text):
    lower = text.lower()
    return (any(p in lower for p in RELIABILITY_PHRASES)
            or bool(_CIP_TOKEN_RE.search(text)))


def _has_cip_context(payload, text):
    """CIP context for nerc_cip_revision: explicit CIP wording, an RM/RD
    rulemaking docket, or a CFR 18 Part 40 reference."""
    if "critical infrastructure protection" in text.lower():
        return True
    if _CIP_STD_RE.search(text):
        return True
    if any(str(d).strip().upper().startswith(("RM", "RD"))
           for d in payload.get("docket_ids") or []):
        return True
    return any(isinstance(c, dict) and str(c.get("title")) == "18"
               and str(c.get("part")) == "40"
               for c in payload.get("cfr_references") or [])


def _matched_sentences(abstract, matcher):
    """Up to MAX_ABSTRACT_SENTENCES abstract sentences that satisfy
    ``matcher(sentence)`` - the evidence for why the doc matched (R4.1)."""
    out = []
    for s in _SENT_SPLIT.split((abstract or "").strip()):
        s = s.strip()
        if s and matcher(s):
            out.append(s)
            if len(out) == MAX_ABSTRACT_SENTENCES:
                break
    return out


def _evidence(title, sentences, effective_on, comments_close_on):
    ev = [{"text": title, "locator": "title"}]
    ev.extend({"text": s, "locator": "abstract"} for s in sentences)
    if effective_on:
        ev.append({"text": f"Effective on {effective_on}",
                   "locator": "effective_on"})
    if comments_close_on:
        ev.append({"text": f"Comments close on {comments_close_on}",
                   "locator": "comments_close_on"})
    return ev


def _named_company(title):
    """Leading 'Company Name; Notice of ...' segment of a Federal Register
    title, accepted only when it carries a clear corporate-suffix token
    (conservative named-entity rule, R9.4)."""
    seg, sep, _rest = title.partition(";")
    if not sep:
        return None
    seg = seg.strip()
    if len(seg) > 80:
        return None
    tokens = [t for t in (re.sub(r"[^\w]", "", t).lower()
                          for t in seg.split()) if t]
    if len(tokens) >= 2 and any(t in _CORP_SUFFIXES for t in tokens):
        return seg
    return None


def classify_federal_register(conn, raw):
    """Federal Register document -> regulatory trigger candidates. See the
    module docstring for the accept-rule table."""
    try:
        payload = json.loads(raw["payload"] or "")
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    title = (payload.get("title") or "").strip()
    if not title:
        return []
    abstract = payload.get("abstract") or ""
    doc_type = payload.get("type") or ""
    effective_on = payload.get("effective_on") or None
    comments_close_on = payload.get("comments_close_on") or None

    # Durable compliance clock (R7.2/R8.4): no anchor, no signal.
    if not (effective_on or comments_close_on
            or _COMPLIANCE_RE.search(abstract)):
        return []

    slugs = _agency_slugs(payload)
    text = f"{title} {abstract}"
    lower = text.lower()
    event_date = payload.get("publication_date") or raw["event_date"] or ""
    scope = "sector" if effective_on else "regulatory_calendar"
    candidates = []

    if (TSA_SLUG in slugs and doc_type in RULEMAKING_TYPES
            and any(t in lower for t in TSA_TERMS)
            and is_pipeline_lng_relevant(lower)):
        candidates.append({
            "trigger_id": "tsa_security_directive", "signal_scope": scope,
            "entity_id": None, "entity_name_hint": None,
            "event_date": event_date,
            "headline": _headline("TSA", doc_type, title),
            "evidence": _evidence(
                title,
                _matched_sentences(
                    abstract,
                    lambda s: any(t in s.lower() for t in TSA_TERMS)),
                effective_on, comments_close_on),
            "confidence": FR_CONFIDENCE,
        })

    if (FERC_SLUG in slugs and doc_type in RULEMAKING_TYPES
            and "reliability standard" in lower
            and _has_cip_context(payload, text)):
        candidates.append({
            "trigger_id": "nerc_cip_revision", "signal_scope": scope,
            "entity_id": None, "entity_name_hint": None,
            "event_date": event_date,
            "headline": _headline("FERC", doc_type, title),
            "evidence": _evidence(
                title, _matched_sentences(abstract, _has_reliability_term),
                effective_on, comments_close_on),
            "confidence": FR_CONFIDENCE,
        })

    if (FERC_SLUG in slugs and doc_type == "Notice"
            and _ENFORCEMENT_RE.search(text)
            and _has_reliability_term(text)):
        evidence = _evidence(
            title,
            _matched_sentences(
                abstract, lambda s: bool(_ENFORCEMENT_RE.search(s))),
            effective_on, comments_close_on)
        # Sector headline stays class-phrased WITHOUT the company name:
        # the trigger is URE-anonymized by default (attribution gated) and
        # sector cards explain affected classes, never a named account
        # (R7.2). The named company appears only in the account-scoped
        # candidate below and in the rank-1 title evidence.
        candidates.append({
            "trigger_id": "nerc_enforcement", "signal_scope": "sector",
            "entity_id": None, "entity_name_hint": None,
            "event_date": event_date,
            "headline": ("FERC enforcement notice: CIP/reliability "
                         "enforcement action affecting registered entities"),
            "evidence": evidence,
            "confidence": FR_CONFIDENCE,
        })
        name = _named_company(title)
        if name:
            candidates.append({
                "trigger_id": "nerc_enforcement", "signal_scope": "account",
                "entity_id": None, "entity_name_hint": name,
                "event_date": event_date,
                "headline": f"FERC enforcement action names {name}",
                "evidence": evidence,
                "confidence": ACCOUNT_CONFIDENCE,
            })
    return candidates


def _previous_snapshot(conn, raw, page_url):
    """Text of the most recent earlier snapshot of the same page_url, or
    None when this is the page's first snapshot."""
    for r in conn.execute(
            "SELECT payload FROM raw_events WHERE source_id = ? "
            "AND first_seen_at < ? ORDER BY first_seen_at DESC",
            (raw["source_id"], raw["first_seen_at"])):
        try:
            p = json.loads(r["payload"] or "")
        except ValueError:
            continue
        if isinstance(p, dict) and p.get("page_url") == page_url:
            return p.get("text") or ""
    return None


def classify_nerc_pages(conn, raw):
    """NERC page snapshot -> nerc_cip_revision candidates via token-level
    diff against the previous snapshot of the same page."""
    try:
        payload = json.loads(raw["payload"] or "")
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    page_url = payload.get("page_url")
    if not page_url:
        return []
    prev_text = _previous_snapshot(conn, raw, page_url)
    if prev_text is None:
        return []

    old_tokens = prev_text.split()
    new_tokens = (payload.get("text") or "").split()
    event_date = payload.get("fetched_date") or raw["event_date"] or ""
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens,
                                      autojunk=False)
    changed = {}    # standard id -> evidence snippet (first region wins)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("replace", "insert"):
            continue
        region = " ".join(new_tokens[j1:j2])
        for m in _CIP_STD_RE.finditer(region):
            std = f"CIP-{m.group(1)}"
            if std in changed:
                continue
            lo = max(0, j1 - DIFF_CONTEXT_TOKENS)
            hi = min(len(new_tokens), j2 + DIFF_CONTEXT_TOKENS)
            changed[std] = " ".join(new_tokens[lo:hi])[:MAX_SNIPPET_CHARS]

    return [{
        "trigger_id": "nerc_cip_revision", "signal_scope": "sector",
        "entity_id": None, "entity_name_hint": None,
        "event_date": event_date,
        "headline": (f"NERC standards page update: {std} changed - CIP "
                     "revision activity affecting registered entities"),
        "evidence": [{"text": snippet, "locator": "page_diff"}],
        "confidence": DIFF_CONFIDENCE,
    } for std, snippet in sorted(changed.items())]


SOURCES = {
    "federal_register": classify_federal_register,
    "nerc_pages": classify_nerc_pages,
}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify Federal Register documents and NERC page diffs into "
        "regulatory signals."))
