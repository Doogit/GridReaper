"""Classification runner framework (R3.7, R4.1, R6.2, R6.4, R6.5, R7.1, R7.2,
R10.6).

A classifier is a function ``classify(conn, raw_event_row)`` returning a list
of candidate dicts:

  trigger_id        triggers.trigger_id; the candidate's signal_scope must be
                    in the trigger's allowed_scopes (R7.2) or it is dropped
  signal_scope      account / sector / regulatory_calendar / ...
  entity_id         pre-attributed watchlist entity (EDGAR payloads carry
                    one); None when the classifier only has a name
  entity_name_hint  name text for EntityResolver when entity_id is None
  event_date        UTC ISO date of the underlying event
  headline          one-line signal headline
  evidence          non-empty list of {"text": ..., "locator": ...} (R4.1:
                    nothing surfaces unsourced - no evidence, no signal)
  confidence        classifier confidence 0..1; a name-resolved signal stores
                    min(classifier confidence, resolution confidence)

Classifiers never write signals. The framework owns everything downstream:
entity resolution via EntityResolver with decisions logged (R6.4), review /
no-match candidates routed to review_queue and never fired (R6.2), parent
rollup so one event yields one card per top-level account (R6.5),
deterministic ``signal_id = "{trigger_id}:{raw_event_id}:{entity_id or
scope}"`` with insert-or-skip so re-runs emit nothing new (R3.7),
signal_evidence rows ranked from source_policies, and per-event bookkeeping
in classified_events. A classifier exception on one event rolls back that
event's writes and leaves it unprocessed for the next run; it never aborts
the run.

RETRACTION (R3.7). Insert-or-skip lets a re-run ADD what a corrected rule now
emits, but until now nothing could take back what the OLD rule emitted: a
stored signal could outlive the rule that minted it, so the store was not
reproducible from raw events plus current rules. A run therefore reconciles
in both directions. For the events it processed cleanly, any signal of its own
that it no longer emits is flipped to ``status='retracted'``; a retracted
signal the classifier emits again is restored to 'active'. This is what makes
a parser_version bump self-correcting - see app/classify/ransomware.py 1.0 ->
1.1, which invalidated 97 stored peer cards.

  scope   Only raw_events this run processed WITHOUT error are eligible. An
          errored event is rolled back before its classified_events row is
          written and never reaches events_processed, so a classifier that
          crashes retracts NOTHING - the failure mode that would otherwise
          empty the store. A --limit'ed run likewise reconciles only what it
          reached.
  owner   Only signals belonging to THIS classifier, matched on the
          ``signal_evidence.extraction_version`` prefix ("{name}/"). Necessary
          because one source can feed several classifiers -
          sec_edgar_submissions feeds both `incident` and `leadership` - and
          retracting by raw_event alone would take the other one's cards down.
  respect A 'dismissed' signal is never overwritten: that status is the
          operator's own judgment, and a rule change does not get to restate
          it. Every other status yields to retraction.

REFRESH (R3.7). Retraction covers what a corrected rule no longer emits. It
does NOT cover the other half: a card the rule STILL emits, only worded
differently. That card keeps its deterministic signal_id, so insert-or-skip
returned before both the INSERT and the guard below, and its stored text was
immutable - a wording fix reached only cards minted after it. So a run also
re-mints the TEXT of any card it emits whose stored version is not the version
the run mints at, rewriting headline, evidence_snippet and the signal_evidence
rows and nothing else. status, score, confidence and the incident tier are left
as stored: this restates how a card READS, never what was concluded about it. A
'dismissed' card is skipped for the same reason retraction skips it. See
_refresh_stored_text.

PROVENANCE (R10.6). "No PII beyond executive names in public filings/press. No
person-level enrichment." Until now that held by OMISSION - the classifiers
happen to quote or hand-write their text, and nothing checked. The guard below
asserts what is actually decidable: PROVENANCE, not name shape. Every word of a
candidate's headline and of each evidence text must come from the raw event it
cites or from the classifier's own source file. Source-quoted text is exactly
what R10.6 permits; a classifier literal is a string a reviewer can read in the
diff; anything else has no traceable origin, which IS person-level enrichment.

  not shape  A person-name regex is not a usable gate here: measured over the
             real store it matches every non-peer card ("Virtualization
             Reliability", "Critical Infrastructure", "Order No"), and a
             first-name gazetteer fares no better ("Order", "Mark", "Grant",
             "Bill" are live tokens in this domain).
  blast      A violation quarantines the ONE offending candidate to
             review_queue (raw_event_id logged) and mints no signal. It never
             raises: for a cron-driven single-operator tool a guard that can
             stop the pipeline is worse than the risk it prevents, so every
             other candidate in the same run still lands. A stored card is
             re-judged only when REFRESH above would rewrite its text, and a
             violation then leaves the stored text standing rather than
             replacing it with unprovenanced text.
  scope      headline too, not just evidence - it is written by the same INSERT,
             is rendered more prominently, and security_rss.py fills it with
             verbatim source text.

  incident_evidence_level  optional (R10.5): confirmed / corroborated /
                    unconfirmed_early_warning. When set, the candidate is an
                    incident: the framework stores the tier and derives
                    customer_facing_allowed per R7.12 -
                    unconfirmed_early_warning suppresses outreach (0), the
                    higher tiers allow it (1). A confirmed/corroborated card
                    sourced from a leak site must still suppress outreach - the
                    emitting classifier owns that by tiering it
                    unconfirmed_early_warning (R10.5), not the framework. When
                    omitted the candidate is a non-incident signal:
                    incident_evidence_level NULL, customer_facing_allowed 1.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone

from app.db.connection import get_connection
from app.ingest import runner as ingest_runner
from app.resolve import EntityResolver, enqueue_review, record_decision

COMMIT_EVERY = 200          # short transactions per R3.2

ACCOUNT_SCOPES = {"account", "parent"}

# R10.5 incident evidence tiers. Only unconfirmed_early_warning suppresses
# customer-facing outreach (R7.12); confirmed/corroborated allow it.
INCIDENT_TIERS = ("confirmed", "corroborated", "unconfirmed_early_warning")


# R10.6 provenance guard. Tokens shorter than this carry no identity, and
# matching them would only produce noise.
PROVENANCE_MIN_TOKEN = 3
PROVENANCE_REASON = "provenance_guard"
_PROVENANCE_TOKEN_RE = re.compile(r"[0-9a-z]+")


def customer_facing_for_tier(incident_level):
    """R7.12: only an unconfirmed early warning suppresses customer-facing
    outreach (0); every confirmed/corroborated tier (and a non-incident signal,
    incident_level None) allows it (1). Single source of this rule so the
    classifier and the R8.7 operator re-tier editor can never diverge."""
    return 0 if incident_level == "unconfirmed_early_warning" else 1


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _active_entity_exists(conn, entity_id):
    """True only for watchlist entities that can receive new account signals."""
    return conn.execute(
        "SELECT 1 FROM watchlist_entities "
        "WHERE entity_id = ? AND active = 1",
        (entity_id,)).fetchone() is not None


def top_level_entity(conn, entity_id):
    """R6.5: follow parent links (entity_relationships, then
    watchlist_entities.parent_id) to the top-level account. Cycle-guarded.
    Soft-disabled parents (R8.7) are not valid rollup targets because disabling
    an entity must prevent any future signal from being minted for it."""
    seen = set()
    current = entity_id
    while current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT parent_entity_id FROM entity_relationships "
            "WHERE child_entity_id = ? ORDER BY parent_entity_id LIMIT 1",
            (current,)).fetchone()
        parent = row["parent_entity_id"] if row else None
        if not parent:
            row = conn.execute(
                "SELECT parent_id FROM watchlist_entities WHERE entity_id = ?",
                (current,)).fetchone()
            parent = (row["parent_id"] or "").strip() if row else ""
            if parent and not _active_entity_exists(conn, parent):
                parent = ""
        elif not _active_entity_exists(conn, parent):
            parent = ""
        if not parent:
            return current
        current = parent
    return current


def _provenance_tokens(text):
    """Lowercased alphanumeric tokens of at least PROVENANCE_MIN_TOKEN chars.
    Splitting on every non-alphanumeric character makes the comparison immune to
    escaping and punctuation drift - a payload's "CIP 013" covers a card's
    "CIP-013", and a JSON-escaped "Company\\u2019s" still yields "company"."""
    return {t for t in _PROVENANCE_TOKEN_RE.findall((text or "").lower())
            if len(t) >= PROVENANCE_MIN_TOKEN}


def authored_tokens(classify_fn):
    """Tokens of the source file that defines ``classify_fn`` - the literals its
    author wrote and a reviewer can read in the diff.

    Returns None when the file cannot be read, which DISABLES the guard for that
    run: a guard that cannot load its own baseline must not quarantine every
    card the run produces."""
    module = sys.modules.get(getattr(classify_fn, "__module__", "") or "")
    path = getattr(module, "__file__", None)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return _provenance_tokens(fh.read())
    except OSError:
        return None


def provenance_vocabulary(raw, authored):
    """Everything a candidate off this raw event is allowed to say: the event it
    cites plus the classifier's own literals. None disables the guard."""
    if authored is None:
        return None
    return (authored | _provenance_tokens(raw["payload"])
            | _provenance_tokens(raw["url"]))


