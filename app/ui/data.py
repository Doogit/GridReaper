"""Read-only data-access layer for the GridSignals UI (R8.1-R8.3).

Plain stdlib functions the FastAPI routes share, so the pages stay thin and
every query is covered by a hermetic test. The app is a reader; its writes are
each an explicitly sanctioned, transactional seam: ``record_feedback`` (R9.1),
``triage_decision`` (R8.2 human match decisions + review-queue disposition),
the config-write helpers over seeded config tables (R8.7, single-writer lock +
``config_audit``), and ``retier_incident`` — the R8.7 operator re-tier, the one
write that mutates a ``signals`` row's state (single-writer lock +
``incident_tier_edits``). The read seam never calls the write seam. Cards read
``license_play_snapshots``, never live ``license_facts`` (R7.6); fact rows are
surfaced only as provenance chips and this layer never returns a fact's
``price_note`` so a non-primary price can never reach the DOM (R4.3/R7.11).

Timestamps are UTC ISO-8601 (R10.2); functions that reason about age accept an
injectable ``now`` so pages and tests are deterministic.
"""
import contextlib
import json
import math
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from hashlib import sha256
from urllib.parse import urlsplit

from app import aggregates
from app.audit import precision
from app.classify import ransomware as ransomware_classifier
from app.classify import regulatory as regulatory_classifier
from app.classify.runner import INCIDENT_TIERS, customer_facing_for_tier
from app.db.connection import get_connection
from app.licensing import EDITABLE_FACT_COLS
from app.scoring import rescore

# R10.9: the UI reads the backend through THIS module and no other, so
# INCIDENT_TIERS (imported above and used by retier_incident) is also the
# view layer's supported way to reach the tier vocabulary — app/ui_web/ reads
# ``data.INCIDENT_TIERS``, never ``app.classify.runner``. Import-neutral: this
# module already depended on it.

# Signal-scope groupings for the scope-separated feed (R7.2): account cards are
# rendered first, then a labeled divider, then sector/regulatory cards.
SCOPE_GROUPS = {
    "account": ("account", "parent"),
    "sector": ("sector", "subsector", "regulatory_calendar"),
}

# Feed orderings (R8.1). 'recent' is the original keyset-paged chronological
# feed - it answers "what happened". 'score' answers the operator's actual
# question, "what deserves action": a date-ordered feed can leave the
# highest-scoring card anywhere in the list, and once retracted/decayed cards
# are included it is buried pages down. A score page is capped, not paged: the
# keyset is chronological by construction, so a score ordering takes no
# after_key (see feed_page).
FEED_ORDERS = ("score", "recent")

# Feedback verdicts (R9.1) and reason codes (R9.2, verbatim). A not_useful
# verdict MUST carry a reason_code.
VERDICTS = ("useful", "not_useful", "converted")
REASON_CODES = (
    "wrong_entity", "duplicate", "stale_event", "weak_trigger", "weak_evidence",
    "bad_product_mapping", "bad_license_play", "not_my_account",
    "already_known", "other",
)

TRIAGE_PARSER_VERSION = "ui-triage/1.0"

# Columns every card needs, identical shape from feed_page and account_signals
# so the one card component (B1) renders both. Score components are the 0005
# nullable columns (NULL until app.scoring.rescore runs under this build).
_SIGNAL_COLUMNS = """
  s.signal_id, s.raw_event_id, s.entity_id, s.signal_scope, s.trigger_id,
  s.event_date, s.headline, s.evidence_snippet, s.source_url, s.confidence,
  s.evidence_quality, s.incident_evidence_level, s.customer_facing_allowed,
  s.score, s.status,
  s.score_base, s.score_decay, s.score_account_fit, s.score_scope_fit,
  s.scored_at, s.scoring_config_version,
  t.name AS trigger_name, t.base_strength, t.decay_half_life_days,
  e.name AS entity_name, e.subsector, e.richness, e.coverage_flag,
  e.gov_cloud_likelihood, e.tenant_cloud_environment,
  re.source_id, sp.name AS source_name
"""
# The source join is what makes two templated peer cards tellable apart (R4.1):
# both security_rss peer paths mint one FIXED headline, so a feed of peer cards
# reads as N identical rows until the meta line names which outlet reported it.
# Both joins are 1:1 on a primary key (raw_events.raw_event_id,
# source_policies.source_id) and LEFT, so no row count changes.
_SIGNAL_FROM = (
    " FROM signals s"
    " JOIN triggers t ON t.trigger_id = s.trigger_id"
    " LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id"
    " LEFT JOIN raw_events re ON re.raw_event_id = s.raw_event_id"
    " LEFT JOIN source_policies sp ON sp.source_id = re.source_id ")


# -- small helpers -----------------------------------------------------------

