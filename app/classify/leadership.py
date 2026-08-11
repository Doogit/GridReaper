"""Leadership-change classifier (R7.1, R7.2, R4.1).

Rule-based (no ML) detection of security-relevant executive appointments
across three sources, run through the classification framework
(``app.classify.runner`` owns resolution, review routing, dedupe):

  presswire_prnewswire / presswire_globenewswire
    The headline must contain an appointment verb (appoints, names, hires,
    taps, promotes, elevates, welcomes, or "joins <company> as") followed by
    a security-relevant executive title in the post-verb role region (the
    text after the first "as" following the verb, else everything after the
    verb) - this rejects "former CISO of X joins Acme as CFO". The company
    hint is the title text before the verb (or between "joins" and "as");
    an implausible hint (empty, >10 words, >80 chars, no letters) emits
    nothing. Description HTML is stripped and only its first 600 chars are
    consulted, solely as security context for a bare "CSO" match.

  sec_edgar_submissions
    8-K / 8-K/A with Item 5.02 AND a security-relevant title token in the
    only text fields the submissions payload carries (primaryDocDescription,
    primaryDocument). Item 5.02 alone is never sufficient - most 5.02s are
    routine officer/director churn. KNOWN RECALL LIMITATION: submissions
    payloads have no filing body text and primaryDocDescription is usually
    just "8-K", so this path emits at or near zero signals at MVP; fetching
    filing documents to recover that recall is out of scope (no network in
    classification).

Titles and confidence: CISO / Chief Information Security Officer / Chief
Security Officer -> 0.9; CIO / Chief Information Officer / CTO / Chief
Technology Officer -> 0.75; bare "CSO" counts (0.9) only when security
context ("chief security officer", "information security", "cybersecurity")
appears in the title or description window, because CSO frequently means
Chief Strategy/Sustainability Officer. Acronyms match uppercase-only.
CFO/CEO/COO/board-director changes are not signals. Precision over recall
throughout.

  python -m app.classify.leadership [--source X] [--limit N] [--force]
"""
import html
import json
import re
import sys

from app.classify import runner as classify_runner

CLASSIFIER_ID = "leadership"
PARSER_VERSION = "leadership/1.0"
TRIGGER_ID = "leadership_change"

CONFIDENCE_SECURITY = 0.9   # explicit security-executive title
CONFIDENCE_IT_EXEC = 0.75   # CIO/CTO: security-adjacent, weaker buy signal

DESC_WINDOW = 600           # chars of stripped description consulted
EDGAR_FORMS = ("8-K", "8-K/A")
EDGAR_ITEM = "5.02"
EDGAR_TEXT_FIELDS = ("primaryDocDescription", "primaryDocument")

# Acronyms are uppercase-only on purpose: lowercase "ciso"/"cio" in prose is
# rare and matching it case-insensitively collides with ordinary words.
_SECURITY_TITLES = [
    re.compile(r"\bCISO\b"),
    re.compile(r"\bchief information security officer\b", re.IGNORECASE),
    re.compile(r"\bchief security officer\b", re.IGNORECASE),
]
_IT_EXEC_TITLES = [
    re.compile(r"\bCIO\b"),
    re.compile(r"\bchief information officer\b", re.IGNORECASE),
    re.compile(r"\bCTO\b"),
    re.compile(r"\bchief technology officer\b", re.IGNORECASE),
]
_CSO = re.compile(r"\bCSO\b")
_CSO_CONTEXT = re.compile(
    r"chief security officer|information security|cyber\s?security",
    re.IGNORECASE)