def novel_tokens(text, vocabulary):
    """Tokens of ``text`` that the vocabulary does not account for.

    The FINAL token may instead be a prefix of a vocabulary token: classifiers
    cap long text (the headline cap, the page-diff snippet cap) and a word cut
    in half is still sourced text, not invented text. Without this, every
    truncated regulatory headline would quarantine itself."""
    found = _PROVENANCE_TOKEN_RE.findall((text or "").lower())
    last = len(found) - 1
    novel = set()
    for i, token in enumerate(found):
        if len(token) < PROVENANCE_MIN_TOKEN or token in vocabulary:
            continue
        if i == last and any(v.startswith(token) for v in vocabulary):
            continue
        novel.add(token)
    return sorted(novel)


def unprovenanced_text(cand, evidence, vocabulary):
    """(field, sorted novel tokens) for the first headline/evidence text that
    carries words found in neither the raw event nor the classifier source;
    None when the candidate is fully provenanced (R10.6)."""
    if vocabulary is None:
        return None
    texts = [("headline", cand.get("headline") or "")]
    texts += [(f"evidence[{i}]", e["text"]) for i, e in enumerate(evidence)]
    for field, text in texts:
        novel = novel_tokens(text, vocabulary)
        if novel:
            return field, novel
    return None


def _quarantine(conn, raw, field, novel, confidence):
    """Route ONE unprovenanced candidate to the review queue instead of minting
    it (R10.6). Never raises and never touches the rest of the run. The queue
    row carries the raw_event_id, so the operator can read the source and decide
    whether the classifier or the text is wrong."""
    reason = (f"{PROVENANCE_REASON}: {field} carries text found in neither the "
              f"raw event nor the classifier source ("
              f"{', '.join(novel[:5])})")
    if conn.execute(
            "SELECT 1 FROM review_queue WHERE raw_event_id = ? "
            "AND disposition = 'pending' AND reason = ?",
            (raw["raw_event_id"], reason)).fetchone() is None:
        conn.execute(
            "INSERT INTO review_queue (raw_event_id, candidate_entity_id, "
            " reason, confidence, created_at, disposition) "
            "VALUES (?, NULL, ?, ?, ?, 'pending')",
            (raw["raw_event_id"], reason, confidence, _utcnow()))