def _utcnow_iso(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_dt(value):
    """Parse a UTC ISO-8601 timestamp; None on empty/garbage."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value):
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


# -- sector cards must not link a per-victim leak-site URL --------------------
#
# ⚠️ THE RATIONALE FOR THIS GATE CHANGED. It is no longer an identity rule.
# Peer cards now NAME the victim (operator ruling: naming is acceptable wherever
# the card cites its source), so "the card would otherwise leak an identity" is
# no longer why this exists and must not be re-derived from the old comment.
#
# What survives is a DESTINATION rule: do not send a reader to an extortionist.
# ransomware.live's permalink path is literally base64("<Victim>@<crew>"), so
# https://www.ransomware.live/id/Q29tbXVuaXR5IENvbm5lY3Rpb25zQHRoZWdlbnRsZW1lbg==
# decodes to "Community Connections@thegentlemen" — a criminal leak page
# republishing the victim's stolen data. Naming a victim in our own prose, with
# a citation, is not the same act as driving traffic to the crew extorting them.
# This is the one distinction the PRD itself draws (R10.5 separates leak-site
# evidence from press evidence); legitimate press permalinks are unrestricted.
#
# Sector cards therefore link the tracker's index instead of the per-victim
# permalink: attribution survives (R10.4) and the destination stays lawful.
# ACCOUNT cards are untouched — an own_incident card's permalink is reached only
# for a watchlist entity the operator is already working.
# The rule is a HOST rule, not a substring one. Matching "ransomware.live/id/"
# anywhere in the lowercased string got the two shapes ingest emits right by
# luck and everything else wrong: it missed any tracker URL where the substring
# is broken up or absent (a port — ransomware.live:8443/id/… — or a future
# non-/id/ victim path), missed any OTHER leak-site host entirely, and fired on
# a URL that merely mentions the tracker in a query string, destroying an
# innocent citation. So: parse the URL, normalise the host, compare it against
# a named set. Exact host match plus a stripped "www." only — a suffix test
# would be the same class of bug ("ransomware.live.evil.com").
_LEAK_TRACKER_HOSTS = frozenset({"ransomware.live"})


def _leak_tracker_host(url):
    """True when ``url``'s host is a known victim-naming leak tracker.

    ``urlsplit().hostname`` already lowercases the host and drops the port and
    any userinfo; a leading ``www.`` is stripped here. A string with no scheme
    has no host and so never matches — such a value is not a clickable URL
    either (``render.safe_source_url`` drops non-http(s) values).
    """
    host = urlsplit(url).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host in _LEAK_TRACKER_HOSTS


def identity_safe_source_url(url, signal_scope=None):
    """Redirect a per-victim leak-site permalink to the tracker index.

    Returns ``url`` unchanged unless it points at a leak tracker
    (``_LEAK_TRACKER_HOSTS``) on a non-account-scoped signal, in which case the
    tracker's index is returned in its place. Pure — no I/O — so the view layer
    can call it. This is the single chokepoint for the leak-destination rule: it
    is the only gate that is told the signal's scope, so it is the only one that
    can tell an own-incident card (may keep its permalink) from a sector card
    (may not). Press permalinks are never touched at any scope.
    """
    value = (url or "").strip()
    if not value or signal_scope in ("account", "parent"):
        return value
    if _leak_tracker_host(value):
        return RANSOMWARE_SOURCE_URL
    return value


# -- reproducing a card must not name the victim (R3.7 + R4.1) ---------------
#
# A raw_event_id is "{source_id}:{native_id}" (ingest/runner.py) and an RSS
# classifier's native_id is the feed item's guid - which is the article link, so
# on this corpus a raw_event_id literally reads
# "bleepingcomputer:https://.../trezor-discloses-data-breach-affecting-.../".
# The R3.7 provenance line renders on the feed, Account 360 and /card/ alike, so
# printing the stored value would undo the name-free peer card the classifier
# went to trouble to build.
#
# Account/parent cards already name their entity, so their id discloses nothing
# new and is shown verbatim - reproducing an account card needs the real key.
# Every other scope gets a sha256 digest: two cards from one raw event still
# match, the operator can still grep the store for the digest, and the identity
# stays out of the DOM. Same discipline as render.card_key / review_row_dom_id.
RAW_EVENT_REF_LEN = 16


def identity_safe_raw_event_ref(raw_event_id, signal_scope=None):
    """Name-free reproducibility reference for a signal's raw event (R3.7/R4.1).

    Returns ``{"ref": str, "hashed": bool}``. Account/parent scopes pass the
    raw_event_id through; every other scope gets a sha256 digest of it. Pure -
    no I/O - so the view layer can call it, and the gate lives here rather than
    in a template so no markup change can reopen it.
    """
    value = (raw_event_id or "").strip()
    if not value:
        return {"ref": "", "hashed": False}
    if signal_scope in ("account", "parent"):
        return {"ref": value, "hashed": False}
    digest = sha256(value.encode("utf-8")).hexdigest()[:RAW_EVENT_REF_LEN]
    return {"ref": digest, "hashed": True}


def _placeholders(n):
    return ",".join("?" * n)


# -- feed --------------------------------------------------------------------

def feed_page(conn, scope_group="account", after_key=None, limit=25,
              statuses=("active",), order="recent"):
    """One page of the signal feed for a scope group.

    ``order='recent'`` (the default) is the keyset-paged chronological feed:
    ordering is ``(event_date, signal_id)`` descending (R8.1) and ``after_key``
    is the ``(event_date, signal_id)`` of the last row on the previous page
    (None for the first page). ``order='score'`` ranks by score instead -
    highest first, unscored rows last, newest as the tie-break - which is the
    only ordering that surfaces what deserves action; it is a capped top-N view
    and takes no ``after_key``, because the keyset is chronological.

    ``statuses`` filters status (feed default: active only; decayed/superseded/
    dismissed/retracted reachable by widening it). Returns up to ``limit`` rows;
    a 'recent' caller uses the last row's key as the next after_key.
    """
    if scope_group not in SCOPE_GROUPS:
        raise ValueError(f"unknown scope_group {scope_group!r}")
    if order not in FEED_ORDERS:
        raise ValueError(f"unknown feed order {order!r}")
    scopes = SCOPE_GROUPS[scope_group]
    statuses = tuple(statuses)
    where = [f"s.signal_scope IN ({_placeholders(len(scopes))})",
             f"s.status IN ({_placeholders(len(statuses))})"]
    params = list(scopes) + list(statuses)
    if after_key is not None:
        if order != "recent":
            raise ValueError(
                f"keyset paging is chronological; order={order!r} takes no "
                "after_key")
        ev, sid = after_key
        where.append("(s.event_date < ? OR (s.event_date = ? AND s.signal_id < ?))")
        params += [ev, ev, sid]
    order_sql = (" ORDER BY s.event_date DESC, s.signal_id DESC LIMIT ?"
                 if order == "recent" else
                 " ORDER BY s.score IS NULL, s.score DESC, s.event_date DESC,"
                 " s.signal_id DESC LIMIT ?")
    sql = (f"SELECT {_SIGNAL_COLUMNS} {_SIGNAL_FROM} WHERE " + " AND ".join(where)
           + order_sql)
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def all_signal_ids(conn):
    """Every signal_id, any status (the per-card permalink resolves a hashed
    card_key back to its signal by scanning these — signal_ids are one-way
    hashed for the URL, so the reverse map is a scan, not an index)."""
    return [r["signal_id"]
            for r in conn.execute("SELECT signal_id FROM signals").fetchall()]


def _facts_for(conn, fact_ids_json):
    """License facts for one play's stored ``fact_ids`` JSON array (R4.3/R7.11).

    Parses the column (empty/unparseable -> no facts) and, when non-empty,
    reads fact_id/product_id/segment/source_quality/source_url from
    license_facts — never price_note, so a non-primary price can never reach
    the DOM. Shared by ``signal_detail`` (card fact provenance) and
    ``account_license_plays`` (Products tab badge) so the no-price_note
    guarantee is enforced in exactly one place instead of two.
    """
    try:
        fact_ids = json.loads(fact_ids_json or "[]")
    except ValueError:
        fact_ids = []
    if not fact_ids:
        return []
    return conn.execute(
        "SELECT fact_id, product_id, segment, source_quality, source_url "
        f"FROM license_facts WHERE fact_id IN ({_placeholders(len(fact_ids))}) "
        "ORDER BY fact_id", fact_ids).fetchall()


def signal_detail(conn, signal_id):
    """Full detail for one signal: the card row, its evidence rows, and its
    license-play snapshots with per-play fact provenance. Returns None if the
    signal is unknown. Snapshot text is read verbatim from
    license_play_snapshots (R7.6); facts carry no price_note by construction.
    """
    signal = conn.execute(
        f"SELECT {_SIGNAL_COLUMNS} {_SIGNAL_FROM} WHERE s.signal_id = ?",
        (signal_id,)).fetchone()
    if signal is None:
        return None
    evidence = conn.execute(
        "SELECT raw_event_id, evidence_text, evidence_locator, evidence_rank, "
        " extraction_version FROM signal_evidence WHERE signal_id = ? "
        "ORDER BY evidence_rank, rowid", (signal_id,)).fetchall()
    snap_rows = conn.execute(
        "SELECT sn.play_id, sn.fact_ids, sn.generated_at, sn.generation_version, "
        " sn.display_text, sn.outreach_safe_text, c.product_id, "
        " c.recommended_path, c.discovery_question, p.name AS product_name "
        "FROM license_play_snapshots sn "
        "JOIN license_play_candidates c ON c.play_id = sn.play_id "
        "LEFT JOIN products p ON p.product_id = c.product_id "
        "WHERE sn.signal_id = ? ORDER BY sn.play_id", (signal_id,)).fetchall()
    snapshots = []
    for sn in snap_rows:
        facts = _facts_for(conn, sn["fact_ids"])
        snapshots.append({
            "play_id": sn["play_id"],
            "product_id": sn["product_id"],
            "product_name": sn["product_name"] or sn["product_id"],
            "recommended_path": sn["recommended_path"],
            "discovery_question": sn["discovery_question"],
            "display_text": sn["display_text"],
            "outreach_safe_text": sn["outreach_safe_text"],
            "generated_at": sn["generated_at"],
            "generation_version": sn["generation_version"],
            "facts": facts,
        })
    return {"signal": signal, "evidence": evidence, "snapshots": snapshots,
            "provenance": signal_provenance(conn, signal, evidence)}


def signal_provenance(conn, signal, evidence_rows):
    """The two parser versions behind one card (R3.7), as a dict.

    ``classifier_parser_version`` is the load-bearing one for "reproduce this
    card": the version of the rule that read the raw event. classified_events is
    keyed (raw_event_id, classifier_id, parser_version), and signals carry no
    classifier column, so the classifier is recovered from the signal's own
    ``signal_evidence.extraction_version`` - which the runner writes as the
    classifier's parser_version, "{classifier_id}/{n}". Matching on the
    classifier id as well as the raw event matters because one source can feed
    several classifiers (sec_edgar_submissions feeds both `incident` and
    `leadership`); joining on raw_event_id alone would report a co-tenant's
    version. Where several versions have processed the event, the newest wins;
    ``extraction_version`` (the version that actually minted this signal) is
    returned beside it, so a drift between the two is visible rather than hidden.

    ``fetch_parser_version`` is the FETCHER's version, from the source_run that
    pulled the event. It is a second line, never a substitute: it says how the
    bytes were retrieved, not how they were interpreted.

    Any of them may be None - a signal inserted by a fixture or an older build
    has no bookkeeping row, and the card renders that honestly.
    """
    raw_event_id = signal["raw_event_id"]
    minted = sorted({(r["extraction_version"] or "").strip()
                     for r in evidence_rows} - {""})
    classifier_ids = sorted({v.split("/", 1)[0] for v in minted if "/" in v})
    out = {"classifier_id": classifier_ids[0] if classifier_ids else None,
           "classifier_parser_version": None,
           "extraction_version": minted[0] if minted else None,
           "fetch_parser_version": None}
    if not raw_event_id:
        return out
    if classifier_ids:
        row = conn.execute(
            "SELECT classifier_id, parser_version FROM classified_events "
            f"WHERE raw_event_id = ? AND classifier_id IN "
            f"({_placeholders(len(classifier_ids))}) "
            "ORDER BY processed_at DESC, parser_version DESC LIMIT 1",
            [raw_event_id] + classifier_ids).fetchone()
        if row is not None:
            out["classifier_id"] = row["classifier_id"]
            out["classifier_parser_version"] = row["parser_version"]
    row = conn.execute(
        "SELECT sr.parser_version FROM raw_events re "
        "JOIN source_runs sr ON sr.run_id = re.run_id "
        "WHERE re.raw_event_id = ?", (raw_event_id,)).fetchone()
    if row is not None:
        out["fetch_parser_version"] = row["parser_version"]
    return out


def account_signals(conn, entity_id, statuses=("active",)):
    """All signals attributed to one entity, newest first (Account 360 tabs).
    Same row shape as feed_page so the card component is reused."""
    statuses = tuple(statuses)
    sql = (f"SELECT {_SIGNAL_COLUMNS} {_SIGNAL_FROM} "
           f"WHERE s.entity_id = ? AND s.status IN ({_placeholders(len(statuses))}) "
           "ORDER BY s.event_date DESC, s.signal_id DESC")
    return conn.execute(sql, [entity_id] + list(statuses)).fetchall()


# -- review queue / triage ---------------------------------------------------

def review_pending(conn):
    """Pending review-queue candidates (R8.2) joined to the candidate entity
    and the underlying raw event. review_queue stores the resolver ``reason``
    (its method, e.g. collision_term / fuzzy_below_threshold) and a confidence
    - matched/rejected term trails are not persisted for review candidates, so
    the page shows the reason, not a fabricated term list. ``snippet`` is the
    raw event's title/headline when the payload is JSON, else a truncation."""
    rows = conn.execute(
        "SELECT rq.rowid AS rowid, rq.raw_event_id, rq.candidate_entity_id, "
        " rq.reason, rq.confidence, rq.created_at, "
        " e.name AS candidate_name, e.subsector, "
        " re.url AS source_url, re.payload AS payload, re.event_date "
        "FROM review_queue rq "
        "LEFT JOIN watchlist_entities e ON e.entity_id = rq.candidate_entity_id "
        "LEFT JOIN raw_events re ON re.raw_event_id = rq.raw_event_id "
        "WHERE rq.disposition = 'pending' "
        "ORDER BY rq.created_at DESC, rq.rowid DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        snippet, truncated, omitted = _payload_snippet_parts(r["payload"])
        d["snippet"] = snippet
        d["snippet_truncated"] = truncated
        d["snippet_omitted_fields"] = omitted
        d["snippet_limit"] = _PAYLOAD_SNIPPET_LIMIT
        d.pop("payload", None)
        out.append(d)
    return out


_PAYLOAD_SNIPPET_LIMIT = 200


def _payload_snippet(payload, limit=_PAYLOAD_SNIPPET_LIMIT):
    return _payload_snippet_parts(payload, limit)[0]


def _payload_snippet_parts(payload, limit=_PAYLOAD_SNIPPET_LIMIT):
    """``(snippet, truncated, omitted_fields)`` for one raw payload (R8.2, R10.5).

    The review queue shows a fragment of a raw record, and there are TWO ways
    the fragment is smaller than the record: the character cut (``truncated``)
    and the field selection — a title lifted out of a JSON payload that also
    carried a body, an activity tag and a group name. ``omitted_fields`` counts
    the top-level keys not shown, so the caller can disclose both. Without it a
    short title extracted from a 1 KB record renders as an apparently complete
    line with no cut to label. Pure.
    """
    if not payload:
        return "", False, 0
    try:
        obj = json.loads(payload)
        for key in ("title", "headline", "name", "summary"):
            if isinstance(obj, dict) and (obj.get(key) or "").strip():
                text = obj[key].strip()
                return text[:limit], len(text) > limit, max(len(obj) - 1, 0)
    except (ValueError, TypeError):
        pass
    text = str(payload)
    return text[:limit], len(text) > limit, 0


# Two signals are duplicate CANDIDATES, never duplicates. Nothing here merges,
# dismisses, supersedes or scores anything: the operator judges, this layer only
# proposes (R8.2). Both lineages are deliberately coarse and stated on the row so
# a proposal can be rejected on its face.
DUPLICATE_NEAR_DAYS = 3
# The two lineages are NOT equally strong, and a render that shows them
# identically trains the operator to ignore the section: a shared content_hash
# is the same underlying record, while same-account-same-trigger-within-N-days
# routinely pairs two genuinely distinct events (a VP departing and their
# successor being named). The label says which one is talking, and
# ``_DUP_BASIS_RANK`` floats the strong lineage to the top.
_DUP_BASIS_LABELS = {
    "content_hash": ("identical raw content_hash on two raw events — the same "
                     "underlying record"),
    "entity_trigger_date": ("same account and trigger type, {gap} day(s) apart "
                            "— may be two distinct events"),
}
_DUP_BASIS_RANK = {"content_hash": 0, "entity_trigger_date": 1}


def duplicate_candidates(conn, near_days=DUPLICATE_NEAR_DAYS,
                         statuses=("active",)):
    """Pairs of signals an operator may want to judge as duplicates (R8.2).

    Two lineages, unioned and reported per pair with the basis that produced it:

    * **entity + trigger + near date** — same non-NULL ``entity_id`` and
      ``trigger_id`` within ``near_days``. A pair whose dates do not parse is
      NOT proposed: nearness that cannot be computed is not asserted.
    * **content_hash** — two DISTINCT ``raw_events`` carrying the same non-empty
      ``content_hash`` (R10.4's dedupe key), each with a signal. Same raw event
      is excluded: two triggers off one record are not a duplicate pair.

    The lineages are not equally strong, so ordering puts every content_hash
    pair above every date-proximity pair, newest-first within each. Each pair
    appears once, with ``(signal_a, signal_b)`` in stable ``signal_id`` order,
    carrying every basis that produced it. Read-only.
    """
    statuses = tuple(statuses)
    ph = _placeholders(len(statuses))
    cols = (
        " s1.signal_id AS signal_a, s2.signal_id AS signal_b, "
        " s1.headline AS headline_a, s2.headline AS headline_b, "
        " s1.event_date AS event_date_a, s2.event_date AS event_date_b, "
        " s1.signal_scope AS scope_a, s2.signal_scope AS scope_b, "
        " s1.score AS score_a, s2.score AS score_b, "
        " s1.entity_id AS entity_id, e.name AS entity_name, "
        " s1.trigger_id AS trigger_id, t.name AS trigger_name ")
    joins = (" LEFT JOIN watchlist_entities e ON e.entity_id = s1.entity_id "
             " LEFT JOIN triggers t ON t.trigger_id = s1.trigger_id ")

    by_pair = {}

    def _collect(rows, basis):
        for r in rows:
            row = dict(r)
            d = by_pair.setdefault((row["signal_a"], row["signal_b"]), {})
            # a pair found by both lineages keeps the columns each one carries
            # (only the content_hash query selects content_hash)
            for key, value in row.items():
                if d.get(key) is None:
                    d[key] = value
            d.setdefault("bases", [])
            if basis not in d["bases"]:
                d["bases"].append(basis)

    _collect(conn.execute(
        f"SELECT {cols} FROM signals s1 JOIN signals s2 "
        " ON s2.entity_id = s1.entity_id AND s2.trigger_id = s1.trigger_id "
        " AND s2.signal_id > s1.signal_id "
        f"{joins} "
        "WHERE s1.entity_id IS NOT NULL AND s1.trigger_id IS NOT NULL "
        f" AND s1.status IN ({ph}) AND s2.status IN ({ph}) "
        " AND ABS(JULIANDAY(s2.event_date) - JULIANDAY(s1.event_date)) <= ?",
        list(statuses) + list(statuses) + [near_days]).fetchall(),
        "entity_trigger_date")

    _collect(conn.execute(
        f"SELECT {cols}, r1.content_hash AS content_hash "
        "FROM signals s1 "
        " JOIN raw_events r1 ON r1.raw_event_id = s1.raw_event_id "
        " JOIN raw_events r2 ON r2.content_hash = r1.content_hash "
        "  AND r2.raw_event_id <> r1.raw_event_id "
        " JOIN signals s2 ON s2.raw_event_id = r2.raw_event_id "
        "  AND s2.signal_id > s1.signal_id "
        f"{joins} "
        "WHERE r1.content_hash IS NOT NULL AND r1.content_hash <> '' "
        f" AND s1.status IN ({ph}) AND s2.status IN ({ph})",
        list(statuses) + list(statuses)).fetchall(),
        "content_hash")

    out = list(by_pair.values())
    for d in out:
        d.setdefault("content_hash", None)
        d["day_gap"] = _day_gap(d["event_date_a"], d["event_date_b"])
        d["bases"].sort(key=lambda b: _DUP_BASIS_RANK.get(b, 99))
        # the strongest lineage backing this pair; drives ordering so the
        # same-record proposals are not buried under coincidental proximity
        d["basis_rank"] = min(_DUP_BASIS_RANK.get(b, 99) for b in d["bases"])
    # newest-first, then a stable pass that floats the strong lineage to the top
    out.sort(key=lambda d: (max(d["event_date_a"] or "", d["event_date_b"] or ""),
                            d["signal_a"]), reverse=True)
    out.sort(key=lambda d: d["basis_rank"])
    return out


def _day_gap(date_a, date_b):
    """Whole days between two event dates, or None when either will not parse."""
    a, b = _parse_date(date_a), _parse_date(date_b)
    return None if a is None or b is None else abs((b - a).days)


def duplicate_basis_label(basis, day_gap=None):
    """Human label for one duplicate-candidate basis; the gap is stated so the
    operator can reject a proposal without opening either card."""
    template = _DUP_BASIS_LABELS.get(basis, basis)
    return template.format(gap="?" if day_gap is None else day_gap)


def judge_human_disagreements(conn):
    """Judge-vs-human disagreements as a triage work queue (R8.2, R9.11).

    Reuses the Precision page's computation verbatim
    (``precision.judge_human_disagreement``) rather than restating it, so the
    queue and the metric can never diverge. A signal is comparable only when it
    has BOTH a judge verdict (entity_match / evidence_support) AND a human
    rating; only the DISAGREEING items are work, but ``comparable`` ships with
    them so an empty queue reads as 'nothing to compare yet', not 'nothing is
    wrong'. Disagreement is a QA signal, not ground truth - the judge rates
    objective facts and the human rates usefulness, so some disagreement is
    expected. Nothing here changes a verdict; the operator judges.
    """
    computed = precision.judge_human_disagreement(
        precision_audit_rows(conn), precision_feedback_rows(conn))
    items = [i for i in computed["items"] if i["judge"] != i["human"]]
    detail = {}
    if items:
        ids = [i["signal_id"] for i in items]
        rows = conn.execute(
            "SELECT s.signal_id, s.headline, s.event_date, s.signal_scope, "
            " s.status, s.entity_id, e.name AS entity_name, "
            " t.name AS trigger_name, re.source_id AS source_id "
            "FROM signals s "
            "LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id "
            "LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
            "LEFT JOIN raw_events re ON re.raw_event_id = s.raw_event_id "
            f"WHERE s.signal_id IN ({_placeholders(len(ids))})", ids).fetchall()
        detail = {r["signal_id"]: dict(r) for r in rows}
    out = []
    for item in items:
        d = dict(detail.get(item["signal_id"], {}))
        d.update(item)
        out.append(d)
    out.sort(key=lambda d: (d.get("event_date") or "", d["signal_id"]),
             reverse=True)
    return {
        "comparable": computed["comparable"],
        "agree": computed["agree"],
        "disagree": computed["disagree"],
        "items": out,
    }


def source_health(conn):
    """Every source policy with its most recent run and most recent successful
    run (R8.2/R10.3 operator surface). Callers pass rows to ``source_state`` to
    label them error / never-run / stale / disabled / ok."""
    return conn.execute(
        "SELECT sp.source_id, sp.name, sp.enabled, sp.ttl, sp.access_method, "
        " sp.evidence_rank, sp.origin, "
        " (SELECT r.status FROM source_runs r WHERE r.source_id = sp.source_id "
        "   ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) AS last_status, "
        " (SELECT r.error_state FROM source_runs r WHERE r.source_id = sp.source_id "
        "   ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) AS last_error_state, "
        " (SELECT r.finished_at FROM source_runs r WHERE r.source_id = sp.source_id "
        "   ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) AS last_finished_at, "
        " (SELECT r.started_at FROM source_runs r WHERE r.source_id = sp.source_id "
        "   ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) AS last_started_at, "
        " (SELECT r.finished_at FROM source_runs r WHERE r.source_id = sp.source_id "
        "   AND r.status = 'success' ORDER BY r.started_at DESC, r.run_id DESC "
        "   LIMIT 1) AS last_success_at "
        "FROM source_policies sp ORDER BY sp.enabled DESC, sp.source_id").fetchall()


def source_state(row, now=None):
    """Classify a source_health row: disabled | error | never_run | stale | ok.
    'stale' means the last success is older than the policy ttl (seconds)."""
    if not row["enabled"]:
        return "disabled"
    if row["last_status"] is None:
        return "never_run"
    if row["last_status"] != "success":
        return "error"
    now = now or datetime.now(timezone.utc)
    ttl = row["ttl"]
    success = _parse_dt(row["last_success_at"])
    if ttl and success is not None:
        age = (now - success).total_seconds()
        if age > ttl:
            return "stale"
    return "ok"


def stale_facts(conn, days=180, now=None):
    """License facts whose verified_date is older than ``days`` (R10.7, default
    180). Facts with an empty/unparseable verified_date are included as stale
    (unknown verification cannot be proven fresh); their age_days is None.
    Returns rows ordered oldest-first, each with product_name and age_days."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    rows = conn.execute(
        "SELECT lf.fact_id, lf.product_id, lf.sku_or_plan, lf.segment, "
        " lf.source_quality, lf.source_url, lf.verified_date, "
        " p.name AS product_name "
        "FROM license_facts lf "
        "LEFT JOIN products p ON p.product_id = lf.product_id").fetchall()
    stale = []
    for r in rows:
        vd = _parse_date(r["verified_date"])
        if vd is None:
            age = None
        else:
            age = (today - vd).days
            if age <= days:
                continue
        d = dict(r)
        d["age_days"] = age
        stale.append(d)
    # oldest first; unknown-verified (age None) sort last
    stale.sort(key=lambda d: (d["age_days"] is None, -(d["age_days"] or 0)))
    return stale


# -- feedback / precision reads (R8.6, R9.3) ---------------------------------
#
# These feed app.audit.precision, whose PURE functions read rows with row.get()
# — so every helper here returns plain dicts (sqlite3.Row has no .get()). The
# dimension keys precision slices by (trigger_id, source_id, signal_scope,
# incident_evidence_level, entity_id) are resolved once, here, by the joins:
#   * source_id   comes from the signal's raw_event  (signals.raw_event_id ->
#                 raw_events.source_id); NULL for sector/regulatory signals with
#                 no raw_event.
#   * trigger_name comes from triggers.name (signals.trigger_id -> triggers).
# entity_id is carried through verbatim (None for sector-scope signals) so
# precision's account-vs-sector partition (R9.4) works off the real value.

def precision_feedback_rows(conn):
    """Feedback rows joined to their signal's dimensions (R8.6, R9.3).

    One dict per feedback row, carrying the keys precision.py consumes:
    ``signal_id, verdict, reason_code, ts, trigger_id, trigger_name, source_id,
    signal_scope, incident_evidence_level, entity_id``. source_id resolves via
    ``signals.raw_event_id -> raw_events.source_id`` (LEFT JOIN: None when the
    signal has no raw event); trigger_name via ``triggers.name``.
    """
    rows = conn.execute(
        "SELECT f.signal_id, f.verdict, f.reason_code, f.ts, "
        " s.trigger_id, t.name AS trigger_name, re.source_id AS source_id, "
        " s.signal_scope, s.incident_evidence_level, s.entity_id "
        "FROM feedback f "
        "JOIN signals s ON s.signal_id = f.signal_id "
        "LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
        "LEFT JOIN raw_events re ON re.raw_event_id = s.raw_event_id "
        "ORDER BY f.ts, f.rowid").fetchall()
    return [dict(r) for r in rows]


def precision_audit_rows(conn):
    """Audit verdicts joined to their signal's dimensions (R8.6, R9.3, R9.4).

    One dict per audit verdict, carrying: ``signal_id, check_type, result,
    model_id, prompt_version, ts, trigger_id, trigger_name, source_id,
    signal_scope, incident_evidence_level, entity_id``. Same joins as
    ``precision_feedback_rows`` resolve source_id / trigger_name / entity_id.
    """
    rows = conn.execute(
        "SELECT a.signal_id, a.check_type, a.result, a.model_id, "
        " a.prompt_version, a.ts, s.trigger_id, t.name AS trigger_name, "
        " re.source_id AS source_id, s.signal_scope, s.incident_evidence_level, "
        " s.entity_id "
        "FROM audit a "
        "JOIN signals s ON s.signal_id = a.signal_id "
        "LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
        "LEFT JOIN raw_events re ON re.raw_event_id = s.raw_event_id "
        "ORDER BY a.ts, a.rowid").fetchall()
    return [dict(r) for r in rows]


def precision_halflife_rows(conn):
    """One row per signal for the half-life effectiveness view (R8.6, R9.3).

    Carries ``signal_id, trigger_id, trigger_name, decay_half_life_days, score,
    score_decay, status, event_date, verdict``. ``verdict`` comes from a LEFT
    JOIN to feedback and is None when the signal is unrated. A signal may have
    several feedback rows; we pick the LATEST by ts (ties broken by feedback
    rowid) via a correlated subquery, so a signal contributes exactly one row
    and half_life_effectiveness's rated/useful counts are per-signal, not
    per-feedback-row.
    """
    rows = conn.execute(
        "SELECT s.signal_id, s.trigger_id, t.name AS trigger_name, "
        " t.decay_half_life_days, s.score, s.score_decay, s.status, "
        " s.event_date, "
        " (SELECT f.verdict FROM feedback f WHERE f.signal_id = s.signal_id "
        "   ORDER BY f.ts DESC, f.rowid DESC LIMIT 1) AS verdict "
        "FROM signals s "
        "LEFT JOIN triggers t ON t.trigger_id = s.trigger_id "
        "ORDER BY s.trigger_id, s.signal_id").fetchall()
    return [dict(r) for r in rows]


def audit_run_rows(conn):
    """audit_runs rows newest-first for the run-history panel (R8.6, R9.12).

    Surfaces the R9.12 skip/budget transparency (status, verdicts_written,
    budget_spent, skipped_reason, error_state) so an operator sees why a run
    wrote nothing rather than a silent gap. Returns plain dicts."""
    rows = conn.execute(
        "SELECT run_id, started_at, finished_at, model_id, prompt_version, "
        " parser_version, signals_sampled, verdicts_written, budget_spent, "
        " status, error_state, skipped_reason "
        "FROM audit_runs ORDER BY started_at DESC, run_id DESC").fetchall()
    return [dict(r) for r in rows]


def _reporting_month_rows(rows, now):
    """Subset of ``rows`` (each a dict carrying a ``ts`` key) whose UTC
    calendar month matches the month containing ``now`` (real current time
    when ``now`` is None) — the SAME "month containing now" convention
    ``app.audit.precision.spotcheck_coverage`` already uses (R9.3/R9.5), so a
    report windowed with this helper reports the identical period its
    ``spotcheck_window``/``as_of`` fields already name: for the live page
    (``now=None``) that is the current, still-accumulating month; for the
    scheduled job (``now=prior_month_end(...)``) it is the month that just
    ended. A row whose ``ts`` does not parse is dropped — unevaluable is
    excluded, never defaulted into the window.

    Windowing itself (U31) is shared code, not shared convention: both the
    ``now`` normalization and the per-row month key delegate to
    ``app.audit.precision.resolve_now``/``month_key``, the same helpers
    ``spotcheck_coverage`` uses, so this can no longer independently drift
    from that module's "month containing now" definition.
    """
    now_dt = precision.resolve_now(now)
    month = (now_dt.year, now_dt.month)
    out = []
    for r in rows:
        if precision.month_key(r.get("ts")) == month:
            out.append(r)
    return out


def precision_report(conn, now=None):
    """Every precision COMPUTATION the QA surface needs, in one read (R8.6,
    R9.2-R9.5, R9.11, R9.12, R10.9).

    R10.9 makes this module the only door between the view layer and the
    backend, so the view may not call ``app.audit.precision`` itself: it reads
    the computed dicts from here and only FORMATS them. One call so the four
    row-reads happen once per page, and so the G2 gate overlay (``g2_status``
    plus the R9.11 disagreement gate, KTD3) is applied in exactly one place —
    the same composition ``source_policy_rows`` uses, which is what keeps the
    Precision page and the Admin source table from ever contradicting each other.

    The headline useful=/auto= rates, the per-dimension tables, the reason-code
    distribution, the judge-human disagreement view, and Gate G2 are windowed
    to the reporting month via ``_reporting_month_rows`` (R9.3, R9.5) —
    ``g2_status``'s own docstring already says the caller is expected to hand
    it rows already windowed to the period. Gate G1 (R9.4) and the spot-check
    tracker (R9.11) deliberately keep the FULL, unwindowed rows: G1 measures
    cumulative evidence over a >=30-day span that can outlive one calendar
    month, and ``spotcheck_coverage`` already does its own per-row month
    filtering against ``now`` internally. ``halflife`` and ``runs`` are not
    feedback/audit rates and are unaffected either way.

    Returns the raw computation shapes verbatim (rates as floats or None, each
    beside its n) — no percent strings, no "n/a": the n-carrying trust invariant
    is enforced when these are rendered, and this layer must stay renderable by
    any front end. ``now`` is injectable for deterministic tests (R10.2).
    """
    feedback = precision_feedback_rows(conn)
    audit = precision_audit_rows(conn)
    feedback_window = _reporting_month_rows(feedback, now)
    audit_window = _reporting_month_rows(audit, now)
    g2 = precision.g2_gated(
        precision.g2_status(feedback_window, now=now),
        precision.judge_human_disagreement_by_source(audit_window, feedback_window))
    return {
        "min_rated": precision.G1_MIN_RATED,
        "useful_overall": precision.useful_rate_overall(feedback_window),
        "auto_overall": precision.auto_accuracy(audit_window, "trigger")["overall"],
        "g1": precision.g1_status(feedback, audit, now=now),
        "g2": g2,
        "spotcheck": precision.spotcheck_coverage(audit, feedback, now=now),
        "useful_by_dimension": {d: precision.useful_rate(feedback_window, d)
                                for d in precision.DIMENSIONS},
        "auto_by_dimension": {d: precision.auto_accuracy(audit_window, d)
                              for d in precision.DIMENSIONS},
        "reason_codes": precision.reason_code_distribution(feedback_window),
        "disagreement": precision.judge_human_disagreement(
            audit_window, feedback_window),
        "halflife": precision.half_life_effectiveness(
            precision_halflife_rows(conn)),
        "runs": audit_run_rows(conn),
    }


# -- account 360 -------------------------------------------------------------

def account_header(conn, entity_id):
    """Account 360 header (R8.3): the entity row, its parent and children from
    entity_relationships, and its signal counts by status. Returns None for an
    unknown entity."""
    entity = conn.execute(
        "SELECT * FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if entity is None:
        return None
    parent = conn.execute(
        "SELECT e.entity_id, e.name FROM entity_relationships r "
        "JOIN watchlist_entities e ON e.entity_id = r.parent_entity_id "
        "WHERE r.child_entity_id = ? ORDER BY e.entity_id LIMIT 1",
        (entity_id,)).fetchone()
    if parent is None and (entity["parent_id"] or "").strip():
        parent = conn.execute(
            "SELECT entity_id, name FROM watchlist_entities WHERE entity_id = ?",
            (entity["parent_id"].strip(),)).fetchone()
    children = conn.execute(
        "SELECT DISTINCT e.entity_id, e.name FROM entity_relationships r "
        "JOIN watchlist_entities e ON e.entity_id = r.child_entity_id "
        "WHERE r.parent_entity_id = ? ORDER BY e.name", (entity_id,)).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM signals WHERE entity_id = ? "
        "GROUP BY status", (entity_id,))}
    return {
        "entity": entity,
        "parent": parent,
        "children": children,
        "signal_counts": counts,
        "total_signals": sum(counts.values()),
    }


# -- Account 360 tabs 3-5: Products, Compliance Calendar, Entity Graph (R8.3) --
#
# Three reads over three tables that are populated to very different depths, and
# each read says so rather than papering over it:
#
#   Products    license_play_snapshots keyed by the account's OWN signals. 312
#               snapshots exist, 0 of them on an entity-bearing signal, because
#               no signal in the store has ever carried an entity_id (R7.2). So
#               this returns [] for every account today, by construction, and
#               the tab's copy names that reason. It is deliberately NOT joined
#               through the class-scoped obligation predicate: a play is a
#               per-signal licensing recommendation (R7.6), and attributing a
#               sector signal's play to an account would assert an account fit
#               that was never computed.
#
#   Calendar    regulatory_obligations, joined to the account by
#               applicability_rule (see app/obligations.py). Class-scoped, never
#               account-keyed; the predicate is a regulated-class FILTER, not a
#               registration claim.
#
#   Graph       entity_relationships in both directions. One row exists in the
#               whole store (E0088 -> E0089, direct_parent, from GLEIF), so this
#               is a relationship LIST, not a graph drawing - there is no
#               measured structure to draw.

def account_license_plays(conn, entity_id, statuses=("active",)):
    """Stored license plays for the account's own signals (R8.3, R7.6).

    Reads ``license_play_snapshots`` — the frozen text the card showed — never
    live ``license_facts``, and never a fact's ``price_note`` (R4.3/R7.11).
    Ordered newest signal first, then by play_id, so the tab is stable.

    Each row also carries its play's fact provenance under ``facts``
    (fact_id/product_id/segment/source_quality/source_url — the same
    no-price_note projection ``_facts_for`` builds for ``signal_detail``):
    ``fact_ids`` was never selected here before, so the Products tab could
    not badge a non-primary-sourced play the way the feed card does (R4.3).
    """
    statuses = tuple(statuses)
    rows = conn.execute(
        "SELECT lps.signal_id, lps.play_id, lps.fact_ids, lps.display_text, "
        "       lps.outreach_safe_text, lps.generated_at, "
        "       lps.generation_version, c.product_id, c.discovery_question, "
        "       p.name AS product_name, s.headline, s.event_date "
        "FROM license_play_snapshots lps "
        "JOIN signals s ON s.signal_id = lps.signal_id "
        "LEFT JOIN license_play_candidates c ON c.play_id = lps.play_id "
        "LEFT JOIN products p ON p.product_id = c.product_id "
        f"WHERE s.entity_id = ? AND s.status IN ({_placeholders(len(statuses))}) "
        "ORDER BY s.event_date DESC, lps.signal_id DESC, lps.play_id",
        [entity_id] + list(statuses)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["facts"] = _facts_for(conn, d.pop("fact_ids"))
        out.append(d)
    return out


# The predicate format app/obligations.py writes:
#     subsector_in:a;b;c
#     subsector_in:a;b;c|exclude_entities:E0001;E0002
# Spelled out on both sides rather than imported, so the reader does not drag
# the ingestion lock into the UI import path; tests/test_obligations.py binds
# the two ends so a drift on either side fails a test instead of silently
# emptying the calendar tab.
SUBSECTOR_RULE_PREFIX = "subsector_in:"
EXCLUDE_ENTITIES_RULE_PREFIX = "exclude_entities:"
RULE_CLAUSE_SEPARATOR = "|"

# Every entity_id in seeds/watchlist_entities.csv is the literal form 'E' plus
# four ASCII digits, and the producer names entities by that id and no other
# spelling. So a canonical-form check is what makes the exclusion clause's
# CONTENTS fail closed: an id carrying whitespace or the wrong case parses fine
# and then matches nobody, silently admitting the account it was written to
# exclude. Explicit [0-9] rather than \d, which also matches non-ASCII digits.
_ENTITY_ID_RE = re.compile(r"E[0-9]{4}")


def applicability_scope(applicability_rule):
    """What a stored applicability predicate admits: ``(subsectors, excluded)``.

    ``subsectors`` is the set a ``subsector_in:a;b;c`` clause admits;
    ``excluded`` is the entity_id set an optional
    ``|exclude_entities:E1;E2`` clause removes from it (empty when the rule
    carries no such clause).

    Returns None when the rule is missing or in a form this reader does not
    understand — the caller then EXCLUDES the obligation rather than showing
    it. Fail-closed is the safe direction: the predicate exists to keep a FERC
    CIP deadline off a refiner's page, so an unevaluable rule must not default
    to "applies to everyone".

    The two clauses fail closed in opposite-looking ways for the same reason.
    An empty subsector clause is already closed — it admits nobody — so it
    parses. An empty or unrecognized second clause is NOT: degrading it to "no
    exclusions" would widen the rule back to everyone the subsector labels
    over-admit, so it is treated as unevaluable and the obligation is dropped.

    That applies to the exclusion clause's CONTENTS as well as its structure.
    An excluded id must be in the canonical ``E``+4-digit form; one that is not
    (``"E0155 "``, ``"e0155"``) would parse into a non-empty set that matches no
    real entity_id, silently readmitting the account it names — the same
    widening by a subtler route. The producer emits canonical ids, so any other
    form means the predicate was not written by app/obligations.py and cannot be
    trusted to name every entity it should. Shape is checked, existence is not:
    this reader has no watchlist access, so a well-formed id that is absent from
    the store parses and simply excludes nobody.
    """
    rule = (applicability_rule or "").strip()
    if not rule.startswith(SUBSECTOR_RULE_PREFIX):
        return None
    clauses = rule.split(RULE_CLAUSE_SEPARATOR)
    if len(clauses) > 2:
        return None
    subsectors = {
        s for s in clauses[0][len(SUBSECTOR_RULE_PREFIX):].split(";") if s}
    if len(clauses) == 1:
        return subsectors, set()
    if not clauses[1].startswith(EXCLUDE_ENTITIES_RULE_PREFIX):
        return None
    excluded = {
        e for e in clauses[1][len(EXCLUDE_ENTITIES_RULE_PREFIX):].split(";")
        if e}
    if not excluded:
        return None
    if any(not _ENTITY_ID_RE.fullmatch(e) for e in excluded):
        return None
    return subsectors, excluded


def account_obligations(conn, entity_id, now=None):
    """Regulatory obligations applying to one account's subsector (R8.3, R7.2).

    Returns ``{subsector, obligations, unscoped, excluded_by_entity, total}``:

        subsector    the account's own subsector — the value the predicate was
                     matched on, surfaced so the reader can see WHY these rows
                     are here (and an empty tab can say which class it checked).
                     A rule may still drop this account by entity_id where its
                     subsector label admits more than the rule binds
        obligations  matching rows, soonest effective date first, each carrying
                     ``in_effect`` computed against ``now``
        unscoped     obligations excluded because their applicability_rule could
                     not be evaluated. Disclosed, never silently dropped
        excluded_by_entity
                     obligations that matched this account's subsector but named
                     this account in their exclusion clause. Without it two
                     accounts sharing a subsector label render contradictory
                     counts with nothing disclosing why, and an empty tab claims
                     the class has no obligation when one applies to the class
                     and this account alone was dropped (R4.1)
        total        obligations in the store, so "3 of 3 checked" is verifiable

    ``compliance_date`` is passed through untouched and is NULL on every derived
    row: FERC states compliance dates in the order body, which is not in the
    fetched metadata. This is an EFFECTIVE-DATE calendar; deriving a deadline
    from an effective date would be a fabricated obligation (R4.1).
    """
    entity = conn.execute(
        "SELECT subsector FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if entity is None:
        return {"subsector": "", "obligations": [], "unscoped": 0,
                "excluded_by_entity": 0, "total": 0}
    subsector = (entity["subsector"] or "").strip()
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()

    rows = conn.execute(
        "SELECT obligation_id, source_url, regulator, rule_name, "
        "       affected_scope, applicability_rule, effective_date, "
        "       compliance_date, mapped_products, verified_at, signal_id "
        "FROM regulatory_obligations "
        "ORDER BY effective_date, obligation_id").fetchall()

    obligations, unscoped, excluded_by_entity = [], 0, 0
    for row in rows:
        scope = applicability_scope(row["applicability_rule"])
        if scope is None:
            unscoped += 1
            continue
        admitted, excluded = scope
        if subsector not in admitted:
            continue
        if entity_id in excluded:
            excluded_by_entity += 1
            continue
        effective = _parse_date(row["effective_date"])
        item = dict(row)
        item["in_effect"] = effective is not None and effective <= today
        item["product_names"] = _obligation_product_names(
            conn, row["mapped_products"])
        obligations.append(item)
    return {"subsector": subsector, "obligations": obligations,
            "unscoped": unscoped, "excluded_by_entity": excluded_by_entity,
            "total": len(rows)}


def _obligation_product_names(conn, mapped_products):
    """[(product_id, name)] for a ';'-joined mapped_products string.

    An id with no products row keeps its id as the label — the mapping came
    from cip_product_map and is shown as stored, not silently dropped.
    """
    ids = [p for p in (mapped_products or "").split(";") if p]
    if not ids:
        return []
    names = {r["product_id"]: r["name"] for r in conn.execute(
        f"SELECT product_id, name FROM products "
        f"WHERE product_id IN ({_placeholders(len(ids))})", ids)}
    return [(pid, names.get(pid) or pid) for pid in ids]


def account_relationships(conn, entity_id):
    """Corporate relationships touching one account, both directions (R8.3).

    Each row carries ``direction`` ('parent' — the other entity is this one's
    parent — or 'child'), the related entity, the stored relationship_type, and
    the source/verified_at provenance the header drops. Unlike
    ``account_header``, this does NOT fall back to watchlist_entities.parent_id:
    that column is a seeded hint with no source or verification date, and this
    tab's whole claim is that every edge shown is a sourced, stored edge.
    """
    return conn.execute(
        "SELECT 'parent' AS direction, e.entity_id, e.name, e.subsector, "
        "       r.relationship_type, r.source, r.verified_at "
        "FROM entity_relationships r "
        "JOIN watchlist_entities e ON e.entity_id = r.parent_entity_id "
        "WHERE r.child_entity_id = ? "
        "UNION ALL "
        "SELECT 'child' AS direction, e.entity_id, e.name, e.subsector, "
        "       r.relationship_type, r.source, r.verified_at "
        "FROM entity_relationships r "
        "JOIN watchlist_entities e ON e.entity_id = r.child_entity_id "
        "WHERE r.parent_entity_id = ? "
        "ORDER BY direction, name", (entity_id, entity_id)).fetchall()


def badge_legend(conn):
    """badge_legend table -> {badge_kind: {code: {'label','description'}}}.
    The UI renders badge labels from this, never hardcoding a code's meaning."""
    out = {}
    for r in conn.execute(
            "SELECT badge_kind, code, label, description FROM badge_legend"):
        out.setdefault(r["badge_kind"], {})[r["code"]] = {
            "label": r["label"], "description": r["description"]}
    return out


# -- writes (the only two) ---------------------------------------------------
#
# Bounded exponential backoff (R3.2). These two are the operator writes that do
# NOT take the ingestion lock - they must land while a run is in flight - so
# they are the only UI writes exposed to SQLite's writer lock. The connection's
# passive 5s busy_timeout (connection.py) covers a short overlap; a longer one
# used to surface a raw "database is locked" to the operator. The config /
# re-tier writes are NOT wrapped: they hold the single-writer lock and already
# fail with "Ingestion in progress" instead. This is deliberately not a retry
# wrapper for arbitrary SQL - nothing outside this section calls it.
WRITE_RETRY_DELAYS_S = (0.05, 0.15, 0.45)


class WriteBusyError(RuntimeError):
    """A sanctioned UI write could not get the SQLite writer lock (R3.2)."""


def _is_locked(exc):
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _commit_with_backoff(conn, write, what, sleep=None):
    """Run ``write()`` and commit, retrying a busy database with bounded
    exponential backoff (R3.2). The success path never sleeps.

    Retries only a locked/busy database; any other OperationalError is a real
    SQL error and is re-raised on the first attempt. Each retry rolls back
    first, so a multi-statement write (triage_decision) can never half-apply.
    Once the ``WRITE_RETRY_DELAYS_S`` waits are spent the failure is raised as
    WriteBusyError naming ``what`` and what to do about it, rather than as a
    bare sqlite3 message. ``sleep`` is injectable so tests stay fast.
    """
    sleeper = sleep or time.sleep
    for delay in WRITE_RETRY_DELAYS_S + (None,):
        try:
            write()
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            conn.rollback()
            if delay is None:
                waited = sum(WRITE_RETRY_DELAYS_S)
                raise WriteBusyError(
                    f"could not save {what}: the database stayed busy across "
                    f"{len(WRITE_RETRY_DELAYS_S) + 1} attempts over {waited:.2f}s. "
                    f"An ingestion or scoring run is probably writing - nothing "
                    f"was saved; try again in a moment."
                ) from exc
            sleeper(delay)


def record_feedback(conn, signal_id, verdict, reason_code=None, note="",
                    now=None, sleep=None):
    """Insert a feedback row (R9.1). A not_useful verdict MUST carry a
    reason_code from R9.2; any provided reason_code must be a known code.
    Raises ValueError on invalid input (the page surfaces it, never writes).
    A busy database is retried with bounded backoff (R3.2)."""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    if verdict == "not_useful" and not reason_code:
        raise ValueError("reason_code is required when verdict is not_useful (R9.1)")
    if reason_code and reason_code not in REASON_CODES:
        raise ValueError(f"unknown reason_code {reason_code!r}")

    def write():
        conn.execute(
            "INSERT INTO feedback (signal_id, verdict, reason_code, ts, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (signal_id, verdict, reason_code or None, _utcnow_iso(now),
             note or ""))

    _commit_with_backoff(conn, write, "your feedback", sleep)


def triage_decision(conn, raw_event_id, entity_id, accept, now=None, sleep=None):
    """Record a human review decision (R8.2): log an entity_match_decisions row
    (decided_by='human') and update the matching review_queue row's disposition
    - both in one transaction. Accepting records the decision only; creating a
    signal from an accepted match is deferred (documented on the page).

    Some review rows are not entity candidates: a NULL candidate_entity_id means
    the row was routed to triage for another reason - the R10.6 provenance guard
    quarantines a raw event that way when its text needs operator review. Those
    rows still need to be dismissible, but they must not fabricate an
    entity_match_decisions row for a nonexistent entity.

    A busy database is retried with bounded backoff (R3.2).
    """
    ts = _utcnow_iso(now)
    decision = "reviewed" if accept else "rejected"
    disposition = "accepted" if accept else "rejected"
    entity_id = (entity_id or "").strip() or None

    def write():
        if entity_id is not None:
            conn.execute(
                "INSERT INTO entity_match_decisions (raw_event_id, entity_id, "
                " method, confidence, matched_terms, rejected_terms, decision, "
                " decided_by, ts, parser_version) VALUES (?, ?, 'human_review', "
                " 1.0, '[]', '[]', ?, 'human', ?, ?)",
                (raw_event_id, entity_id, decision, ts, TRIAGE_PARSER_VERSION))
        conn.execute(
            "UPDATE review_queue SET disposition = ?, disposed_at = ? "
            "WHERE raw_event_id = ? AND candidate_entity_id IS ?",
            (disposition, ts, raw_event_id, entity_id))

    _commit_with_backoff(conn, write, "this triage decision", sleep)


# -- config writes (R8.7 Admin/Config) ---------------------------------------
#
# The first UI writes into *seeded* config tables. Three rules hold every
# one (see the plan's KTDs):
#   * Provenance (R3.3): each edit writes a config_audit row (field, old->new,
#     editor, reason, ts) in the SAME transaction as the edit. Append-only:
#     these helpers only UPDATE the target row and INSERT audit - never DELETE,
#     so nothing FK-referencing a config row can be orphaned.
#   * Reload-safe (Pattern B): weight / decay_half_life_days are frozen against
#     seed reload by the loader's update_cols, so a direct UPDATE here survives
#     the next load_seeds run. Reads (scoring.load_weights, rescore) are unchanged.
#   * Single-writer (R3.2): the page wraps each save in config_write_conn(),
#     which takes the ingestion lock and hands back a fresh short-lived
#     connection. A weight/half-life edit re-runs rescore() ACTIVE-ONLY (R8.1):
#     decayed/dismissed/superseded rows keep their frozen score and components.
CONFIG_EDITOR = "operator"          # single-operator MVP: no auth, no PII (R10.6)

# R8.7 watchlist manager: the curation columns an operator may edit on a seeded
# entity. The loader freezes exactly these against reload (load_seeds
# watchlist_entities update_cols excludes them); identifiers/name stay
# seed-refreshable so seed corrections still flow. All are watchlist CSV columns,
# so reset_entity_to_seed can restore every one of them from the seed row.
EDITABLE_ENTITY_COLS = (
    "subsector", "parent_id", "richness", "coverage_flag",
    "gov_cloud_likelihood", "notes", "owning_seller",
)

# Operator-added alias / collision-term rows carry this marker (entity_aliases.
# source / entity_collision_terms.reason) so the loader's seed-scoped DELETE
# (load_seeds WATCHLIST_ALIAS_SOURCE / WATCHLIST_COLLISION_REASON) never wipes
# them - Pattern B for the normalized side tables (R4.4).
OPERATOR_SOURCE = "operator"

# Tables that FK-reference watchlist_entities.entity_id. The FKs are bare (no ON
# DELETE, so NO ACTION): a hard-delete with any referencing row raises
# IntegrityError. remove_operator_entity counts these first and blocks legibly
# instead of crashing or orphaning history (R8.7 delete safety; trap 2).
_ENTITY_REFERENCING = (
    ("signals", "entity_id"),
    ("entity_match_decisions", "entity_id"),
    ("review_queue", "candidate_entity_id"),
    ("facility_assets", "owner_operator_entity_id"),
)

# R8.7 license-fact editor. EDITABLE_FACT_COLS is imported from app.licensing
# (canonical there, no import cycle - licensing has no UI deps); segment /
# sku_or_plan / fact_id / product_id encode a fact's matrix identity and are NOT
# editable. FACT_SEGMENTS validates an optional segment at add-time only.
FACT_SEGMENTS = ("commercial", "gcc", "gcc_high", "dod", "azure_gov", "unknown")
FACT_SOURCE_QUALITY = ("primary", "non-primary")

# Fact date columns validated as ISO-8601 date (or '') on edit.
_FACT_DATE_COLS = ("effective_date", "verified_date")

# R9.5 source registry. Tables that FK-reference source_policies.source_id; a
# hard-delete with any referencing row raises IntegrityError (bare FK, NO
# ACTION). remove_source counts these first and blocks legibly. signals has no
# direct source_id - it references transitively via signals.raw_event_id ->
# raw_events.source_id (surfaced by source_reference_breakdown's join). Mirror
# the exhaustive-FK pattern of _ENTITY_REFERENCING: every bare FK to source_id
# must be listed or an operator source that owns rows in the missing table would
# get past the breakdown and leak a raw IntegrityError.
_SOURCE_REFERENCING = (
    ("raw_events", "source_id"),
    ("source_runs", "source_id"),
    ("facility_assets", "source_id"),
)


def tuning_usage(conn):
    """Which tuning knobs actually reach a live card (R8.7 prioritization).

    Admin renders 30 scoring weights and one half-life per trigger, but a knob
    only changes a score if scoring CONSULTS it, and rescore() reads
    status='active' signals only. This replays app.scoring.account_fit's key
    selection over exactly the rows rescore() would score, so the answer is a
    transcription of the scorer rather than a guess about it. A knob no active
    signal consults is inert: editing it is a no-op until such a signal exists
    (scoring._weight already falls back to a neutral 1.0 for absent keys).

    Read-only. Returns {'weights': {(weight_kind, key), ...},
    'triggers': {trigger_id, ...}} - the keys that are LIVE.
    """
    rows = conn.execute(
        "SELECT s.signal_scope, s.entity_id, s.trigger_id, "
        " e.subsector, e.richness, e.coverage_flag "
        "FROM signals s "
        "LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id "
        "WHERE s.status = 'active'").fetchall()
    applicability_keys = {
        r["key"] for r in conn.execute(
            "SELECT key FROM scoring_weights WHERE weight_kind = 'applicability'")}
    weights, triggers = set(), set()
    for r in rows:
        scope = r["signal_scope"]
        triggers.add(r["trigger_id"])
        weights.add(("scope", scope))
        if not r["entity_id"]:
            # Entity-less: sector is neutral 1.0 (no account to fit); only a
            # regulatory_calendar card consults applicability['default'].
            if scope == "regulatory_calendar":
                weights.add(("applicability", "default"))
            continue
        weights.add(("subsector", r["subsector"]))
        weights.add(("coverage", r["coverage_flag"]))
        if scope == "regulatory_calendar":
            # Subsector-keyed applicability REPLACES richness, falling back to
            # 'default' exactly as account_fit does.
            sub = r["subsector"] or ""
            weights.add(("applicability",
                         sub if sub in applicability_keys else "default"))
        else:
            weights.add(("richness", r["richness"]))
    return {"weights": weights, "triggers": triggers}


@contextlib.contextmanager
def config_write_conn(db_path=None, lock_path=None):
    """Fresh connection holding the single-writer ingestion lock (R3.2) for one
    Admin save. Raises RuntimeError if an ingestion/scoring run holds the lock
    (the page catches it -> "ingestion in progress"). Acquire this INSIDE the
    save handler, never while rendering a GET route (a template render must
    never hold the lock).

    ``lock_path`` is passed straight through to ``ingest_lock``, whose own
    resolution order (explicit arg -> GRIDSIGNALS_LOCK env var -> LOCK_PATH)
    then applies. Routes call this with ``lock_path=None``, so must NOT
    resolve a concrete path here first (e.g. ``lock_path or LOCK_PATH``) —
    that would hand ``ingest_lock`` an already-decided path and the env var
    would never get a turn, the one call site (of eleven in the repo) where
    GRIDSIGNALS_LOCK would silently stop reaching the writer."""
    from app.ingest.runner import ingest_lock
    with ingest_lock(lock_path):
        conn = get_connection(db_path) if db_path else get_connection()
        try:
            yield conn
        finally:
            conn.close()


def _config_str(value):
    """Store config_audit old/new as TEXT; numbers become their str form."""
    return None if value is None else str(value)


def _record_config_edit(conn, table_name, pk, field, old_value, new_value,
                        editor, reason, now):
    """Append one config_audit provenance row (R3.3). Caller does the matching
    UPDATE in the same transaction and commits."""
    conn.execute(
        "INSERT INTO config_audit (table_name, pk, field, old_value, new_value, "
        " editor, reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (table_name, pk, field, _config_str(old_value), _config_str(new_value),
         editor, reason or "", _utcnow_iso(now)))


