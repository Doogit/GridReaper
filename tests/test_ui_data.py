"""UI data-access layer tests (R8.1-R8.3, R9.1, R4.3, R7.6, R10.7).

Hermetic: real migrations (including 0005) against in-memory SQLite, FK on,
fixture rows. Covers feed scope grouping + keyset pagination + status filter,
signal_detail (evidence + snapshots + fact provenance with no price_note),
account_signals, review_pending (pending-only + payload snippet), source_health
+ source_state classification, stale_facts, account_header, badge_legend, and
the two writers (feedback validation, atomic triage decision).
"""
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.db.migrate import apply_migrations
from app.ui import data

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def days_ago_date(n):
    return (NOW.date() - timedelta(days=n)).isoformat()


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)

    # triggers
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_reg','Regulatory',5,600)")

    # entities: parent, child, dark
    ents = [
        ("E_ACME", "Acme Energy", "iou_electric", None, "high", "edgar-visible",
         "possible", "commercial"),
        ("E_SUB", "Acme Grid Sub", "iou_electric", "E_ACME", "medium", "dark",
         "unknown", "unknown"),
        ("E_DARK", "Dark Muni Co", "muni_public", None, "low", "dark",
         "likely", "gcc_high"),
    ]
    for eid, name, sub, parent, rich, cov, gov, tenant in ents:
        conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector, "
            "parent_id, richness, coverage_flag, gov_cloud_likelihood, "
            "tenant_cloud_environment) VALUES (?,?,?,?,?,?,?,?)",
            (eid, name, sub, parent, rich, cov, gov, tenant))
    conn.execute("INSERT INTO entity_relationships (parent_entity_id, "
                 "child_entity_id, relationship_type) VALUES "
                 "('E_ACME','E_SUB','subsidiary')")

    # product + play + facts (one primary, one non-primary, one unverified)
    conn.execute("INSERT INTO products (product_id, name) VALUES "
                 "('p_sentinel','Microsoft Sentinel')")
    conn.execute(
        "INSERT INTO license_play_candidates (play_id, trigger_id, product_id, "
        "recommended_path, discovery_question) VALUES "
        "('play1','t_lead','p_sentinel','Adopt E5 grant','What is your SIEM?')")
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, price_note, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_primary','p_sentinel','commercial','$2/GB','primary',"
        "'http://primary', ?)", (days_ago_date(30),))
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, price_note, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_nonprimary','p_sentinel','commercial','$9/GB rumored',"
        "'non-primary','http://blog', ?)", (days_ago_date(400),))
    conn.execute(
        "INSERT INTO license_facts (fact_id, product_id, segment, price_note, "
        "source_quality, source_url, verified_date) VALUES "
        "('f_unknown','p_sentinel','gcc_high','',"
        "'non-primary','http://na', '')")

    # source policies + runs for source_health
    for sid, name, enabled, ttl in [
            ("sp_ok", "OK Source", 1, 3600), ("sp_err", "Err Source", 1, 3600),
            ("sp_never", "Never Source", 1, 3600),
            ("sp_stale", "Stale Source", 1, 3600),
            ("sp_disabled", "Disabled Source", 0, 3600)]:
        conn.execute(
            "INSERT INTO source_policies (source_id, name, enabled, ttl, "
            "access_method, evidence_rank) VALUES (?,?,?,?, 'rss', 2)",
            (sid, name, enabled, ttl))
    runs = [
        ("r_ok", "sp_ok", iso(NOW - timedelta(minutes=10)), "success", ""),
        ("r_err", "sp_err", iso(NOW - timedelta(minutes=5)), "error",
         "HTTP 503 from upstream"),
        ("r_stale", "sp_stale", iso(NOW - timedelta(days=2)), "success", ""),
    ]
    for rid, sid, started, status, err in runs:
        conn.execute(
            "INSERT INTO source_runs (run_id, source_id, started_at, "
            "finished_at, status, error_state) VALUES (?,?,?,?,?,?)",
            (rid, sid, started, started, status, err))

    # raw events (FK target for signals + review_queue)
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url) VALUES ('re_acc','sp_ok', ?, "
        "'{\"title\": \"Acme names new CISO\"}', 'http://acme/8k')",
        (days_ago_date(5),))
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url) VALUES ('re_rev','sp_ok', ?, "
        "'{\"title\": \"Ambiguous Utility Co filing\"}', 'http://rev/doc')",
        (days_ago_date(3),))

    # signals: 3 account (distinct dates for keyset), 1 sector, 1 regulatory,
    # 1 decayed account (status filter)
    add_signal(conn, "S_ACC1", "E_ACME", "account", "t_lead",
               days_ago_date(5), "Acme names new CISO", cfa=1)
    add_signal(conn, "S_ACC2", "E_ACME", "account", "t_lead",
               days_ago_date(10), "Acme CFO departs", cfa=1)
    add_signal(conn, "S_ACC3", "E_SUB", "account", "t_lead",
               days_ago_date(15), "Sub unit reorg", cfa=1)
    add_signal(conn, "S_SEC", None, "sector", "t_reg",
               days_ago_date(2), "FERC final rule: CIP revision", cfa=1)
    add_signal(conn, "S_REG", None, "regulatory_calendar", "t_reg",
               days_ago_date(1), "FERC proposed rule: virtualization", cfa=1)
    add_signal(conn, "S_DEAD", "E_ACME", "account", "t_lead",
               days_ago_date(400), "Old exec note", cfa=1, status="decayed")

    # evidence + snapshot for S_ACC1
    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        "evidence_locator, evidence_rank) VALUES ('S_ACC1','re_acc',"
        "'Acme Energy appointed a new Chief Information Security Officer.',"
        "'para 2', 1)")
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC1','play1', "
        "'[\"f_primary\",\"f_nonprimary\"]', ?, 'plays/1.0', "
        "'Recommended path: Adopt E5 grant', "
        "'Given the recent development, a licensing check may be timely.')",
        (iso(NOW),))

    # review queue: one pending, one already disposed (must not surface)
    conn.execute(
        "INSERT INTO review_queue (raw_event_id, candidate_entity_id, reason, "
        "confidence, created_at, disposition) VALUES "
        "('re_rev','E_DARK','fuzzy_below_threshold', 0.82, ?, 'pending')",
        (iso(NOW),))
    conn.execute(
        "INSERT INTO review_queue (raw_event_id, candidate_entity_id, reason, "
        "confidence, created_at, disposition) VALUES "
        "('re_rev','E_ACME','fuzzy_below_threshold', 0.80, ?, 'accepted')",
        (iso(NOW),))

    # badge legend
    conn.executemany(
        "INSERT INTO badge_legend (badge_kind, code, label, description) "
        "VALUES (?,?,?,?)",
        [("evidence_quality", "IR", "Investor Report", "From an investor filing"),
         ("source_quality", "non-primary", "Non-primary",
          "Not an authoritative source (R4.3)")])
    conn.commit()
    return conn