def _triggers_config(conn):
    """trigger_id -> {'allowed_scopes': set, 'evidence_quality': str}."""
    cfg = {}
    for r in conn.execute(
            "SELECT trigger_id, allowed_scopes, evidence_quality FROM triggers"):
        try:
            scopes = set(json.loads(r["allowed_scopes"] or "[]"))
        except ValueError:
            scopes = set()
        cfg[r["trigger_id"]] = {"allowed_scopes": scopes,
                                "evidence_quality": r["evidence_quality"]}
    return cfg


def _process_candidate(conn, resolver, raw, cand, evidence_rank,
                       triggers_cfg, parser_version, counts, vocabulary=None):
    """Resolve, roll up, and insert-or-skip one candidate. Returns the
    signal_id when the candidate mapped to a signal (new or already existing),
    else None. The caller collects those ids: a signal this run did NOT emit is
    what retraction acts on, so "emitted" must include an already-existing row,
    not just a fresh insert."""
    trig = triggers_cfg.get(cand.get("trigger_id"))
    if trig is None or cand.get("signal_scope") not in trig["allowed_scopes"]:
        counts["dropped_scope"] += 1
        return None
    evidence = [e for e in (cand.get("evidence") or [])
                if (e.get("text") or "").strip()]
    if not evidence:
        counts["dropped_no_evidence"] += 1
        return None

    confidence = float(cand.get("confidence") or 0.0)
    entity_id = None
    if cand["signal_scope"] in ACCOUNT_SCOPES:
        entity_id = (cand.get("entity_id") or "").strip() or None
        if entity_id is None:
            hint = (cand.get("entity_name_hint") or "").strip()
            if not hint:
                counts["dropped_no_entity"] += 1
                return None
            context = " ".join(
                [cand.get("headline") or ""] + [e["text"] for e in evidence])
            res = resolver.resolve(name=hint, context_text=context)
            if res.status == "matched":
                record_decision(conn, raw["raw_event_id"], res,
                                decided_by="classification")
                entity_id = res.entity_id
                confidence = min(confidence, res.confidence)
            elif res.status == "review":
                # R6.2: below-threshold/ambiguous MUST NOT auto-fire
                enqueue_review(conn, raw["raw_event_id"], res)
                counts["review_enqueued"] += 1
                return None
            else:
                counts["dropped_no_entity"] += 1
                return None
        elif not _active_entity_exists(conn, entity_id):
            counts["dropped_no_entity"] += 1
            return None
        entity_id = top_level_entity(conn, entity_id)

    incident_level = cand.get("incident_evidence_level")
    if incident_level is not None and incident_level not in INCIDENT_TIERS:
        raise ValueError(
            f"unknown incident_evidence_level {incident_level!r}")
    # R7.12: only unconfirmed early warning suppresses outreach.
    customer_facing = customer_facing_for_tier(incident_level)

    signal_id = (f"{cand['trigger_id']}:{raw['raw_event_id']}:"
                 f"{entity_id or cand['signal_scope']}")
    stored = conn.execute("SELECT status FROM signals WHERE signal_id = ?",
                          (signal_id,)).fetchone()
    if stored:
        counts["signals_existing"] += 1
        _refresh_stored_text(conn, raw, cand, evidence, evidence_rank,
                             parser_version, vocabulary, counts, signal_id,
                             stored["status"], confidence)
        return signal_id

    # R10.6: past this point the candidate becomes a stored, rendered card.
    unprovenanced = unprovenanced_text(cand, evidence, vocabulary)
    if unprovenanced is not None:
        _quarantine(conn, raw, unprovenanced[0], unprovenanced[1], confidence)
        counts["quarantined"] += 1
        return None

    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, event_date, headline, evidence_snippet, "
        " source_url, confidence, evidence_quality, incident_evidence_level, "
        " customer_facing_allowed, score, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active')",
        (signal_id, raw["raw_event_id"], entity_id, cand["signal_scope"],
         cand["trigger_id"], cand.get("event_date") or "",
         cand.get("headline") or "", evidence[0]["text"],
         raw["url"] or "", round(confidence, 4), trig["evidence_quality"],
         incident_level, customer_facing))
    _write_evidence(conn, signal_id, raw, evidence, evidence_rank,
                    parser_version)
    counts["signals_new"] += 1
    return signal_id