_VERBS = r"appoints|names|hires|taps|promotes|elevates|welcomes"
_COMPANY_VERB = re.compile(
    rf"^(?P<company>.{{2,80}}?)\s+(?:{_VERBS})\b(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL)
_JOINS = re.compile(r"\bjoins\s+(?P<company>.{2,80}?)\s+as\b(?P<rest>.*)$",
                    re.IGNORECASE | re.DOTALL)
_AS = re.compile(r"\bas\b", re.IGNORECASE)

_TAG = re.compile(r"<[^>]+>")


def _clean_text(s):
    """Strip HTML tags, unescape entities, collapse whitespace."""
    return " ".join(html.unescape(_TAG.sub(" ", s or "")).split())


def _match_exec_title(role_text, context_text):
    """Return (matched title term, confidence) or None. ``context_text`` is
    only used to qualify a bare CSO match (security context required)."""
    for rx in _SECURITY_TITLES:
        m = rx.search(role_text)
        if m:
            return m.group(0), CONFIDENCE_SECURITY
    m = _CSO.search(role_text)
    if m and _CSO_CONTEXT.search(context_text or ""):
        return m.group(0), CONFIDENCE_SECURITY
    for rx in _IT_EXEC_TITLES:
        m = rx.search(role_text)
        if m:
            return m.group(0), CONFIDENCE_IT_EXEC
    return None


def _plausible_company(text):
    """Conservative company-hint filter; '' means emit nothing."""
    hint = text.strip().strip(",;:.-–—").strip()
    if not 2 <= len(hint) <= 80:
        return ""
    if len(hint.split()) > 10:
        return ""
    if not re.search(r"[A-Za-z]", hint):
        return ""
    return hint


def classify_presswire(conn, raw):
    """Press-release appointment headlines -> name-resolved candidates."""
    payload = json.loads(raw["payload"] or "{}")
    title = _clean_text(payload.get("title"))
    desc = _clean_text(payload.get("description"))[:DESC_WINDOW]
    if not title:
        return []

    m = _COMPANY_VERB.match(title)
    if m:
        rest = m.group("rest")
        m_as = _AS.search(rest)          # role region: after the first "as"
        role_text = rest[m_as.end():] if m_as else rest
    else:
        m = _JOINS.search(title)
        if not m:
            return []
        role_text = m.group("rest")      # already after "as"
    company = _plausible_company(m.group("company"))
    if not company:
        return []

    hit = _match_exec_title(role_text, f"{title} {desc}")
    if hit is None:
        return []
    term, confidence = hit

    evidence = [{"text": title, "locator": "title"}]
    # The description only participates as CSO security context; cite it
    # when the match depended on it (R4.1: evidence shows what fired).
    if term == "CSO" and not _CSO_CONTEXT.search(title) and desc:
        evidence.append({"text": desc[:300], "locator": "description"})

    return [{
        "trigger_id": TRIGGER_ID,
        "signal_scope": "account",
        "entity_id": None,
        "entity_name_hint": company,
        "event_date": raw["event_date"] or "",
        "headline": title,
        "evidence": evidence,
        "confidence": confidence,
    }]


def classify_edgar(conn, raw):
    """8-K Item 5.02 with a security-title token -> pre-attributed candidate.

    See the module docstring for the accepted recall limitation: submissions
    payloads carry no filing body text, so this rarely fires."""
    payload = json.loads(raw["payload"] or "{}")
    if payload.get("form") not in EDGAR_FORMS:
        return []
    if EDGAR_ITEM not in (payload.get("items") or []):
        return []
    entity_id = (payload.get("entity_id") or "").strip()
    if not entity_id:
        return []

    full_text = " ".join(
        str(payload.get(f) or "") for f in EDGAR_TEXT_FIELDS)
    hit = _match_exec_title(full_text, full_text)
    if hit is None:
        return []
    term, confidence = hit

    evidence = []
    for field in EDGAR_TEXT_FIELDS:
        value = str(payload.get(field) or "")
        if value and _match_exec_title(value, full_text):
            evidence.append({"text": value, "locator": field})
    if not evidence:
        return []

    return [{
        "trigger_id": TRIGGER_ID,
        "signal_scope": "account",
        "entity_id": entity_id,
        "entity_name_hint": None,
        "event_date": (payload.get("reportDate")
                       or payload.get("filingDate")
                       or raw["event_date"] or ""),
        "headline": f"8-K Item 5.02 leadership change ({term})",
        "evidence": evidence,
        "confidence": confidence,
    }]


SOURCES = {
    "sec_edgar_submissions": classify_edgar,
    "presswire_prnewswire": classify_presswire,
    "presswire_globenewswire": classify_presswire,
}


if __name__ == "__main__":
    sys.exit(classify_runner.cli(
        CLASSIFIER_ID, SOURCES, PARSER_VERSION,
        "Classify leadership-change signals from press wires and EDGAR 8-Ks"))