def _validate_weight_value(new_weight):
    """Parse/validate a scoring weight without touching the DB, so a batch save
    can reject a bad entry before it writes anything. Raises ValueError."""
    try:
        w = float(new_weight)
    except (TypeError, ValueError):
        raise ValueError(f"weight must be a number, got {new_weight!r}")
    if not math.isfinite(w) or w < 0:
        raise ValueError(f"weight must be a finite value >= 0, got {w}")
    return w


def _validate_half_life_value(new_half_life_days):
    """Parse/validate a decay half-life without touching the DB (see
    _validate_weight_value). Raises ValueError."""
    if isinstance(new_half_life_days, bool):
        raise ValueError("half-life must be a whole number of days")
    try:
        f = float(new_half_life_days)
    except (TypeError, ValueError):
        raise ValueError(
            f"half-life must be a whole number of days, got {new_half_life_days!r}")
    hl = int(f)
    if hl != f:
        raise ValueError(
            f"half-life must be a whole number of days, got {new_half_life_days!r}")
    if hl < 1:
        raise ValueError(f"half-life must be >= 1 day, got {hl}")
    return hl


def update_weight(conn, weight_kind, key, new_weight, reason="",
                  editor=CONFIG_EDITOR, now=None):
    """Set a scoring_weights.weight (R7.5 tunable), audit it, and rescore active
    signals so live cards reflect it. Validates a finite weight >= 0 and that the
    (weight_kind, key) row exists (no key insert/delete here - value edits only,
    so scoring.py's neutral-1.0 fallback can't be tripped by a removed key).
    A no-op edit (new == old) writes nothing and does not rescore. Raises
    ValueError (the page surfaces it, never writes). Returns
    {old, new, changed[, scored, decayed]} (rescore stats only when changed)."""
    w = _validate_weight_value(new_weight)
    row = conn.execute(
        "SELECT weight FROM scoring_weights WHERE weight_kind = ? AND key = ?",
        (weight_kind, key)).fetchone()
    if row is None:
        raise ValueError(f"unknown scoring weight {(weight_kind, key)!r}")
    old = row["weight"]
    if w == old:
        # A no-op save writes no provenance row and skips the rescore, so the
        # audit trail records real changes, not button presses (persona pass).
        return {"old": old, "new": w, "changed": False}
    conn.execute(
        "UPDATE scoring_weights SET weight = ? WHERE weight_kind = ? AND key = ?",
        (w, weight_kind, key))
    _record_config_edit(
        conn, "scoring_weights",
        json.dumps({"weight_kind": weight_kind, "key": key}, sort_keys=True),
        "weight", old, w, editor, reason, now)
    try:
        summary = rescore(conn, now=now)
    except Exception:
        conn.rollback()
        raise
    return {"old": old, "new": w, "changed": True, **summary}