def _write_evidence(conn, signal_id, raw, evidence, evidence_rank,
                    parser_version):
    """Write one signal's evidence rows, stamped with the version that minted
    them. Shared by the insert and the refresh path so the two cannot drift."""
    for e in evidence:
        conn.execute(
            "INSERT INTO signal_evidence (signal_id, raw_event_id, "
            " evidence_text, evidence_locator, evidence_rank, "
            " extraction_version) VALUES (?, ?, ?, ?, ?, ?)",
            (signal_id, raw["raw_event_id"], e["text"],
             e.get("locator", "") or "", evidence_rank, parser_version))


def _refresh_stored_text(conn, raw, cand, evidence, evidence_rank,
                         parser_version, vocabulary, counts, signal_id,
                         status, confidence):
    """Re-mint the TEXT of a card an OLDER parser_version stored (R3.7, R10.6).

    Insert-or-skip made stored card text immutable. A corrected rule
    reprocessed the event, rebuilt the same deterministic signal_id, and
    returned before both the INSERT and the R10.6 guard - so a wording fix
    reached only cards minted after it, and the guard never judged a card
    minted before the guard existed. Retraction did not cover this: it acts on
    signals the run NO LONGER emits, and a reworded card is still emitted.

    Rewrites headline, evidence_snippet and the signal_evidence rows, and
    NOTHING else. status, score, confidence, entity_id and the incident tier
    are left exactly as stored: this restates how the card reads, never what
    the pipeline or the operator concluded about it.

    stale    Staleness is version INEQUALITY, not ordering: the run mints at
             the current version, so any other stored version is by definition
             not it. Comparing with '<' would call "1.10" older than "1.9".
    respect  A 'dismissed' card is never touched - same rule as retraction: the
             operator judged that text, and a rule change does not get to
             restate what they judged. 'retracted' and 'superseded' ARE
             refreshed; both stay rendered (a retracted one is about to be
             restored by this very run, since the run emitted it).
    guard    R10.6 runs here, on text that is about to be stored - the point
             the insert path guards. A violation quarantines and leaves the
             STORED text standing: the old wording is worse than the new, but
             unprovenanced text is worse than both.
    visible  A refresh closes the classifier_parser_version/extraction_version
             drift that signal_provenance reports (R3.7). A skip or a
             quarantine leaves it open, which is the honest reading: that
             card's text is not what the current rule would write.
    """
    if status == "dismissed":
        return
    stored_versions = {r[0] for r in conn.execute(
        "SELECT DISTINCT extraction_version FROM signal_evidence "
        "WHERE signal_id = ?", (signal_id,))}
    if stored_versions == {parser_version}:
        return

    unprovenanced = unprovenanced_text(cand, evidence, vocabulary)
    if unprovenanced is not None:
        _quarantine(conn, raw, unprovenanced[0], unprovenanced[1], confidence)
        counts["quarantined"] += 1
        return

    conn.execute(
        "UPDATE signals SET headline = ?, evidence_snippet = ? "
        "WHERE signal_id = ?",
        (cand.get("headline") or "", evidence[0]["text"], signal_id))
    conn.execute("DELETE FROM signal_evidence WHERE signal_id = ?",
                 (signal_id,))
    _write_evidence(conn, signal_id, raw, evidence, evidence_rank,
                    parser_version)
    counts["signals_refreshed"] += 1