def add_signal(conn, sid, entity_id, scope, trigger_id, event_date, headline,
               cfa=0, status="active", score=None):
    raw = "re_acc" if scope in ("account", "parent") else None
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, customer_facing_allowed, score, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, raw, entity_id, scope, trigger_id, event_date, headline,
         headline, "http://src", 0.9, "IR", cfa, score, status))


class TestFeed(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_account_group_excludes_sector(self):
        rows = data.feed_page(self.conn, scope_group="account")
        ids = [r["signal_id"] for r in rows]
        self.assertEqual(ids, ["S_ACC1", "S_ACC2", "S_ACC3"])  # newest first
        self.assertNotIn("S_SEC", ids)

    def test_sector_group_includes_regulatory(self):
        rows = data.feed_page(self.conn, scope_group="sector")
        ids = {r["signal_id"] for r in rows}
        self.assertEqual(ids, {"S_SEC", "S_REG"})

    def test_status_filter_default_active_hides_decayed(self):
        ids = [r["signal_id"] for r in data.feed_page(self.conn, "account")]
        self.assertNotIn("S_DEAD", ids)

    def test_status_filter_can_widen(self):
        ids = [r["signal_id"] for r in data.feed_page(
            self.conn, "account", statuses=("active", "decayed"))]
        self.assertIn("S_DEAD", ids)

    def test_keyset_pagination(self):
        first = data.feed_page(self.conn, "account", limit=2)
        self.assertEqual([r["signal_id"] for r in first], ["S_ACC1", "S_ACC2"])
        last = first[-1]
        nxt = data.feed_page(self.conn, "account", limit=2,
                             after_key=(last["event_date"], last["signal_id"]))
        self.assertEqual([r["signal_id"] for r in nxt], ["S_ACC3"])

    def test_unknown_scope_group_raises(self):
        with self.assertRaises(ValueError):
            data.feed_page(self.conn, scope_group="bogus")

    def test_feed_row_carries_score_components(self):
        row = data.feed_page(self.conn, "account")[0]
        for col in ("score_base", "score_decay", "score_account_fit",
                    "score_scope_fit", "scored_at", "base_strength",
                    "entity_name", "coverage_flag"):
            self.assertIn(col, row.keys())


class TestSignalDetail(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_detail_bundles_evidence_and_snapshots(self):
        d = data.signal_detail(self.conn, "S_ACC1")
        self.assertEqual(d["signal"]["signal_id"], "S_ACC1")
        self.assertEqual(len(d["evidence"]), 1)
        self.assertEqual(len(d["snapshots"]), 1)
        snap = d["snapshots"][0]
        self.assertEqual(snap["product_name"], "Microsoft Sentinel")
        self.assertEqual(len(snap["facts"]), 2)

    def test_fact_provenance_never_exposes_price_note(self):
        """R4.3/R7.11: a non-primary price can never reach the DOM - the data
        layer does not even return price_note on fact rows."""
        d = data.signal_detail(self.conn, "S_ACC1")
        for fact in d["snapshots"][0]["facts"]:
            self.assertNotIn("price_note", fact.keys())
            self.assertIn("source_quality", fact.keys())

    def test_snapshot_and_facts_immune_to_license_fact_mutation(self):
        """R7.6 + R4.3: mutating a referenced license_fact (price + verified
        date) after the snapshot exists changes NOTHING the card reads - the
        snapshot text is pinned and the fact projection omits price_note, so a
        leaked price cannot reach the DOM by construction (persona: maintainer
        + Renn)."""
        before = data.signal_detail(self.conn, "S_ACC1")
        self.conn.execute(
            "UPDATE license_facts SET price_note = 'LEAKED-9999', "
            "verified_date = '1999-01-01' WHERE fact_id = 'f_nonprimary'")
        self.conn.commit()
        after = data.signal_detail(self.conn, "S_ACC1")
        # pinned snapshot text unchanged
        self.assertEqual(before["snapshots"][0]["display_text"],
                         after["snapshots"][0]["display_text"])
        self.assertEqual(before["snapshots"][0]["outreach_safe_text"],
                         after["snapshots"][0]["outreach_safe_text"])
        # leaked price appears nowhere in the returned structure
        for fact in after["snapshots"][0]["facts"]:
            self.assertNotIn("LEAKED-9999", tuple(fact))
            self.assertNotIn("price_note", fact.keys())

    def test_unknown_signal_returns_none(self):
        self.assertIsNone(data.signal_detail(self.conn, "nope"))

    def test_account_signals_for_entity(self):
        rows = data.account_signals(self.conn, "E_ACME")
        self.assertEqual([r["signal_id"] for r in rows], ["S_ACC1", "S_ACC2"])


class TestReviewAndSources(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_review_pending_only_pending_with_snippet(self):
        rows = data.review_pending(self.conn)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["candidate_entity_id"], "E_DARK")
        self.assertEqual(r["candidate_name"], "Dark Muni Co")
        self.assertEqual(r["reason"], "fuzzy_below_threshold")
        self.assertEqual(r["snippet"], "Ambiguous Utility Co filing")
        self.assertEqual(r["source_url"], "http://rev/doc")

    def test_source_state_classification(self):
        by_id = {r["source_id"]: r for r in data.source_health(self.conn)}
        self.assertEqual(data.source_state(by_id["sp_ok"], NOW), "ok")
        self.assertEqual(data.source_state(by_id["sp_err"], NOW), "error")
        self.assertEqual(data.source_state(by_id["sp_never"], NOW), "never_run")
        self.assertEqual(data.source_state(by_id["sp_stale"], NOW), "stale")
        self.assertEqual(data.source_state(by_id["sp_disabled"], NOW), "disabled")

    def test_source_health_surfaces_error_state(self):
        by_id = {r["source_id"]: r for r in data.source_health(self.conn)}
        self.assertEqual(by_id["sp_err"]["last_error_state"],
                         "HTTP 503 from upstream")

    def test_stale_facts_older_than_window(self):
        rows = data.stale_facts(self.conn, days=180, now=NOW)
        ids = [r["fact_id"] for r in rows]
        self.assertIn("f_nonprimary", ids)   # 400d old
        self.assertIn("f_unknown", ids)       # unverified -> stale
        self.assertNotIn("f_primary", ids)    # 30d old -> fresh
        # oldest-first, unverified (age None) sorts last
        self.assertEqual(ids[0], "f_nonprimary")
        self.assertIsNone(rows[-1]["age_days"])


class TestAccountAndLegend(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_account_header_parent_children_counts(self):
        h = data.account_header(self.conn, "E_ACME")
        self.assertEqual(h["entity"]["name"], "Acme Energy")
        self.assertIsNone(h["parent"])
        self.assertEqual([c["entity_id"] for c in h["children"]], ["E_SUB"])
        self.assertEqual(h["signal_counts"].get("active"), 2)
        self.assertEqual(h["signal_counts"].get("decayed"), 1)
        self.assertEqual(h["total_signals"], 3)

    def test_account_header_child_sees_parent(self):
        h = data.account_header(self.conn, "E_SUB")
        self.assertEqual(h["parent"]["entity_id"], "E_ACME")

    def test_account_header_unknown_entity(self):
        self.assertIsNone(data.account_header(self.conn, "nope"))

    def test_badge_legend_shape(self):
        legend = data.badge_legend(self.conn)
        self.assertEqual(
            legend["source_quality"]["non-primary"]["label"], "Non-primary")


class TestWrites(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def _feedback_rows(self):
        return self.conn.execute(
            "SELECT signal_id, verdict, reason_code, note FROM feedback").fetchall()

    def test_record_useful(self):
        data.record_feedback(self.conn, "S_ACC1", "useful", now=NOW)
        rows = self._feedback_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "useful")
        self.assertIsNone(rows[0]["reason_code"])

    def test_not_useful_requires_reason(self):
        with self.assertRaises(ValueError):
            data.record_feedback(self.conn, "S_ACC1", "not_useful", now=NOW)
        self.assertEqual(len(self._feedback_rows()), 0)

    def test_not_useful_with_reason_ok(self):
        data.record_feedback(self.conn, "S_ACC1", "not_useful",
                             reason_code="wrong_entity", note="not them", now=NOW)
        r = self._feedback_rows()[0]
        self.assertEqual(r["reason_code"], "wrong_entity")
        self.assertEqual(r["note"], "not them")

    def test_invalid_verdict_and_reason_raise(self):
        with self.assertRaises(ValueError):
            data.record_feedback(self.conn, "S_ACC1", "bogus", now=NOW)
        with self.assertRaises(ValueError):
            data.record_feedback(self.conn, "S_ACC1", "useful",
                                 reason_code="nonsense", now=NOW)

    def test_converted_verdict_ok(self):
        data.record_feedback(self.conn, "S_ACC1", "converted", now=NOW)
        self.assertEqual(self._feedback_rows()[0]["verdict"], "converted")

    def test_triage_accept_writes_both_tables(self):
        data.triage_decision(self.conn, "re_rev", "E_DARK", accept=True, now=NOW)
        dec = self.conn.execute(
            "SELECT decision, decided_by, parser_version FROM "
            "entity_match_decisions WHERE raw_event_id='re_rev'").fetchone()
        self.assertEqual(dec["decision"], "reviewed")
        self.assertEqual(dec["decided_by"], "human")
        rq = self.conn.execute(
            "SELECT disposition, disposed_at FROM review_queue WHERE "
            "raw_event_id='re_rev' AND candidate_entity_id='E_DARK'").fetchone()
        self.assertEqual(rq["disposition"], "accepted")
        self.assertIsNotNone(rq["disposed_at"])
        # disposed item leaves the pending list
        self.assertEqual(data.review_pending(self.conn), [])

    def test_triage_reject_writes_both_tables(self):
        data.triage_decision(self.conn, "re_rev", "E_DARK", accept=False, now=NOW)
        dec = self.conn.execute(
            "SELECT decision FROM entity_match_decisions "
            "WHERE raw_event_id='re_rev'").fetchone()
        self.assertEqual(dec["decision"], "rejected")
        rq = self.conn.execute(
            "SELECT disposition FROM review_queue WHERE raw_event_id='re_rev' "
            "AND candidate_entity_id='E_DARK'").fetchone()
        self.assertEqual(rq["disposition"], "rejected")


if __name__ == "__main__":
    unittest.main()