def update_half_life(conn, trigger_id, new_half_life_days, reason="",
                     editor=CONFIG_EDITOR, now=None):
    """Set a triggers.decay_half_life_days (R7.4 heuristic), audit it, and
    rescore active signals. Validates a whole number of days >= 1 (it is a decay
    divisor; a fractional or non-positive value is rejected, not truncated) and
    that the trigger exists. A no-op edit writes nothing and does not rescore.
    Raises ValueError. Returns {old, new, changed[, scored, decayed]}."""
    hl = _validate_half_life_value(new_half_life_days)
    row = conn.execute(
        "SELECT decay_half_life_days FROM triggers WHERE trigger_id = ?",
        (trigger_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown trigger {trigger_id!r}")
    old = row["decay_half_life_days"]
    if hl == old:
        return {"old": old, "new": hl, "changed": False}
    conn.execute(
        "UPDATE triggers SET decay_half_life_days = ? WHERE trigger_id = ?",
        (hl, trigger_id))
    _record_config_edit(conn, "triggers", trigger_id, "decay_half_life_days",
                        old, hl, editor, reason, now)
    try:
        summary = rescore(conn, now=now)
    except Exception:
        conn.rollback()
        raise
    return {"old": old, "new": hl, "changed": True, **summary}


def update_tuning(conn, weight_edits=(), half_life_edits=(), reason="",
                  editor=CONFIG_EDITOR, now=None):
    """Apply a batch of weight (R7.5) and half-life (R7.4) edits as ONE Admin
    save, so a tier of related knobs is tuned and audited together instead of
    one button-press per row.

    ``weight_edits``: iterable of (weight_kind, key, new_weight).
    ``half_life_edits``: iterable of (trigger_id, new_half_life_days).

    EVERY value is validated before ANYTHING is written, so one bad entry
    rejects the whole batch rather than half-applying it. Unchanged values are
    skipped by the underlying helpers, so the audit trail still records real
    changes and not button presses. Each applied edit writes its own
    config_audit row (per-field provenance is the R3.3 contract; a batch is a
    UI convenience, not a coarser audit unit). Raises ValueError. Returns
    {'changed': n, 'submitted': n}."""
    parsed_weights = []
    parsed_half_lives = []
    for weight_kind, key, value in weight_edits:
        new = _validate_weight_value(value)
        row = conn.execute(
            "SELECT weight FROM scoring_weights "
            "WHERE weight_kind = ? AND key = ?",
            (weight_kind, key)).fetchone()
        if row is None:
            raise ValueError(f"unknown scoring weight {(weight_kind, key)!r}")
        parsed_weights.append((weight_kind, key, row["weight"], new))
    for trigger_id, value in half_life_edits:
        new = _validate_half_life_value(value)
        row = conn.execute(
            "SELECT decay_half_life_days FROM triggers WHERE trigger_id = ?",
            (trigger_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown trigger {trigger_id!r}")
        parsed_half_lives.append((trigger_id, row["decay_half_life_days"], new))

    changed = 0
    for weight_kind, key, old, new in parsed_weights:
        if new == old:
            continue
        conn.execute(
            "UPDATE scoring_weights SET weight = ? "
            "WHERE weight_kind = ? AND key = ?",
            (new, weight_kind, key))
        _record_config_edit(
            conn, "scoring_weights",
            json.dumps({"weight_kind": weight_kind, "key": key},
                       sort_keys=True),
            "weight", old, new, editor, reason, now)
        changed += 1
    for trigger_id, old, new in parsed_half_lives:
        if new == old:
            continue
        conn.execute(
            "UPDATE triggers SET decay_half_life_days = ? "
            "WHERE trigger_id = ?",
            (new, trigger_id))
        _record_config_edit(conn, "triggers", trigger_id,
                            "decay_half_life_days", old, new, editor,
                            reason, now)
        changed += 1
    if changed:
        try:
            rescore(conn, now=now)
        except Exception:
            conn.rollback()
            raise
    return {"changed": changed,
            "submitted": len(parsed_weights) + len(parsed_half_lives)}


def set_source_enabled(conn, source_id, enabled, reason="",
                       editor=CONFIG_EDITOR, now=None):
    """Toggle source_policies.enabled (R8.7 source-policy review; enabled is
    already runtime-managed) and audit it. No rescore - enabling/disabling a
    source affects the next ingestion run, not existing scores. Validates the
    source exists; a no-op toggle writes nothing. Raises ValueError. Returns
    {old, new, changed}."""
    row = conn.execute(
        "SELECT enabled FROM source_policies WHERE source_id = ?",
        (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown source_id {source_id!r}")
    old = row["enabled"]
    new = 1 if enabled else 0
    if new == old:
        return {"old": old, "new": new, "changed": False}
    conn.execute("UPDATE source_policies SET enabled = ? WHERE source_id = ?",
                 (new, source_id))
    _record_config_edit(conn, "source_policies", source_id, "enabled",
                        old, new, editor, reason, now)
    conn.commit()
    return {"old": old, "new": new, "changed": True}


def config_audit_tail(conn, limit=50):
    """Recent config edits, newest first, as plain dicts (R8.7 recent-changes
    panel / provenance visibility)."""
    rows = conn.execute(
        "SELECT table_name, pk, field, old_value, new_value, editor, reason, ts "
        "FROM config_audit ORDER BY audit_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- incident evidence-tier editor (R8.7; R10.5, R7.12, R3.3) -----------------
#
# The one UI write that mutates a signals row's state. It mirrors the config-write
# path: the route's config_write_conn() owns the single-writer lock + fresh
# connection (R3.2); this helper takes that open conn, validates (ValueError ->
# the route surfaces it, no write), UPDATEs the signal IN PLACE (never delete +
# reinsert - snapshots/evidence FK-reference it), appends one immutable
# incident_tier_edits row in the SAME transaction, and commits. It does NOT
# rescore: a tier change gates outreach (R7.12), it does not move the frozen score.

def retier_incident(conn, signal_id, new_level, reason="",
                    editor=CONFIG_EDITOR, now=None):
    """Operator confirm/re-tier of an incident signal (R8.7, R10.5, R7.12).

    Sets signals.incident_evidence_level = new_level and recomputes
    customer_facing_allowed from it (R7.12: only unconfirmed_early_warning -> 0,
    via the classifier's own customer_facing_for_tier so the two never diverge),
    then appends ONE incident_tier_edits provenance row in the same transaction.
    A no-op (new_level == current) writes nothing (idempotent - the trail records
    real changes, not button presses). Refuses a non-incident signal
    (incident_evidence_level NULL): the editor confirms an EXISTING incident's
    tier, it does not turn an ordinary signal into an incident. Raises ValueError
    (the route surfaces it, never writes). Returns
    {old_level, new_level, old_cfa, new_cfa, changed}."""
    if new_level not in INCIDENT_TIERS:
        raise ValueError(f"unknown incident tier {new_level!r}")
    row = conn.execute(
        "SELECT incident_evidence_level, customer_facing_allowed "
        "FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown signal {signal_id!r}")
    old_level = row["incident_evidence_level"]
    if old_level is None:
        raise ValueError(
            f"signal {signal_id!r} is not an incident — the tier editor "
            "re-tiers an existing incident, it does not create one")
    old_cfa = row["customer_facing_allowed"]
    if new_level == old_level:
        return {"old_level": old_level, "new_level": new_level,
                "old_cfa": old_cfa, "new_cfa": old_cfa, "changed": False}
    new_cfa = customer_facing_for_tier(new_level)
    # Trust gate (R4.1/R7.12): a re-tier that RAISES customer_facing_allowed
    # (unconfirmed -> confirmed/corroborated) clears the card for customer-facing
    # outreach — the one transition that can promote an early warning to a basis
    # for contacting the account. "Nothing surfaces unsourced" applies to that
    # promotion itself, so it requires the operator to record why (which source
    # confirmed it). Suppressing (1 -> 0) and lateral moves stay reason-optional.
    if new_cfa == 1 and old_cfa == 0 and not (reason or "").strip():
        raise ValueError(
            f"Re-tiering to {new_level} clears this card for customer-facing "
            "outreach — record which source confirmed it (a reason is required).")
    conn.execute(
        "UPDATE signals SET incident_evidence_level = ?, "
        "customer_facing_allowed = ? WHERE signal_id = ?",
        (new_level, new_cfa, signal_id))
    conn.execute(
        "INSERT INTO incident_tier_edits (signal_id, old_level, new_level, "
        " old_cfa, new_cfa, editor, reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (signal_id, old_level, new_level, old_cfa, new_cfa, editor,
         reason or "", _utcnow_iso(now)))
    conn.commit()
    return {"old_level": old_level, "new_level": new_level,
            "old_cfa": old_cfa, "new_cfa": new_cfa, "changed": True}


def incident_tier_history(conn, signal_id):
    """The append-only re-tier trail for one signal, newest first (R8.7
    provenance). Plain dicts so the template/tests read row['field']."""
    rows = conn.execute(
        "SELECT old_level, new_level, old_cfa, new_cfa, editor, reason, ts "
        "FROM incident_tier_edits WHERE signal_id = ? "
        "ORDER BY edit_id DESC", (signal_id,)).fetchall()
    return [dict(r) for r in rows]


def recent_retiers(conn, limit=50):
    """Central re-tier audit trail across ALL incident cards, newest first (R8.7
    oversight). incident_tier_history shows one signal's trail on its own card;
    this is the single place an operator sees every re-tier — who changed which
    card's tier, and whether any card was cleared for customer-facing outreach.
    Each incident_tier_edits row is joined to its signal's headline / scope /
    entity / current tier as a plain dict. ``gate_raised`` is True for the one
    transition that clears outreach (old_cfa 0 -> new_cfa 1) and so required a
    recorded reason (R4.1/R7.12) — the panel flags it. ``entity_name`` is None
    for a sector-scoped peer incident (no account). Read-only; ordered by
    edit_id (append-only, so it never ties like ts can)."""
    rows = conn.execute(
        "SELECT ite.edit_id, ite.signal_id, ite.old_level, ite.new_level, "
        " ite.old_cfa, ite.new_cfa, ite.editor, ite.reason, ite.ts, "
        " s.headline, e.name AS entity_name "
        "FROM incident_tier_edits ite "
        "JOIN signals s ON s.signal_id = ite.signal_id "
        "LEFT JOIN watchlist_entities e ON e.entity_id = s.entity_id "
        "ORDER BY ite.edit_id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["gate_raised"] = bool(r["old_cfa"] == 0 and r["new_cfa"] == 1)
        out.append(d)
    return out


def source_policy_rows(conn, now=None):
    """Source policies for the Admin review table (R8.7): each source_health row
    as a plain dict, plus its computed state, its Gate G2 demotion recommendation
    (report-only, R9.5) keyed off the same source_id, its ``origin`` (seed vs
    operator, from source_health), and a ``reference_count`` so the UI renders
    disable-vs-remove (only operator-origin zero-reference sources are
    removable). ``g2`` is None for a source with no rated feedback yet.

    The G2 recommendation carries the R9.11 disagreement gate (KTD3): the SAME
    ``g2_gated`` overlay the Precision page applies is applied here, so the Admin
    source table and the Precision page can NEVER show contradictory demotion
    recommendations for a source (the two-surface consistency invariant). Both
    surfaces window feedback/audit to the reporting month first via
    ``_reporting_month_rows`` (R9.5) — this reader must stay in lockstep with
    ``precision_report``'s G2 windowing or the two-surface invariant above
    breaks silently. The gate needs the per-source judge-human disagreement, so
    this reader now also fetches ``precision_audit_rows``."""
    from app.audit.precision import (
        g2_status, g2_gated, judge_human_disagreement_by_source)
    feedback = _reporting_month_rows(precision_feedback_rows(conn), now)
    audit = _reporting_month_rows(precision_audit_rows(conn), now)
    g2 = g2_status(feedback, now=now)
    dis = judge_human_disagreement_by_source(audit, feedback)
    g2 = g2_gated(g2, dis)
    out = []
    for r in source_health(conn):
        d = dict(r)
        d["state"] = source_state(r, now=now)
        d["g2"] = g2.get(r["source_id"])
        d["reference_count"] = sum(
            source_reference_breakdown(conn, r["source_id"]).values())
        out.append(d)
    return out


# -- watchlist entity editors (R8.7 entity manager; R3.3, R4.4, R6.3) ---------
#
# Write helpers mirror the config-write path above: the page's
# config_write_conn() owns the single-writer lock + fresh connection; each helper
# takes that open conn, validates (ValueError -> the page surfaces it, no write),
# writes + one config_audit row, and commits. NONE of them rescore: entity edits
# change FUTURE resolution/ingestion, never an existing signal's frozen score
# (R8.1). Soft-disable + edits are UPDATE-in-place; the only hard-delete is
# remove_operator_entity, guarded by an FK-referencing-row check.

def _entity_exists(conn, entity_id):
    return conn.execute(
        "SELECT 1 FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone() is not None


def add_watchlist_entity(conn, entity_id, name, fields=None, reason="",
                         editor=CONFIG_EDITOR, now=None):
    """Add an operator watchlist entity (origin='operator', active=1) that the
    seed CSV lacks (R8.7). ``fields`` may set curation columns (a subset of
    EDITABLE_ENTITY_COLS); identifiers are the enrichment pipeline's job. New
    entity rows are not deleted by load_seeds, so they survive reload; a
    from-scratch rebuild drops them (replayable from config_audit). Raises
    ValueError. Returns {'entity_id', 'created': True}."""
    eid = (entity_id or "").strip()
    nm = (name or "").strip()
    if not eid:
        raise ValueError("entity_id is required")
    if not nm:
        raise ValueError("name is required")
    if _entity_exists(conn, eid):
        raise ValueError(f"entity_id {eid!r} already exists")
    fields = fields or {}
    bad = set(fields) - set(EDITABLE_ENTITY_COLS)
    if bad:
        raise ValueError(f"not editable column(s): {sorted(bad)}")
    cols = ["entity_id", "name", "origin", "active"] + list(fields)
    vals = [eid, nm, "operator", 1] + [fields[c] for c in fields]
    placeholders = ",".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO watchlist_entities ({','.join(cols)}) VALUES ({placeholders})",
        vals)
    _record_config_edit(conn, "watchlist_entities", eid, "__add__",
                        None, nm, editor, reason, now)
    conn.commit()
    return {"entity_id": eid, "created": True}


def update_watchlist_entity(conn, entity_id, field, new_value, reason="",
                            editor=CONFIG_EDITOR, now=None):
    """Edit one curation column of a watchlist entity (R8.7). ``field`` must be in
    EDITABLE_ENTITY_COLS - identifiers/name are seed-managed, not edited here.
    A no-op edit writes nothing. Raises ValueError. Returns {old,new,changed}."""
    if field not in EDITABLE_ENTITY_COLS:
        raise ValueError(
            f"{field!r} is not an editable column "
            f"(editable: {', '.join(EDITABLE_ENTITY_COLS)})")
    row = conn.execute(
        f"SELECT {field} AS v FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown entity {entity_id!r}")
    old = row["v"]
    new = "" if new_value is None else str(new_value)
    if new == ("" if old is None else str(old)):
        return {"old": old, "new": new, "changed": False}
    conn.execute(
        f"UPDATE watchlist_entities SET {field} = ? WHERE entity_id = ?",
        (new, entity_id))
    _record_config_edit(conn, "watchlist_entities", entity_id, field,
                        old, new, editor, reason, now)
    conn.commit()
    return {"old": old, "new": new, "changed": True}


def set_entity_active(conn, entity_id, active, reason="",
                      editor=CONFIG_EDITOR, now=None):
    """Soft-disable / re-enable a watchlist entity (R8.7). No rescore - a disable
    stops FUTURE resolution/ingestion (EntityResolver, EDGAR, Account 360 all
    filter active=1) while existing signals keep their frozen scores. A no-op
    toggle writes nothing. Raises ValueError. Returns {old,new,changed}."""
    row = conn.execute(
        "SELECT active FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown entity {entity_id!r}")
    old = row["active"]
    new = 1 if active else 0
    if new == old:
        return {"old": old, "new": new, "changed": False}
    conn.execute("UPDATE watchlist_entities SET active = ? WHERE entity_id = ?",
                 (new, entity_id))
    _record_config_edit(conn, "watchlist_entities", entity_id, "active",
                        old, new, editor, reason, now)
    conn.commit()
    return {"old": old, "new": new, "changed": True}


def add_alias(conn, entity_id, alias, alias_type="common", reason="",
              editor=CONFIG_EDITOR, now=None):
    """Add an operator positive alias for entity resolution (R6.1/R6.3). Written
    with source='operator' so a reload never wipes it. Raises ValueError.
    Returns {'entity_id','alias','created': True}."""
    al = (alias or "").strip()
    if not _entity_exists(conn, entity_id):
        raise ValueError(f"unknown entity {entity_id!r}")
    if not al:
        raise ValueError("alias is required")
    if conn.execute("SELECT 1 FROM entity_aliases WHERE entity_id = ? AND alias = ?",
                    (entity_id, al)).fetchone():
        raise ValueError(f"alias {al!r} already exists for this entity")
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias, alias_type, source) "
        "VALUES (?, ?, ?, ?)", (entity_id, al, alias_type or "common",
                                OPERATOR_SOURCE))
    _record_config_edit(conn, "entity_aliases", entity_id, "alias",
                        None, al, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "alias": al, "created": True}


def remove_alias(conn, entity_id, alias, reason="",
                 editor=CONFIG_EDITOR, now=None):
    """Remove an operator-added alias (R6.3). Refuses to touch a seed alias -
    seed aliases change via reset_entity_to_seed. Raises ValueError. Returns
    {'entity_id','alias','removed': True}."""
    row = conn.execute(
        "SELECT source FROM entity_aliases WHERE entity_id = ? AND alias = ?",
        (entity_id, alias)).fetchone()
    if row is None:
        raise ValueError(f"unknown alias {alias!r} for entity {entity_id!r}")
    if row["source"] != OPERATOR_SOURCE:
        raise ValueError(
            f"alias {alias!r} is seed data - reset the entity to change it")
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?",
                 (entity_id, alias))
    _record_config_edit(conn, "entity_aliases", entity_id, "alias",
                        alias, None, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "alias": alias, "removed": True}


def add_collision_term(conn, entity_id, term, reason="",
                       editor=CONFIG_EDITOR, now=None):
    """Add an operator known-collision negative term (R6.3). Written with
    reason='operator' so a reload never wipes it. Raises ValueError. Returns
    {'entity_id','term','created': True}."""
    tm = (term or "").strip()
    if not _entity_exists(conn, entity_id):
        raise ValueError(f"unknown entity {entity_id!r}")
    if not tm:
        raise ValueError("term is required")
    if conn.execute(
            "SELECT 1 FROM entity_collision_terms WHERE entity_id = ? AND term = ?",
            (entity_id, tm)).fetchone():
        raise ValueError(f"collision term {tm!r} already exists for this entity")
    conn.execute(
        "INSERT INTO entity_collision_terms (entity_id, term, reason) "
        "VALUES (?, ?, ?)", (entity_id, tm, OPERATOR_SOURCE))
    _record_config_edit(conn, "entity_collision_terms", entity_id, "term",
                        None, tm, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "term": tm, "created": True}


def remove_collision_term(conn, entity_id, term, reason="",
                          editor=CONFIG_EDITOR, now=None):
    """Remove an operator-added collision term (R6.3). Refuses to touch a seed
    term. Raises ValueError. Returns {'entity_id','term','removed': True}."""
    row = conn.execute(
        "SELECT reason FROM entity_collision_terms WHERE entity_id = ? AND term = ?",
        (entity_id, term)).fetchone()
    if row is None:
        raise ValueError(f"unknown collision term {term!r} for entity {entity_id!r}")
    if row["reason"] != OPERATOR_SOURCE:
        raise ValueError(
            f"collision term {term!r} is seed data - reset the entity to change it")
    conn.execute(
        "DELETE FROM entity_collision_terms WHERE entity_id = ? AND term = ?",
        (entity_id, term))
    _record_config_edit(conn, "entity_collision_terms", entity_id, "term",
                        term, None, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "term": term, "removed": True}


def entity_reference_breakdown(conn, entity_id):
    """{table: count} for each FK source that references this entity (only
    non-zero entries; both directions of entity_relationships combined). An
    empty dict means a hard-delete is FK-safe. Drives the remove caption/error
    so the operator sees exactly what blocks a remove, not a signals-only count."""
    out = {}
    for table, col in _ENTITY_REFERENCING:
        n = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?",
            (entity_id,)).fetchone()["n"]
        if n:
            out[table] = n
    rel = conn.execute(
        "SELECT COUNT(*) AS n FROM entity_relationships "
        "WHERE parent_entity_id = ? OR child_entity_id = ?",
        (entity_id, entity_id)).fetchone()["n"]
    if rel:
        out["entity_relationships"] = rel
    return out


def _count_entity_references(conn, entity_id):
    """Total rows across every table that FK-references this entity. Zero means
    a hard-delete is FK-safe."""
    return sum(entity_reference_breakdown(conn, entity_id).values())


def reset_entity_to_seed(conn, entity_id, reason="",
                         editor=CONFIG_EDITOR, now=None):
    """Restore a SEEDED entity's editable columns to their seed-CSV values,
    re-enable it, and drop its operator-added aliases / collision terms (R8.7).
    An operator-added entity has no seed to reset to - remove it instead. Reads
    the seed row via the loader; performs NO entity-row delete (FK-safe). Raises
    ValueError. Returns {'entity_id','reset': True}."""
    import os
    from app.db.load_seeds import read_rows, SEEDS_DIR
    row = conn.execute(
        "SELECT origin FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown entity {entity_id!r}")
    if row["origin"] != "seed":
        raise ValueError(
            f"entity {entity_id!r} is operator-added and has no seed to reset to "
            "- remove it instead")
    _, seed_rows = read_rows(os.path.join(SEEDS_DIR, "watchlist_entities.csv"))
    seed = next((r for r in seed_rows if r.get("entity_id") == entity_id), None)
    if seed is None:
        raise ValueError(f"no seed row for entity {entity_id!r}")
    assignments = [f"{c} = ?" for c in EDITABLE_ENTITY_COLS] + ["active = 1"]
    params = [seed.get(c, "") for c in EDITABLE_ENTITY_COLS] + [entity_id]
    conn.execute(
        f"UPDATE watchlist_entities SET {', '.join(assignments)} "
        "WHERE entity_id = ?", params)
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ? AND source = ?",
                 (entity_id, OPERATOR_SOURCE))
    conn.execute(
        "DELETE FROM entity_collision_terms WHERE entity_id = ? AND reason = ?",
        (entity_id, OPERATOR_SOURCE))
    _record_config_edit(conn, "watchlist_entities", entity_id, "__reset__",
                        None, "seed values", editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "reset": True}


def remove_operator_entity(conn, entity_id, reason="",
                           editor=CONFIG_EDITOR, now=None):
    """Hard-delete an OPERATOR-added entity - the one delete path in the chunk,
    guarded by an FK-referencing-row check (R8.7 delete safety; trap 2). Blocks
    with a legible ValueError when any signal / match decision / review-queue /
    facility / relationship row references it (disable it instead). Refuses to
    touch a seeded entity. Deletes the entity's own operator aliases/collision
    terms (leaf rows) with it. Raises ValueError. Returns
    {'entity_id','removed': True}."""
    row = conn.execute(
        "SELECT name, origin FROM watchlist_entities WHERE entity_id = ?",
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown entity {entity_id!r}")
    if row["origin"] != "operator":
        raise ValueError(
            f"entity {entity_id!r} is seed data - disable it instead of removing")
    breakdown = entity_reference_breakdown(conn, entity_id)
    if breakdown:
        detail = ", ".join(f"{table}: {n}" for table, n in breakdown.items())
        total = sum(breakdown.values())
        raise ValueError(
            f"{total} row(s) reference this entity ({detail}) - disable it "
            "instead of removing")
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (entity_id,))
    conn.execute("DELETE FROM entity_collision_terms WHERE entity_id = ?",
                 (entity_id,))
    conn.execute("DELETE FROM watchlist_entities WHERE entity_id = ?", (entity_id,))
    _record_config_edit(conn, "watchlist_entities", entity_id, "__remove__",
                        row["name"], None, editor, reason, now)
    conn.commit()
    return {"entity_id": entity_id, "removed": True}


def entity_editor_rows(conn):
    """Watchlist entities for the Admin manager table (R8.7): id, name, a couple
    of curation columns, active/origin, and a signal-reference count (so the
    operator sees which entities a remove would block). Plain dicts, name-ordered."""
    rows = conn.execute(
        "SELECT e.entity_id, e.name, e.subsector, e.gov_cloud_likelihood, "
        "       e.active, e.origin, "
        "       (SELECT COUNT(*) FROM signals s WHERE s.entity_id = e.entity_id) "
        "         AS signal_count "
        "FROM watchlist_entities e ORDER BY e.name").fetchall()
    return [dict(r) for r in rows]


def entity_alias_rows(conn, entity_id):
    """Aliases + collision terms for one entity (R8.7 alias/collision editor),
    each flagged operator_editable when it is an operator-added row (seed rows
    change only via reset). Returns {'aliases': [...], 'collision_terms': [...]}."""
    aliases = [{
        "alias": r["alias"], "alias_type": r["alias_type"], "source": r["source"],
        "operator_editable": r["source"] == OPERATOR_SOURCE,
    } for r in conn.execute(
        "SELECT alias, alias_type, source FROM entity_aliases "
        "WHERE entity_id = ? ORDER BY alias", (entity_id,)).fetchall()]
    terms = [{
        "term": r["term"], "reason": r["reason"],
        "operator_editable": r["reason"] == OPERATOR_SOURCE,
    } for r in conn.execute(
        "SELECT term, reason FROM entity_collision_terms "
        "WHERE entity_id = ? ORDER BY term", (entity_id,)).fetchall()]
    return {"aliases": aliases, "collision_terms": terms}


# -- regulatory monitor (R8.4, R7.2, D8) -------------------------------------
#
# Raw regulatory records that the current regulatory classifier evaluated and
# did NOT graduate to the signal feed. "Regulatory" is the same source set the
# classifier reads; derive it from app.classify.regulatory.SOURCES so the UI
# cannot silently drift when that classifier adds or removes a source.
# Non-graduated = classified_events says this parser emitted zero signals, and
# no signals row references the raw_event. The classified_events gate matters:
# an unprocessed backlog item may still become a scored signal, so it must not
# be displayed as "chatter" yet.
REGULATORY_SOURCE_IDS = tuple(regulatory_classifier.SOURCES)
_REGULATORY_CLASSIFIER_ID = regulatory_classifier.CLASSIFIER_ID
_REGULATORY_PARSER_VERSION = regulatory_classifier.PARSER_VERSION

# Federal Register document type -> display label; mirrors the label map in
# app.classify.regulatory._headline so the two surfaces read identically.
_FR_DOC_TYPE_LABELS = {
    "Rule": "final rule", "Proposed Rule": "proposed rule", "Notice": "notice"}
# nerc_pages snapshots have no document type (they are page-diff records).
_NERC_SNAPSHOT_LABEL = "page snapshot"
_MAX_REGULATORY_HEADLINE_CHARS = 140


def _regulatory_agency(payload, source_id, source_name):
    """Agency string, taken verbatim from the payload where present (never
    inferred). nerc_pages records are NERC; a Federal Register record uses its
    own agency name, falling back to the source policy name."""
    if source_id == "nerc_pages":
        return "NERC"
    for agency in payload.get("agencies") or []:
        if isinstance(agency, dict):
            name = (agency.get("name") or agency.get("raw_name") or "").strip()
            if name:
                return name
    for name in payload.get("agency_names") or []:
        if isinstance(name, str) and name.strip():
            return name.strip()
    return source_name or source_id


def _page_label(page_url):
    """A human page label for a nerc_pages record: the URL's last non-empty path
    segment (e.g. 'CIPStandards.aspx'), falling back to the bare page_url when it
    has no path segment. Derived only from the URL's own text - nothing invented."""
    path = page_url.split("?", 1)[0].split("#", 1)[0]
    for segment in reversed(path.split("/")):
        if segment.strip():
            return segment.strip()
    return page_url


def _truncate_title(title, budget):
    """Trim a quoted-headline title to ``budget`` characters, breaking on a word
    boundary when trivial and appending an ellipsis. The caller composes the
    surrounding quotes AFTER this, so the closing quote always balances."""
    if budget < 1 or len(title) <= budget:
        return title
    cut = title[:budget - 1].rstrip()
    space = cut.rfind(" ")
    if space >= budget // 2:            # prefer a word boundary if not too early
        cut = cut[:space].rstrip()
    return cut + "…"


def _regulatory_chatter_row(row):
    """Shape one non-graduated regulatory raw_event into a display dict, all
    fields derived VERBATIM from payload keys - no action verb is ever inferred
    from the scope or date (D8). Non-JSON / malformed payloads fall back to a
    safe snippet and never raise."""
    source_id = row["source_id"]
    source_name = row["source_name"]
    try:
        payload = json.loads(row["payload"] or "")
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}

    agency = _regulatory_agency(payload, source_id, source_name)
    if source_id == "nerc_pages":
        doc_type_label = _NERC_SNAPSHOT_LABEL
        # nerc_pages carries no title; use a human page label (the page_url's
        # last path segment) UNQUOTED - a raw URL wrapped in quotes reads as a
        # mis-parsed title, and the full URL stays in the record's Source link.
        page_url = (payload.get("page_url") or row["url"] or "").strip()
        title = _page_label(page_url)
        # Unquoted label; the full URL is still shown via the Source link.
        headline = f"{agency} {doc_type_label}: {title}" if title \
            else f"{agency} {doc_type_label}"
    else:
        doc_type_label = _FR_DOC_TYPE_LABELS.get(
            payload.get("type"), (payload.get("type") or "document"))
        title = (payload.get("title") or "").strip()
        if not title:
            # Last resort: a safe snippet of the raw payload, never empty.
            title = _payload_snippet(row["payload"]) or "(untitled record)"
        # Truncate the TITLE to a budget FIRST, then compose the quoted headline
        # around it, so the closing quote is always appended after truncation and
        # the quote pair always balances (never a dangling opening quote).
        budget = _MAX_REGULATORY_HEADLINE_CHARS - len(f'{agency} {doc_type_label}: ""')
        headline = f'{agency} {doc_type_label}: "{_truncate_title(title, budget)}"'

    # Descriptive "does it carry a compliance clock" flag - NOT a graduation
    # claim (a chatter record can carry an anchor and still not have graduated).
    has_anchor = bool(payload.get("effective_on")
                      or payload.get("comments_close_on"))

    return {
        "raw_event_id": row["raw_event_id"],
        "source_id": source_id,
        "source_name": source_name,
        "agency": agency,
        "doc_type_label": doc_type_label,
        "title": title,
        "headline": headline,
        "has_anchor": has_anchor,
        "event_date": row["event_date"],
        "url": row["url"],
        "docket_ids": payload.get("docket_ids") or [],
        "comments_close_on": payload.get("comments_close_on") or None,
        "effective_on": payload.get("effective_on") or None,
    }


def regulatory_monitor(conn, limit=100):
    """Non-graduated regulatory chatter, newest first (R8.4). Regulatory
    raw_events (REGULATORY_SOURCE_IDS) that the current regulatory parser has
    evaluated with zero emitted signals and with NO signals row referencing
    them - i.e. records that did NOT graduate to the signal feed. Read-only:
    this NEVER writes and NEVER emits a score/confidence/entity_name/
    signal_scope/signal_id. Returns up to ``limit`` display dicts."""
    placeholders = _placeholders(len(REGULATORY_SOURCE_IDS))
    rows = conn.execute(
        "SELECT re.raw_event_id, re.source_id, re.event_date, re.payload, "
        " re.url, re.first_seen_at, sp.name AS source_name "
        "FROM raw_events re "
        "JOIN source_policies sp ON sp.source_id = re.source_id "
        f"WHERE re.source_id IN ({placeholders}) "
        " AND EXISTS (SELECT 1 FROM classified_events ce "
        "             WHERE ce.raw_event_id = re.raw_event_id "
        "               AND ce.classifier_id = ? "
        "               AND ce.parser_version = ? "
        "               AND ce.signals_emitted = 0) "
        " AND NOT EXISTS (SELECT 1 FROM signals s "
        "                 WHERE s.raw_event_id = re.raw_event_id) "
        "ORDER BY re.event_date DESC, re.raw_event_id DESC "
        "LIMIT ?",
        list(REGULATORY_SOURCE_IDS) + [
            _REGULATORY_CLASSIFIER_ID, _REGULATORY_PARSER_VERSION, limit,
        ]).fetchall()
    return [_regulatory_chatter_row(r) for r in rows]


# -- license-fact editors (R8.7; R3.3, R7.6) ---------------------------------
#
# license_facts is owned by the app.licensing transform, not load_seeds. These
# helpers mirror the config-write path: validate (ValueError -> the page
# surfaces it, no write), write + one config_audit row, commit. NONE rescore -
# cards read frozen license_play_snapshots (R7.6), never live facts, so a fact
# edit never moves an existing card; it is picked up by the NEXT play snapshot.
# There is no fact-delete path (edit + add + supersede only): fact_snapshot_
# citations is the JSON-scan gate the FK-COUNT pattern cannot see, surfaced as a
# caption ("cited by N cards - edit in place"), not a delete guard.


def _valid_fact_date(value):
    """A fact date column accepts '' (unset) or an ISO-8601 date; else False."""
    v = (value or "").strip()
    if not v:
        return True
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _is_future_date(value, now=None):
    """True when value parses to a date after today (UTC). '' and unparseable
    are not future (parseability is checked separately)."""
    try:
        d = date.fromisoformat((value or "").strip())
    except ValueError:
        return False
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    return d > today


def _fact_id_list(raw):
    """Return fact_ids only when the snapshot column holds a JSON array."""
    try:
        ids = json.loads(raw or "[]")
    except ValueError:
        return []
    return ids if isinstance(ids, list) else []


def _normalize_evidence_rank(value):
    """Evidence rank is optional, but when present it must be documented 1..3."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if not v.isdigit():
            raise ValueError("evidence_rank must be 1, 2, or 3")
        rank = int(v)
    else:
        try:
            rank = int(value)
        except (TypeError, ValueError):
            raise ValueError("evidence_rank must be 1, 2, or 3")
    if rank not in (1, 2, 3):
        raise ValueError("evidence_rank must be 1, 2, or 3")
    return rank


def update_license_fact(conn, fact_id, field, new_value, reason="",
                        editor=CONFIG_EDITOR, now=None):
    """Edit one editable column of a license fact (R8.7). ``field`` must be in
    EDITABLE_FACT_COLS (segment / sku_or_plan / identity are not editable).
    source_quality is validated against FACT_SOURCE_QUALITY; date columns must be
    an ISO-8601 date or ''. A no-op edit writes nothing and does not rescore.
    Raises ValueError. Returns {old, new, changed}."""
    if field not in EDITABLE_FACT_COLS:
        raise ValueError(
            f"{field!r} is not an editable column "
            f"(editable: {', '.join(EDITABLE_FACT_COLS)})")
    new = "" if new_value is None else str(new_value)
    if field == "source_quality" and new and new not in FACT_SOURCE_QUALITY:
        raise ValueError(
            f"source_quality must be one of {FACT_SOURCE_QUALITY}, got {new!r}")
    if field in _FACT_DATE_COLS and not _valid_fact_date(new):
        raise ValueError(f"{field} must be an ISO-8601 date or empty, got {new!r}")
    # verified_date only: a future date would compute a negative age and hide the
    # fact from the R10.7 staleness list (a fact can't be "verified" tomorrow).
    # effective_date may legitimately be future (a rule effective 2026-10-01).
    if field == "verified_date" and _is_future_date(new, now):
        raise ValueError("verified_date cannot be in the future")
    row = conn.execute(
        f"SELECT {field} AS v FROM license_facts WHERE fact_id = ?",
        (fact_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown fact {fact_id!r}")
    old = row["v"]
    if new == ("" if old is None else str(old)):
        return {"old": old, "new": new, "changed": False}
    conn.execute(
        f"UPDATE license_facts SET {field} = ? WHERE fact_id = ?",
        (new, fact_id))
    _record_config_edit(conn, "license_facts", fact_id, field,
                        old, new, editor, reason, now)
    conn.commit()
    return {"old": old, "new": new, "changed": True}


def add_license_fact(conn, fact_id, product_id, fields=None, reason="",
                     editor=CONFIG_EDITOR, now=None):
    """Add an operator license fact the transform lacks (R8.7). ``fact_id`` must
    be unique; ``product_id`` must exist in products or be None/''. ``fields``
    may set EDITABLE_FACT_COLS and an optional ``segment`` (validated against
    FACT_SEGMENTS). sku_or_plan is left NULL - that is the marker the licensing
    rebuild uses to leave operator facts untouched. Raises ValueError. Returns
    {'fact_id', 'created': True}."""
    fid = (fact_id or "").strip()
    if not fid:
        raise ValueError("fact_id is required")
    if conn.execute("SELECT 1 FROM license_facts WHERE fact_id = ?",
                    (fid,)).fetchone():
        raise ValueError(f"fact_id {fid!r} already exists")
    pid = (product_id or "").strip() or None
    if pid is not None and not conn.execute(
            "SELECT 1 FROM products WHERE product_id = ?", (pid,)).fetchone():
        raise ValueError(f"unknown product_id {pid!r}")
    fields = dict(fields or {})
    segment = fields.pop("segment", None)
    bad = set(fields) - set(EDITABLE_FACT_COLS)
    if bad:
        raise ValueError(f"not editable column(s): {sorted(bad)}")
    if segment is not None and segment not in FACT_SEGMENTS:
        raise ValueError(f"segment must be one of {FACT_SEGMENTS}, got {segment!r}")
    sq = fields.get("source_quality")
    if sq and sq not in FACT_SOURCE_QUALITY:
        raise ValueError(
            f"source_quality must be one of {FACT_SOURCE_QUALITY}, got {sq!r}")
    for dc in _FACT_DATE_COLS:
        if dc in fields and not _valid_fact_date(fields[dc]):
            raise ValueError(
                f"{dc} must be an ISO-8601 date or empty, got {fields[dc]!r}")
    # verified_date only: future dates would hide the fact from staleness (see
    # update_license_fact); effective_date may legitimately be future.
    if "verified_date" in fields and _is_future_date(fields["verified_date"], now):
        raise ValueError("verified_date cannot be in the future")
    cols = ["fact_id", "product_id", "segment"] + list(fields)
    vals = [fid, pid, segment] + [fields[c] for c in fields]
    conn.execute(
        f"INSERT INTO license_facts ({','.join(cols)}) "
        f"VALUES ({_placeholders(len(cols))})", vals)
    # Record the created fact_id as new_value so the audit trail says WHAT was
    # created (product_id may be None for a product-less operator fact).
    _record_config_edit(conn, "license_facts", fid, "__add__",
                        None, fid, editor, reason, now)
    conn.commit()
    return {"fact_id": fid, "created": True}


def fact_snapshot_citations(conn, fact_id):
    """Count of license_play_snapshots that cite this fact_id (R7.6). fact_ids is
    a JSON array TEXT (not a real FK: 0001_initial.sql), so this hand-scans and
    json.loads each, guarding malformed JSON like signal_detail. This is the
    citation gate the FK-COUNT breakdown pattern cannot see."""
    n = 0
    for r in conn.execute("SELECT fact_ids FROM license_play_snapshots"):
        if fact_id in _fact_id_list(r["fact_ids"]):
            n += 1
    return n


def license_fact_rows(conn):
    """License facts for the Admin editor selectbox (R8.7): every EDITABLE_FACT_COL
    plus identity columns and the operator/transform origin (sku_or_plan IS NULL
    => operator-added). Plain dicts, ordered by fact_id."""
    rows = conn.execute(
        "SELECT lf.fact_id, lf.product_id, lf.sku_or_plan, lf.segment, "
        " lf.price_note, lf.included_or_addon, lf.prerequisite, "
        " lf.effective_date, lf.verified_date, lf.source_quality, lf.source_url, "
        " p.name AS product_name "
        "FROM license_facts lf "
        "LEFT JOIN products p ON p.product_id = lf.product_id "
        "ORDER BY lf.fact_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["origin"] = "operator" if r["sku_or_plan"] is None else "transform"
        out.append(d)
    return out


# -- source registry (R9.5 add / remove) -------------------------------------
#
# add_source writes origin='operator' (migration 0009) so a reload never
# clobbers it and remove can tell it apart from a seeded row. remove_source is
# the only source hard-delete: refused for seeded sources (disable instead) and
# FK-gated for operator ones (a referencing raw_event / run / signal turns the
# click into a legible error, never a leaked IntegrityError). Editing a seeded
# source's policy fields is out of scope for this chunk (follow-up).


def source_reference_breakdown(conn, source_id):
    """{table: count} for each source that references this source_id (only
    non-zero entries): raw_events + source_runs directly, and signals
    transitively via signals.raw_event_id -> raw_events.source_id (there is no
    signals.source_id FK). An empty dict means a hard-delete is FK-safe."""
    out = {}
    for table, col in _SOURCE_REFERENCING:
        n = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?",
            (source_id,)).fetchone()["n"]
        if n:
            out[table] = n
    signals = conn.execute(
        "SELECT COUNT(*) AS n FROM signals s "
        "JOIN raw_events re ON re.raw_event_id = s.raw_event_id "
        "WHERE re.source_id = ?", (source_id,)).fetchone()["n"]
    if signals:
        out["signals"] = signals
    return out


def add_source(conn, source_id, name, access_method="", ttl=None,
               evidence_rank=None, tos_status="", rate_limit="",
               last_policy_review="", enabled=True, reason="",
               editor=CONFIG_EDITOR, now=None):
    """Add an operator source policy the seed set lacks (origin='operator',
    R9.5). ``source_id`` must be unique and ``name`` non-empty. New rows are not
    deleted by load_seeds, so they survive reload. Raises ValueError. Returns
    {'source_id', 'created': True}."""
    sid = (source_id or "").strip()
    nm = (name or "").strip()
    if not sid:
        raise ValueError("source_id is required")
    if not nm:
        raise ValueError("name is required")
    if conn.execute("SELECT 1 FROM source_policies WHERE source_id = ?",
                    (sid,)).fetchone():
        raise ValueError(f"source_id {sid!r} already exists")
    rank = _normalize_evidence_rank(evidence_rank)
    conn.execute(
        "INSERT INTO source_policies (source_id, name, access_method, ttl, "
        " enabled, tos_status, evidence_rank, rate_limit, last_policy_review, "
        " origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'operator')",
        (sid, nm, access_method or "", ttl, 1 if enabled else 0,
         tos_status or "", rank, rate_limit or "",
         last_policy_review or ""))
    _record_config_edit(conn, "source_policies", sid, "__add__",
                        None, nm, editor, reason, now)
    conn.commit()
    return {"source_id": sid, "created": True}


def remove_source(conn, source_id, reason="", editor=CONFIG_EDITOR, now=None):
    """Hard-delete an OPERATOR-added source (R9.5), guarded by an FK-referencing-
    row check. Refuses a seeded source (disable it instead). Blocks with a
    legible ValueError when any raw_event / source_run / signal references it,
    never leaking an IntegrityError. Raises ValueError. Returns
    {'source_id', 'removed': True}."""
    row = conn.execute(
        "SELECT origin FROM source_policies WHERE source_id = ?",
        (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown source_id {source_id!r}")
    if row["origin"] != "operator":
        raise ValueError(
            f"source {source_id!r} is seeded - disable it instead of removing")
    breakdown = source_reference_breakdown(conn, source_id)
    if breakdown:
        detail = ", ".join(f"{table}: {n}" for table, n in breakdown.items())
        total = sum(breakdown.values())
        raise ValueError(
            f"{total} row(s) reference this source ({detail}) - disable it "
            "instead of removing")
    # Belt-and-suspenders: even if a future bare FK to source_id is not yet in
    # _SOURCE_REFERENCING, the DELETE must never leak a raw IntegrityError to the
    # UI - turn it into the same legible ValueError.
    try:
        conn.execute("DELETE FROM source_policies WHERE source_id = ?",
                     (source_id,))
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(
            "other rows reference this source - disable it instead of removing")
    _record_config_edit(conn, "source_policies", source_id, "__remove__",
                        source_id, None, editor, reason, now)
    conn.commit()
    return {"source_id": source_id, "removed": True}


# -- explore analytics + evidence-safe facility map (R8.5, R4.1) --------------
#
# The Explore page's two reads. Both are strictly read-only aggregations.
#
# Analytics (a): signal counts sliced by trigger / signal_scope / incident tier.
# R4.1 (nothing surfaces unsourced) holds by construction: every ``signals`` row
# is minted by a classifier from a sourced raw_event and carries ranked evidence
# — a count here is a count of sourced signals, and each returned row keeps the
# dimension identity (trigger_name, scope, tier) so a number is inspectable back
# to the same evidence/scope treatment the cards carry. Default is active-only,
# matching the feed (decayed/superseded/dismissed keep their frozen state but do
# not inflate the live analytics).
#
# Facility points (b): THE 0.85 EVIDENCE GATE LIVES IN THIS READER. A facility
# whose facility_owner_confidence is NULL or < 0.85 can never leave this
# function, so an under-evidenced point can never reach the view (R8.5: no
# inferred points). Only gated points join to their owner entity + subsector.
#
# Geography rollup (c): per-state signal density for choropleth shading, derived
# ONLY from gated facilities — a state's count is the sum of active signals of
# the entities that own a >=0.85 facility there. States with no gated facility /
# no signals read 0, never a fabricated value (R6.6).

FACILITY_OWNER_CONFIDENCE_FLOOR = 0.85


def explore_analytics_counts(conn, statuses=("active",)):
    """Signal counts sliced three ways for the Explore Analytics tab (R8.5,
    R4.1). Returns ``{'trigger': [...], 'scope': [...], 'incident_tier': [...]}``
    where each list is plain dicts (label/key/count), most-frequent first. Every
    counted row is a sourced signal (R4.1) — the count carries its dimension
    identity so it is inspectable back to the same signals the cards render.
    ``statuses`` filters signal status (default active-only, matching the feed).

    Always a LIVE computation. The computation itself lives in
    ``app.aggregates.compute_counts`` so the live path and the precomputed R8.10
    path cannot drift; ``analytics_counts`` below is the aggregate-aware reader.
    """
    return aggregates.compute_counts(conn, statuses)


# -- Precomputed aggregates (R8.10) -------------------------------------------
#
# ``app.aggregates`` refreshes the analytics counts nightly as a pipeline step.
# A precomputed number is a claim about a PAST store, so this reader's contract
# is: a stale aggregate is never served, and never served silently.
#
#   * fresh    -> the stored counts, with the refresh's own ``computed_at``.
#   * anything -> a live recompute, returned with ``aggregate_stale=True`` and
#     else         the reason. The caller gets today's truth AND is told the
#                  precomputed layer is behind, so a stale number can neither
#                  reach a reader nor hide the fact that the refresh is broken.
#
# Staleness is decided by three cheap checks, in order: does an aggregate exist
# for this status filter at all; does its stored basis fingerprint still match
# the store (see app.aggregates.basis_fingerprint); and is its stamp inside the
# nightly window. The fingerprint is what makes the guard real — a purely
# time-based rule would happily serve counts taken before a mid-day retraction
# flipped 95 signals, which is exactly the failure this reader exists to
# prevent.
#
# The "caching layer" half of R8.10 is deliberately REQUEST-SCOPED memoization
# and nothing more: pass a plain dict as ``memo`` (one per request, owned by the
# caller) and repeat reads inside that request are answered from it. The clause
# exists to replace Streamlit's caching, and Streamlit is gone. A process-global
# cache is the thing NOT built here on purpose — it would outlive a refresh and
# re-introduce exactly the silent staleness the checks above close.

# Nightly cadence plus slack: an aggregate older than this is presumed to have
# missed its refresh even if the store has not moved since.
AGGREGATE_MAX_AGE_SECONDS = 26 * 3600

AGGREGATE_REFRESH_COMMAND = "python -m app.aggregates"


def _aggregate_state(conn, name):
    return conn.execute(
        "SELECT computed_at, status_filter, basis, refresh_version "
        "FROM aggregate_state WHERE aggregate_name = ?", (name,)).fetchone()


def _aggregate_stale_reason(conn, state, statuses, now=None):
    """None when the stored aggregate may be served; else why it may not."""
    if state is None:
        return "missing"
    if state["status_filter"] != aggregates.status_key(statuses):
        return "status_filter_unsupported"
    if state["basis"] != aggregates.basis_fingerprint(conn):
        return "basis_changed"
    computed = _parse_dt(state["computed_at"])
    # A stamp we cannot read (or one written without a UTC offset, against
    # R10.2) tells us nothing about age, so it is not servable.
    if computed is None or computed.tzinfo is None:
        return "unreadable_stamp"
    age = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc)
           - computed).total_seconds()
    # A future-dated stamp is not "extra fresh" — it is a stamp we cannot
    # trust, so it fails the same way an old one does.
    if age < 0 or age > AGGREGATE_MAX_AGE_SECONDS:
        return "expired"
    return None


def _read_aggregate_counts(conn, name):
    """Stored counts in the same shape and order compute_counts returns."""
    out = {dimension: [] for dimension in aggregates.DIMENSIONS}
    for r in conn.execute(
            "SELECT dimension, key, label, count FROM signal_aggregates "
            "WHERE aggregate_name = ? ORDER BY dimension, count DESC, key",
            (name,)):
        if r["dimension"] in out:
            out[r["dimension"]].append(
                {"key": r["key"], "label": r["label"], "count": r["count"]})
    return out


def analytics_counts(conn, statuses=aggregates.DEFAULT_STATUSES, now=None,
                     memo=None):
    """Analytics counts plus their freshness envelope (R8.10, R10.2).

    Returns::

        {"counts": {trigger/scope/incident_tier lists, as
                    explore_analytics_counts returns them},
         "served_from": "aggregate" | "live",
         "as_of": UTC ISO-8601 the returned counts describe,
         "aggregate_as_of": the stored refresh's stamp, or None if never run,
         "aggregate_stale": bool,
         "stale_reason": None | "missing" | "status_filter_unsupported"
                         | "basis_changed" | "expired" | "unreadable_stamp",
         "refresh_command": how to fix a stale aggregate}

    ``counts`` is always current: when the aggregate cannot be trusted the
    numbers are recomputed live and the envelope says so. ``memo``, when given,
    is a caller-owned request-scoped dict (see the note above).
    """
    statuses = tuple(statuses)
    memo_key = ("analytics_counts", aggregates.status_key(statuses))
    if memo is not None and memo_key in memo:
        return memo[memo_key]

    state = _aggregate_state(conn, aggregates.ANALYTICS_COUNTS)
    reason = _aggregate_stale_reason(conn, state, statuses, now=now)
    if reason is None:
        result = {
            "counts": _read_aggregate_counts(conn,
                                             aggregates.ANALYTICS_COUNTS),
            "served_from": "aggregate",
            "as_of": state["computed_at"],
            "aggregate_as_of": state["computed_at"],
            "aggregate_stale": False,
            "stale_reason": None,
            "refresh_command": AGGREGATE_REFRESH_COMMAND,
        }
    else:
        result = {
            "counts": aggregates.compute_counts(conn, statuses),
            "served_from": "live",
            "as_of": _utcnow_iso(now),
            "aggregate_as_of": state["computed_at"] if state else None,
            "aggregate_stale": True,
            "stale_reason": reason,
            "refresh_command": AGGREGATE_REFRESH_COMMAND,
        }
    if memo is not None:
        memo[memo_key] = result
    return result


def explore_facility_points(conn):
    """Evidence-gated facility points for the Watchlist Map (R8.5). Returns ONLY
    facilities whose ``facility_owner_confidence >= 0.85`` (the gate lives HERE,
    so an under-evidenced point can never reach the view — a 0.5 facility is
    never returned, a 0.9 facility is), joined to their owner entity + subsector.
    Facilities with a NULL confidence or no located owner are excluded. Each
    plain dict carries facility_id, name, lat/long, entity_id + entity_name,
    subsector, capacity_mw, and confidence. Rows with a NULL latitude/longitude
    are dropped (unprojectable). Ordered by facility_id for determinism."""
    rows = conn.execute(
        "SELECT fa.facility_id, fa.facility_name, fa.latitude, fa.longitude, "
        " fa.capacity_mw, fa.facility_owner_confidence, "
        " fa.owner_operator_entity_id AS entity_id, "
        " e.name AS entity_name, e.subsector "
        "FROM facility_assets fa "
        "JOIN watchlist_entities e "
        "  ON e.entity_id = fa.owner_operator_entity_id "
        "WHERE fa.facility_owner_confidence IS NOT NULL "
        "  AND fa.facility_owner_confidence >= ? "
        "  AND fa.latitude IS NOT NULL AND fa.longitude IS NOT NULL "
        "ORDER BY fa.facility_id",
        (FACILITY_OWNER_CONFIDENCE_FLOOR,)).fetchall()
    return [dict(r) for r in rows]


def explore_state_density(conn, statuses=("active",)):
    """Per-owner-entity active-signal counts for the map choropleth, keyed by the
    entity that owns a GATED (>=0.85) facility (R8.5). Returns a list of plain
    dicts (entity_id, entity_name, subsector, latitude, longitude, confidence,
    signal_count) — one per gated facility, carrying the owner's count of
    ``statuses`` signals so render.py can project each to its state and sum the
    density. Density is derived only from gated facilities, so a state with no
    gated facility / no signals contributes nothing and reads 0 (R6.6). A
    facility whose owner entity is off-watchlist (no signals) carries count 0."""
    statuses = tuple(statuses)
    ph = _placeholders(len(statuses))
    rows = conn.execute(
        "SELECT fa.facility_id, fa.latitude, fa.longitude, "
        " fa.facility_owner_confidence AS confidence, "
        " fa.owner_operator_entity_id AS entity_id, "
        " e.name AS entity_name, e.subsector, "
        " (SELECT COUNT(*) FROM signals s "
        f"   WHERE s.entity_id = fa.owner_operator_entity_id "
        f"     AND s.status IN ({ph})) AS signal_count "
        "FROM facility_assets fa "
        "JOIN watchlist_entities e "
        "  ON e.entity_id = fa.owner_operator_entity_id "
        "WHERE fa.facility_owner_confidence IS NOT NULL "
        "  AND fa.facility_owner_confidence >= ? "
        "  AND fa.latitude IS NOT NULL AND fa.longitude IS NOT NULL "
        "ORDER BY fa.facility_id",
        list(statuses) + [FACILITY_OWNER_CONFIDENCE_FLOOR]).fetchall()
    return [dict(r) for r in rows]


# -- Explore: Ransomware Activity (R8.5, R4.1, R10.5, R6.6) ------------------
#
# The AGGREGATE counterpart to the peer-card gate in app/classify/ransomware.py.
# That gate is deliberately narrow — only an energy-industry victim supports the
# "sector peer" claim a peer card makes — which leaves the other ~98 leak-site
# listings per pull classified but unsurfaced. They are still real threat
# activity; they are just not evidence about any watchlist account. This read
# gives them the only honest home: counts, over a stated window, with no score,
# no entity, and no account implication (the R8.4 Regulatory Monitor's framing).
#
# R4.1 IN AGGREGATE. The per-card rule forbids printing the victim name OR the
# attacker-chosen group string, because a crew can name itself after its victim.
# An N:1 aggregate has no single identifiable victim, so counts are safe — but
# that protection is only real while N > 1. A crew with exactly ONE listing is a
# 1:1 mapping back to one company, so a self-named crew would leak that
# company's identity through the crew column. CREW_MIN_VICTIMS is therefore a
# PRIVACY GATE, not display truncation, and it lives here rather than in
# render.py: a singleton crew name is never returned to the view layer at all,
# so no template change can leak it. The withheld crews are still COUNTED (the
# total is honest) and reported as a number, so the panel never implies it is
# showing everything (R6.6).
#
# Marginals only, never a cross-tab. Crew-by-industry would re-identify: with
# two energy listings in the current corpus, a single populated cell names the
# crew that hit one specific company. The two distributions are returned
# independently and must stay that way.
#
# Window: DERIVED from the data, never assumed. ransomware.live is a rolling
# recent feed — the stored corpus spans days, not the year a "trailing 12
# months" label would imply — so the caller gets the real first/last event_date
# and states them.

RANSOMWARE_SOURCE_ID = ransomware_classifier.SOURCE_ID

# Attribution is a LICENSE CONDITION, not decoration: the source policy carries
# tos_status 'approved_cc_by_4.0', and CC-BY-4.0 requires crediting the source
# wherever its data is presented (R10.4). The populated panel must therefore
# name and link ransomware.live, not just the empty state.
RANSOMWARE_SOURCE_URL = "https://www.ransomware.live"

# A named crew must cover at least this many DISTINCT VICTIMS (see R4.1 above).
# Distinct victims, not listings: ransomware.live exposes no stable per-victim
# id, so an upstream record revision mints a second raw_event for the same
# company (see the KNOWN LIMITATIONS in app/classify/ransomware.py). Counting
# listings would let a self-named crew with two revisions of ONE victim clear
# the floor and name that company — precisely what the floor exists to prevent.
CREW_MIN_VICTIMS = 2

# The tracker's own "no industry determined" markers. Counted as unclassified
# rather than dropped: 17 of 100 in the current corpus carry no industry, and
# hiding them would overstate how much of the feed is actually categorized.
UNCLASSIFIED_ACTIVITIES = frozenset({"", "not found", "unknown", "n/a"})


def _is_unclassified_activity(activity):
    """True when the tracker gave no usable industry for this listing."""
    return " ".join((activity or "").split()).lower() in UNCLASSIFIED_ACTIVITIES


# -- baseline delta: the boundary-day trap (R8.5, R6.6) ----------------------
#
# A "change vs prior window" number on a trust panel must not be manufactured
# out of how the feed happens to be fetched. BOTH ENDS of the covered window are
# PARTIAL DAYS, for two independent reasons:
#
#   * the OLDEST covered day is clipped by the FEED. /v2/recentvictims returns a
#     fixed-size slice of the newest listings (100 records in the stored
#     corpus), so whichever day the slice runs out on is truncated, not quiet —
#     the live corpus holds 7 listings on its oldest day against 14-38 on the
#     interior days.
#   * the NEWEST covered day is clipped by the CLOCK. The run that stored the
#     corpus finished mid-afternoon UTC on its own last event_date, so that day
#     holds only part of a day's listings.
#
# Splitting the covered window down the middle therefore drops a truncated day
# into the PRIOR half every single time and reports "rising" by construction, on
# the one panel whose entire purpose is trust. Both boundary days are excluded
# from both halves. When the remaining interior spans an odd number of days the
# MIDDLE day is excluded too, so the halves stay equal-length and the discard
# stays symmetric — trimming an end instead would reintroduce exactly the bias
# this avoids. Fewer than two whole interior days means NO baseline at all: the
# view says "no comparable prior window" rather than printing a number the
# corpus cannot support.
#
# Time is a second marginal on the INDUSTRY axis ONLY. It is never crossed with
# the crew axis; crew-by-anything is what re-identifies, and the R4.1 note above
# still holds without exception.
#
# HISTORY EXISTS UPSTREAM — spiked with read-only GETs, 2026-08-16.
# ransomware.live v2 does expose historical listings, at
# GET /v2/victims/{year}/{month}: 2026/07 returned ~914 listings against the
# 100-record cap of /v2/recentvictims. See app/ingest/ransomware.py for what a
# backfill over that endpoint has to plan around — neither of its date fields is
# confined to the month requested.
# The stored corpus is still a single run of the RECENT endpoint, so the split
# below is written to work over a multi-month corpus and simply reports no
# comparable prior window until a backfill lands. Backfilling is the operator's
# call, not this read's.
BASELINE_MIN_INTERIOR_DAYS = 2


def _baseline_split(days):
    """Split covered days into equal prior/current halves (see the note above).

    ``days`` is the sorted list of distinct ISO days the corpus covers. Returns
    ``(prior, current, boundary, middle)`` as day lists. ``prior``/``current``
    come back empty when the corpus is too short for an honest comparison —
    that is a real answer, not a failure.
    """
    if not days:
        return [], [], [], []
    boundary = [days[0]] if len(days) == 1 else [days[0], days[-1]]
    interior = days[1:-1]
    if len(interior) < BASELINE_MIN_INTERIOR_DAYS:
        return [], [], boundary, []
    half = len(interior) // 2
    return (interior[:half], interior[len(interior) - half:], boundary,
            interior[half:len(interior) - half])


def _ransomware_baseline(day_total, day_peer):
    """Prior-vs-current counts over equal halves of the corpus, or an honest
    'unavailable' when the covered span is too short (R8.5, R6.6).

    Both n's are returned, never just the difference: a delta with no
    denominators is unreadable on a corpus this thin.

    ``subject_available`` is a SECOND, narrower floor for the watchlist row.
    The corpus-wide floor only asks whether two whole interior days exist; it
    says nothing about whether the watchlist's own industry appeared on any of
    them. On the live corpus it does not — the watchlist holds 2 listings in
    100 and both fall on excluded boundary days — so the subject would compare
    0 against 0 and the lede would render the word "no change". That is not a
    fabricated rise, but it is a fabricated FINDING: "no change" reads as a
    measured result when the denominator is zero on both sides. The subject
    delta is withheld unless at least one watchlist listing lands in one half.
    """
    days = sorted(day_total)
    prior, current, boundary, middle = _baseline_split(days)
    base = {
        "covered_days": len(days),
        "excluded_boundary": boundary,
        "excluded_middle": middle,
    }
    if not prior:
        base.update({
            "available": False, "subject_available": False, "half_days": 0,
            "prior_start": "", "prior_end": "",
            "current_start": "", "current_end": "",
            "prior_total": 0, "current_total": 0, "total_delta": None,
            "prior_peer": 0, "current_peer": 0, "peer_delta": None,
        })
        return base
    prior_total = sum(day_total[d] for d in prior)
    current_total = sum(day_total[d] for d in current)
    prior_peer = sum(day_peer.get(d, 0) for d in prior)
    current_peer = sum(day_peer.get(d, 0) for d in current)
    base.update({
        "available": True,
        "subject_available": (prior_peer + current_peer) > 0,
        "half_days": len(prior),
        "prior_start": prior[0], "prior_end": prior[-1],
        "current_start": current[0], "current_end": current[-1],
        "prior_total": prior_total, "current_total": current_total,
        "total_delta": current_total - prior_total,
        "prior_peer": prior_peer, "current_peer": current_peer,
        "peer_delta": current_peer - prior_peer,
    })
    return base


def ransomware_activity(conn, now=None):
    """Aggregate leak-site activity for the Explore Ransomware tab (R8.5, R4.1).

    Counts the stored ``ransomware_live`` raw_events two independent ways —
    crews by volume and industries by volume — over the window the data itself
    spans. Returns a plain dict::

        {'total', 'window_start', 'window_end', 'window_days',
         'source_name', 'source_url', 'last_success_at', 'source_state',
         'run_count',
         'crews': [{'label','count'}],        # >= CREW_MIN_VICTIMS only
         'crews_withheld', 'crews_withheld_listings', 'crew_total',
         'industries': [{'label','count','rank','is_peer'}],
         'peer_listings', 'peer_rank', 'industry_rows', 'unclassified',
         'baseline': {...}}

    ``industries`` leads with the watchlist's OWN industry rows, then falls back
    to descending volume. The panel's subject is the watchlist; world volume
    rank is context, and is carried per row as ``rank`` so it stays readable
    without being the ordering. ``baseline`` is the prior-window comparison
    described in the note above; ``run_count`` is how many ingest runs the
    corpus was assembled from, which is what tells the reader a one-run corpus
    is a rolling-feed snapshot rather than an accumulated series.

    ``now`` is injectable so the freshness classification is deterministic in
    tests (R10.2 UTC ISO-8601 throughout).

    Never returns a victim name, a domain, a URL, or a singleton crew name
    (R4.1 — see the module note above); never returns a score, an entity or a
    signal id (this is not a card surface). ``is_peer`` is computed with the
    classifier's OWN ``is_peer_industry`` predicate, so the row the panel
    highlights is exactly the row that mints peer cards and the two cannot
    drift. An empty/absent source yields zeroed counts and empty lists, which
    the view renders as an honest empty state rather than a broken panel
    (R6.6).
    """
    rows = conn.execute(
        "SELECT re.event_date, re.payload FROM raw_events re "
        "WHERE re.source_id = ? ORDER BY re.raw_event_id",
        (RANSOMWARE_SOURCE_ID,)).fetchall()

    # Freshness: the window below is derived from event_date alone, so a feed
    # that stopped updating a month ago still renders a confident-looking
    # report. Carry the source's own run state so the panel can say when it
    # last actually ingested — "quiet" must stay distinguishable from "broken"
    # (R6.6), and this is the one surface where it otherwise would not be.
    source_row = None
    for candidate in source_health(conn):
        if candidate["source_id"] == RANSOMWARE_SOURCE_ID:
            source_row = candidate
            break

    # How many ingest runs the corpus came from. One run of a rolling recent
    # feed is a snapshot, not a series, and the byline has to say so.
    run_count = conn.execute(
        "SELECT COUNT(*) FROM source_runs WHERE source_id = ?",
        (RANSOMWARE_SOURCE_ID,)).fetchone()[0]

    crew_counts = {}
    crew_victims = {}
    industry_counts = {}
    dates = []
    # Per-day tallies feed the baseline split only. Listings with no usable
    # event_date cannot be placed on the timeline, so they are counted in the
    # total but sit out the comparison rather than landing in an arbitrary half.
    day_total = {}
    day_peer = {}
    unclassified = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        event_date = (row["event_date"] or "")[:10]
        if event_date:
            dates.append(event_date)
            day_total[event_date] = day_total.get(event_date, 0) + 1

        crew = " ".join((payload.get("group") or "").split())
        if crew:
            crew_counts[crew] = crew_counts.get(crew, 0) + 1
            victim = " ".join((payload.get("victim") or "").split()).lower()
            if victim:
                crew_victims.setdefault(crew, set()).add(victim)

        activity = " ".join((payload.get("activity") or "").split())
        if _is_unclassified_activity(activity):
            unclassified += 1
        else:
            industry_counts[activity] = industry_counts.get(activity, 0) + 1
            # Same predicate as the row flag below, so the baseline compares
            # exactly the rows the panel highlights (an unknown industry is
            # never a peer, so it never enters this tally).
            if event_date and ransomware_classifier.is_peer_industry(activity):
                day_peer[event_date] = day_peer.get(event_date, 0) + 1

    # Descending by count, then label, so equal counts render deterministically.
    # The naming gate is distinct victims, not raw listings: upstream revisions
    # can create more than one raw_event for one real victim, and naming a
    # self-named crew in that case would still identify the company.
    named = [{"label": k, "count": n}
             for k, n in sorted(crew_counts.items(), key=lambda kv: (-kv[1], kv[0]))
             if len(crew_victims.get(k, set())) >= CREW_MIN_VICTIMS]
    withheld = [n for k, n in crew_counts.items()
                if len(crew_victims.get(k, set())) < CREW_MIN_VICTIMS]

    # SUBJECT FIRST (R8.5). Ranking the industries purely by volume buried the
    # watchlist's own row ninth on the real corpus, which made the panel answer
    # "who is busiest worldwide" — a question nobody opens this tab with. The
    # watchlist rows lead; volume order still governs within each group, and the
    # world rank each row would have had is carried as `rank` so the context is
    # kept rather than discarded.
    by_volume = sorted(industry_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    industries = [
        {"label": k, "count": n, "rank": i + 1,
         "is_peer": ransomware_classifier.is_peer_industry(k)}
        for i, (k, n) in enumerate(by_volume)
    ]
    industries.sort(key=lambda r: (not r["is_peer"], -r["count"], r["label"]))

    return {
        "total": len(rows),
        "run_count": run_count,
        "baseline": _ransomware_baseline(day_total, day_peer),
        "window_start": min(dates) if dates else "",
        "window_end": max(dates) if dates else "",
        "window_days": _window_days(dates),
        "source_name": (source_row["name"] if source_row
                        else RANSOMWARE_SOURCE_ID),
        "source_url": RANSOMWARE_SOURCE_URL,
        "last_success_at": (source_row["last_success_at"] if source_row
                            else None),
        "source_state": (source_state(source_row, now=now) if source_row
                         else "never_run"),
        "crews": named,
        "crew_total": len(crew_counts),
        "crews_withheld": len(withheld),
        "crews_withheld_listings": sum(withheld),
        "industries": industries,
        "peer_listings": sum(r["count"] for r in industries if r["is_peer"]),
        "peer_rank": min((r["rank"] for r in industries if r["is_peer"]),
                         default=0),
        "industry_rows": len(industries),
        "unclassified": unclassified,
    }


def _window_days(dates):
    """Inclusive day span covered by ``dates`` (ISO YYYY-MM-DD), 0 when empty.
    A single-day corpus spans 1 day, not 0 — the panel states a real window."""
    parsed = [d for d in (_parse_date(v) for v in dates) if d is not None]
    if not parsed:
        return 0
    return (max(parsed) - min(parsed)).days + 1