def _classifier_prefix(parser_version):
    """The "{name}/" owner prefix stored in signal_evidence.extraction_version.
    Compared with substr(), not LIKE, because several classifier names contain
    '_' - a LIKE wildcard - and 'incident_security_rss/%' would then match more
    than itself."""
    return parser_version.split("/", 1)[0] + "/"


def _reconcile_retractions(conn, parser_version, processed_ids, emitted_ids):
    """Reconcile stored signals against what this run actually emitted (R3.7).

    Retracts a signal of this classifier's that the run no longer emits, and
    restores one it emits again. Both directions matter: without the restore, a
    signal retracted by one rule change would stay invisible forever even after
    a later change re-supports it. Returns {'retracted': n, 'restored': n}.

    Scoped to ``processed_ids`` (events this run classified WITHOUT error) so a
    crashing classifier retracts nothing, and to this classifier's own signals
    so a co-tenant classifier on the same source is untouched. A 'dismissed'
    signal is left alone - that is the operator's judgment, not the rule's.
    """
    if not processed_ids:
        return {"retracted": 0, "restored": 0}
    prefix = _classifier_prefix(parser_version)
    owned = conn.execute(
        "SELECT DISTINCT s.signal_id, s.raw_event_id, s.status "
        "FROM signals s "
        "JOIN signal_evidence e ON e.signal_id = s.signal_id "
        "WHERE substr(e.extraction_version, 1, ?) = ?",
        (len(prefix), prefix)).fetchall()

    retract, restore = [], []
    for row in owned:
        if row["raw_event_id"] not in processed_ids:
            continue                      # not re-evaluated by this run
        if row["signal_id"] in emitted_ids:
            if row["status"] == "retracted":
                restore.append(row["signal_id"])
        elif row["status"] not in ("retracted", "dismissed"):
            retract.append(row["signal_id"])

    for signal_id in retract:
        conn.execute("UPDATE signals SET status = 'retracted' "
                     "WHERE signal_id = ?", (signal_id,))
    for signal_id in restore:
        # Back to 'active'; its stored score is stale until the next scoring
        # pass, exactly as for any newly emitted signal.
        conn.execute("UPDATE signals SET status = 'active' "
                     "WHERE signal_id = ?", (signal_id,))
    return {"retracted": len(retract), "restored": len(restore)}


def run_classifier(conn, classifier_id, source_id, classify_fn,
                   parser_version, force=False, limit=None):
    """Run one classifier over the unprocessed raw_events of source_id;
    returns a summary dict. force=True reprocesses events already bookkept
    at this parser_version (deterministic ids make that emit nothing new).
    An unknown source_id is a configuration error and raises."""
    rank_row = conn.execute(
        "SELECT evidence_rank FROM source_policies WHERE source_id = ?",
        (source_id,)).fetchone()
    if rank_row is None:
        raise ValueError(
            f"Unknown source_id {source_id!r}: not in source_policies.")
    evidence_rank = rank_row["evidence_rank"]

    resolver = EntityResolver(conn)
    triggers_cfg = _triggers_config(conn)

    sql = "SELECT * FROM raw_events WHERE source_id = ?"
    if not force:
        sql += (" AND NOT EXISTS (SELECT 1 FROM classified_events c"
                "  WHERE c.raw_event_id = raw_events.raw_event_id"
                "  AND c.classifier_id = ? AND c.parser_version = ?)")
    sql += " ORDER BY first_seen_at, raw_event_id"
    params = (source_id,) if force else (source_id, classifier_id,
                                         parser_version)
    rows = conn.execute(sql, params).fetchall()

    counts = {"events_processed": 0, "events_errored": 0, "signals_new": 0,
              "signals_existing": 0, "signals_refreshed": 0,
              "review_enqueued": 0,
              "dropped_scope": 0, "dropped_no_evidence": 0,
              "dropped_no_entity": 0, "quarantined": 0,
              "signals_retracted": 0, "signals_restored": 0}
    authored = authored_tokens(classify_fn)     # R10.6 baseline, once per run
    last_error = ""
    # Retraction inputs: only cleanly-processed events, and every signal_id
    # this run stands behind (freshly inserted OR already present).
    processed_ids, emitted_ids = set(), set()
    for i, raw in enumerate(rows):
        if limit is not None and i >= limit:
            break
        conn.execute("SAVEPOINT classify_event")
        try:
            reviews_before = conn.execute(
                "SELECT COUNT(*) FROM review_queue "
                "WHERE raw_event_id = ? AND disposition = 'pending'",
                (raw["raw_event_id"],)).fetchone()[0]
            candidates = classify_fn(conn, raw) or []
            reviews_after_classify = conn.execute(
                "SELECT COUNT(*) FROM review_queue "
                "WHERE raw_event_id = ? AND disposition = 'pending'",
                (raw["raw_event_id"],)).fetchone()[0]
            classifier_queued_review = reviews_after_classify > reviews_before
            emitted = 0
            event_emitted = []
            # Built once per event with candidates, shared by all of them.
            vocabulary = (provenance_vocabulary(raw, authored)
                          if candidates else None)
            for cand in candidates:
                signal_id = _process_candidate(
                    conn, resolver, raw, cand, evidence_rank, triggers_cfg,
                    parser_version, counts, vocabulary)
                if signal_id:
                    event_emitted.append(signal_id)
                    emitted += 1
            if classifier_queued_review:
                counts["review_enqueued"] += 1
            conn.execute(
                "INSERT INTO classified_events (raw_event_id, classifier_id, "
                " parser_version, processed_at, signals_emitted) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(raw_event_id, classifier_id, parser_version) "
                "DO UPDATE SET processed_at = excluded.processed_at, "
                " signals_emitted = excluded.signals_emitted",
                (raw["raw_event_id"], classifier_id, parser_version,
                 _utcnow(), emitted))
            conn.execute("RELEASE classify_event")
        except Exception as exc:
            # contain per event: roll back its writes, leave it unprocessed
            conn.execute("ROLLBACK TO classify_event")
            conn.execute("RELEASE classify_event")
            counts["events_errored"] += 1
            last_error = f"{raw['raw_event_id']}: {type(exc).__name__}: {exc}"
            continue
        counts["events_processed"] += 1
        # Recorded only past the except: an errored event is rolled back and
        # must never make its signals eligible for retraction.
        processed_ids.add(raw["raw_event_id"])
        emitted_ids.update(event_emitted)
        if counts["events_processed"] % COMMIT_EVERY == 0:
            conn.commit()
    conn.commit()

    reconciled = _reconcile_retractions(
        conn, parser_version, processed_ids, emitted_ids)
    counts["signals_retracted"] = reconciled["retracted"]
    counts["signals_restored"] = reconciled["restored"]
    conn.commit()

    counts.update({
        "classifier_id": classifier_id, "source_id": source_id,
        "parser_version": parser_version,
        "status": "error" if counts["events_errored"] else "success",
        "last_error": last_error})
    return counts


def cli(classifier_id, sources, parser_version, description):
    """Shared __main__ for classifier modules. ``sources`` maps source_id ->
    classify function; runs all (or --source one) sequentially under the
    single-writer ingestion lock (R3.2). Returns an exit code (1 when any
    source had event errors)."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--source", choices=sorted(sources), default=None,
                        help="classify one source (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after N events per source (debugging)")
    parser.add_argument("--force", action="store_true",
                        help="reprocess events already classified at this "
                             "parser_version")
    args = parser.parse_args()
    run_sources = [args.source] if args.source else sorted(sources)

    exit_code = 0
    with ingest_runner.ingest_lock():
        conn = get_connection()
        try:
            for source_id in run_sources:
                s = run_classifier(conn, classifier_id, source_id,
                                   sources[source_id], parser_version,
                                   force=args.force, limit=args.limit)
                line = (f"{classifier_id}/{source_id}: {s['status']} "
                        f"processed={s['events_processed']} "
                        f"signals_new={s['signals_new']} "
                        f"signals_existing={s['signals_existing']} "
                        f"signals_refreshed={s['signals_refreshed']} "
                        f"review={s['review_enqueued']} "
                        f"dropped_scope={s['dropped_scope']} "
                        f"dropped_no_evidence={s['dropped_no_evidence']} "
                        f"dropped_no_entity={s['dropped_no_entity']} "
                        f"quarantined={s['quarantined']} "
                        f"retracted={s['signals_retracted']} "
                        f"restored={s['signals_restored']} "
                        f"errors={s['events_errored']}")
                if s["last_error"]:
                    line += f" last_error={s['last_error']}"
                print(line)
                if s["events_errored"]:
                    exit_code = 1
        finally:
            conn.close()
    return exit_code
