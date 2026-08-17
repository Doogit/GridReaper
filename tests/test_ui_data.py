"""UI data-access layer tests (R8.1-R8.3, R9.1, R4.3, R7.6, R10.7).

Hermetic: real migrations (including 0005) against in-memory SQLite, FK on,
fixture rows. Covers feed scope grouping + keyset pagination + status filter,
signal_detail (evidence + snapshots + fact provenance with no price_note),
account_signals, review_pending (pending-only + payload snippet), source_health
+ source_state classification, stale_facts, account_header, badge_legend, and
the two writers (feedback validation, atomic triage decision).
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import aggregates
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

    # regulatory obligations (R8.3 Compliance Calendar). Class-scoped, never
    # account-keyed: the reader matches them to an account by subsector.
    #   ob_cip     applies to both fixture electric classes, already in effect
    #   ob_future  applies to E_ACME's class only, effective date still ahead
    #   ob_pipe    a class no fixture entity is in — proves the filter excludes
    #   ob_public  'public' must NOT match 'muni_public' (substring trap)
    #   ob_bad     a rule this reader cannot evaluate — fail-closed + disclosed
    conn.executemany(
        "INSERT INTO regulatory_obligations (obligation_id, source_url, "
        " regulator, rule_name, affected_scope, applicability_rule, "
        " effective_date, compliance_date, mapped_products, verified_at, "
        " signal_id, derived_at) VALUES (?,?,?,?,?,?,?,NULL,?,NULL,?,?)",
        [("ob_cip", "http://fr/cip", "Federal Energy Regulatory Commission",
          "CIP-003-11 Security Management Controls",
          "NERC-registered BES owners and operators (registration not "
          "verified per account)", "subsector_in:iou_electric;muni_public",
          days_ago_date(20), "p_sentinel", "S_SEC", iso(NOW)),
         ("ob_future", "http://fr/virt", "Federal Energy Regulatory Commission",
          "Virtualization Reliability Standards",
          "NERC-registered BES owners and operators",
          "subsector_in:iou_electric", days_ago_date(-30), None, "S_REG",
          iso(NOW)),
         ("ob_pipe", "http://fr/tsa", "Transportation Security Administration",
          "Pipeline Security Directive",
          "TSA-designated critical pipeline owners and operators",
          "subsector_in:lng;midstream", days_ago_date(10), None, "S_REG",
          iso(NOW)),
         ("ob_public", "http://fr/pub", "Some Agency", "Public power rule",
          "public power", "subsector_in:public", days_ago_date(10), None,
          "S_SEC", iso(NOW)),
         ("ob_bad", "http://fr/x", "Some Agency", "Unparseable rule",
          "unknown", "entity_in:E_ACME", days_ago_date(5), None, "S_SEC",
          iso(NOW))])

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


class TestDuplicateCandidates(unittest.TestCase):
    """R8.2: proposals for the operator to judge — never an auto-merge."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_base_fixture_proposes_nothing(self):
        # S_ACC1/S_ACC2 share entity+trigger but are 5 days apart (> 3)
        self.assertEqual(data.duplicate_candidates(self.conn), [])

    def test_near_date_pair_appears_once_with_both_ids(self):
        add_signal(self.conn, "S_DUP", "E_ACME", "account", "t_lead",
                   days_ago_date(6), "Acme names new CISO (reprint)", cfa=1)
        pairs = data.duplicate_candidates(self.conn)
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual({p["signal_a"], p["signal_b"]}, {"S_ACC1", "S_DUP"})
        self.assertEqual(p["bases"], ["entity_trigger_date"])
        self.assertEqual(p["day_gap"], 1)
        self.assertEqual(p["entity_name"], "Acme Energy")

    def test_gap_beyond_window_is_not_proposed(self):
        add_signal(self.conn, "S_FAR", "E_ACME", "account", "t_lead",
                   days_ago_date(9), "Acme names new CISO (late)", cfa=1)
        # 4 days from S_ACC1, 1 day from S_ACC2 -> only the S_ACC2 pair
        pairs = data.duplicate_candidates(self.conn)
        self.assertEqual([{p["signal_a"], p["signal_b"]} for p in pairs],
                         [{"S_ACC2", "S_FAR"}])

    def test_unparseable_date_is_not_asserted_near(self):
        add_signal(self.conn, "S_NODATE", "E_ACME", "account", "t_lead",
                   "", "Acme undated", cfa=1)
        self.assertEqual(data.duplicate_candidates(self.conn), [])

    def test_content_hash_lineage_across_two_raw_events(self):
        for rid in ("re_h1", "re_h2"):
            self.conn.execute(
                "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
                "payload, url, content_hash) VALUES (?,'sp_ok',?,'{}','http://h',"
                "'HASH1')", (rid, days_ago_date(20)))
        for sid, rid in (("S_H1", "re_h1"), ("S_H2", "re_h2")):
            self.conn.execute(
                "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
                "signal_scope, trigger_id, event_date, headline, status) "
                "VALUES (?,?,'E_ACME','account','t_reg',?,?, 'active')",
                (sid, rid, days_ago_date(20), f"same content {sid}"))
        pairs = data.duplicate_candidates(self.conn)
        self.assertEqual(len(pairs), 1)
        # strongest lineage first
        self.assertEqual(pairs[0]["bases"], ["content_hash",
                                             "entity_trigger_date"])
        self.assertEqual(pairs[0]["content_hash"], "HASH1")
        self.assertEqual(pairs[0]["basis_rank"], 0)

    def test_content_hash_pairs_outrank_date_proximity_pairs(self):
        # a date-proximity pair, newer than the content_hash pair below it
        add_signal(self.conn, "S_NEAR", "E_ACME", "account", "t_lead",
                   days_ago_date(6), "Acme names new CISO (reprint)", cfa=1)
        for rid in ("re_h1", "re_h2"):
            self.conn.execute(
                "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
                "payload, url, content_hash) VALUES (?,'sp_ok',?,'{}',"
                "'http://h','HASH1')", (rid, days_ago_date(300)))
        for sid, rid in (("S_H1", "re_h1"), ("S_H2", "re_h2")):
            self.conn.execute(
                "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
                "signal_scope, trigger_id, event_date, headline, status) "
                "VALUES (?,?,'E_SUB','account','t_reg',?,?, 'active')",
                (sid, rid, days_ago_date(300), sid))
        pairs = data.duplicate_candidates(self.conn)
        # the OLDER content_hash-backed pair still sorts above the newer
        # date-proximity-only pair
        self.assertEqual([p["basis_rank"] for p in pairs], [0, 1])
        self.assertIn("content_hash", pairs[0]["bases"])
        self.assertEqual(pairs[1]["bases"], ["entity_trigger_date"])

    def test_scores_are_carried_so_the_double_count_is_visible(self):
        add_signal(self.conn, "S_DUP2", "E_ACME", "account", "t_lead",
                   days_ago_date(6), "Acme CISO reprint", cfa=1, score=2.9)
        self.conn.execute("UPDATE signals SET score=3.4 "
                          "WHERE signal_id='S_ACC1'")
        p = data.duplicate_candidates(self.conn)[0]
        self.assertEqual({p["score_a"], p["score_b"]}, {3.4, 2.9})

    def test_two_signals_off_one_raw_event_are_not_a_pair(self):
        # different triggers on the SAME record is not a duplicate pair
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            "payload, url, content_hash) VALUES ('re_one','sp_ok',?,'{}',"
            "'http://o','HASH2')", (days_ago_date(20),))
        for sid, trig in (("S_O1", "t_lead"), ("S_O2", "t_reg")):
            self.conn.execute(
                "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
                "signal_scope, trigger_id, event_date, headline, status) "
                "VALUES (?, 're_one','E_SUB','account',?,?,?, 'active')",
                (sid, trig, days_ago_date(20), sid))
        self.assertEqual(data.duplicate_candidates(self.conn), [])

    def test_decayed_signal_is_not_proposed(self):
        # S_DEAD shares entity+trigger with S_ACC1 but is decayed
        add_signal(self.conn, "S_NEARDEAD", "E_ACME", "account", "t_lead",
                   days_ago_date(401), "Old exec note reprint", status="decayed")
        self.assertEqual(data.duplicate_candidates(self.conn), [])

    def test_basis_labels_do_not_read_as_equally_strong(self):
        weak = data.duplicate_basis_label("entity_trigger_date", 2)
        strong = data.duplicate_basis_label("content_hash")
        self.assertIn("2 day(s) apart", weak)
        self.assertIn("may be two distinct events", weak)
        self.assertIn("the same underlying record", strong)


class TestJudgeHumanDisagreements(unittest.TestCase):
    """R8.2/R9.11: the Precision computation surfaced as a work queue."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def _audit(self, signal_id, check_type, result):
        self.conn.execute(
            "INSERT INTO audit (signal_id, check_type, result, ts) "
            "VALUES (?,?,?,?)", (signal_id, check_type, result, iso(NOW)))

    def _feedback(self, signal_id, verdict, reason=None):
        self.conn.execute(
            "INSERT INTO feedback (signal_id, verdict, reason_code, ts) "
            "VALUES (?,?,?,?)", (signal_id, verdict, reason, iso(NOW)))

    def test_empty_store_is_an_empty_denominator_not_agreement(self):
        d = data.judge_human_disagreements(self.conn)
        self.assertEqual(d["comparable"], 0)
        self.assertEqual(d["items"], [])

    def test_agreement_is_comparable_but_not_work(self):
        self._audit("S_ACC1", "entity_match", "pass")
        self._feedback("S_ACC1", "useful")
        d = data.judge_human_disagreements(self.conn)
        self.assertEqual(d["comparable"], 1)
        self.assertEqual(d["agree"], 1)
        self.assertEqual(d["items"], [])

    def test_disagreement_surfaces_as_an_item_with_both_verdicts(self):
        self._audit("S_ACC1", "evidence_support", "fail")
        self._feedback("S_ACC1", "useful")
        d = data.judge_human_disagreements(self.conn)
        self.assertEqual(d["disagree"], 1)
        self.assertEqual(len(d["items"]), 1)
        item = d["items"][0]
        self.assertEqual(item["signal_id"], "S_ACC1")
        self.assertEqual(item["judge"], "negative")
        self.assertEqual(item["human"], "positive")
        # enriched from signals so the operator can act without a lookup
        self.assertEqual(item["headline"], "Acme names new CISO")
        self.assertEqual(item["entity_name"], "Acme Energy")
        self.assertEqual(item["source_id"], "sp_ok")

    def test_one_sided_signal_is_not_comparable(self):
        self._audit("S_ACC1", "entity_match", "fail")   # judge only
        self._feedback("S_ACC2", "not_useful", "duplicate")  # human only
        d = data.judge_human_disagreements(self.conn)
        self.assertEqual(d["comparable"], 0)
        self.assertEqual(d["items"], [])


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

    def test_score_order_ranks_by_score_not_date(self):
        """B3/R8.1: the date keyset cannot surface the best card. S_ACC3 is the
        OLDEST account signal and the highest-scoring one."""
        for sid, score in (("S_ACC1", 2.0), ("S_ACC2", 3.5), ("S_ACC3", 4.8)):
            self.conn.execute("UPDATE signals SET score = ? WHERE signal_id = ?",
                              (score, sid))
        by_date = [r["signal_id"] for r in data.feed_page(self.conn, "account")]
        self.assertEqual(by_date, ["S_ACC1", "S_ACC2", "S_ACC3"])
        by_score = [r["signal_id"] for r in data.feed_page(
            self.conn, "account", order="score")]
        self.assertEqual(by_score, ["S_ACC3", "S_ACC2", "S_ACC1"])

    def test_score_order_puts_unscored_rows_last(self):
        self.conn.execute("UPDATE signals SET score = 1.5 "
                          "WHERE signal_id = 'S_ACC3'")
        ids = [r["signal_id"] for r in data.feed_page(
            self.conn, "account", order="score")]
        # S_ACC1/S_ACC2 are unscored (NULL) and must not outrank a real score
        self.assertEqual(ids[0], "S_ACC3")

    def test_unknown_order_raises(self):
        with self.assertRaises(ValueError):
            data.feed_page(self.conn, "account", order="bogus")

    def test_score_order_refuses_a_chronological_after_key(self):
        """Keyset paging is chronological by construction; silently ignoring an
        after_key here would page a score view wrongly."""
        with self.assertRaises(ValueError):
            data.feed_page(self.conn, "account", order="score",
                           after_key=("2026-08-01", "S_ACC1"))

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
        # a short single-field title is the whole record (R10.5)
        self.assertFalse(r["snippet_truncated"])
        self.assertEqual(r["snippet_omitted_fields"], 0)
        self.assertEqual(r["snippet_limit"], 200)

    def test_review_pending_flags_a_cut_off_payload(self):
        long_title = "A" * 250
        self.conn.execute(
            "UPDATE raw_events SET payload=? WHERE raw_event_id='re_rev'",
            ('{"title": "%s"}' % long_title,))
        r = data.review_pending(self.conn)[0]
        self.assertTrue(r["snippet_truncated"])
        self.assertEqual(len(r["snippet"]), 200)

    def test_review_pending_counts_fields_the_snippet_drops(self):
        # a SHORT title lifted out of a big record is not truncated, but it is
        # still a fragment — the omitted-field count is what makes it honest
        self.conn.execute(
            "UPDATE raw_events SET payload=? WHERE raw_event_id='re_rev'",
            ('{"title": "Short title", "activity": "Energy", '
             '"group": "g", "description": "%s"}' % ("body " * 200),))
        r = data.review_pending(self.conn)[0]
        self.assertEqual(r["snippet"], "Short title")
        self.assertFalse(r["snippet_truncated"])
        self.assertEqual(r["snippet_omitted_fields"], 3)

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


class TestAccountLicensePlays(unittest.TestCase):
    """R8.3 Products tab: plays for the account's OWN signals, snapshots only."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_plays_for_account_signal(self):
        rows = data.account_license_plays(self.conn, "E_ACME")
        self.assertEqual([r["play_id"] for r in rows], ["play1"])
        row = rows[0]
        self.assertEqual(row["signal_id"], "S_ACC1")
        self.assertEqual(row["product_name"], "Microsoft Sentinel")
        self.assertEqual(row["discovery_question"], "What is your SIEM?")
        self.assertEqual(row["display_text"], "Recommended path: Adopt E5 grant")

    def test_no_price_note_column_is_returned(self):
        """R4.3/R7.11: a non-primary price must never reach the view layer."""
        row = data.account_license_plays(self.conn, "E_ACME")[0]
        self.assertNotIn("price_note", row.keys())
        self.assertNotIn("$2/GB", " ".join(str(row[k]) for k in row.keys()))

    def test_account_without_signals_gets_no_plays(self):
        self.assertEqual(data.account_license_plays(self.conn, "E_DARK"), [])

    def test_sector_scoped_plays_are_not_attributed_to_an_account(self):
        """A sector signal's play must not appear on any account's tab: the
        account fit it would imply was never computed."""
        self.conn.execute(
            "INSERT INTO license_play_snapshots (signal_id, play_id, "
            "generated_at, generation_version, display_text) VALUES "
            "('S_SEC','play1', ?, 'plays/1.0', 'Sector play')", (iso(NOW),))
        self.conn.commit()
        for eid in ("E_ACME", "E_SUB", "E_DARK"):
            texts = [r["display_text"]
                     for r in data.account_license_plays(self.conn, eid)]
            self.assertNotIn("Sector play", texts)

    def test_non_active_signal_plays_are_excluded(self):
        self.conn.execute(
            "INSERT INTO license_play_snapshots (signal_id, play_id, "
            "generated_at, generation_version, display_text) VALUES "
            "('S_DEAD','play1', ?, 'plays/1.0', 'Decayed play')", (iso(NOW),))
        self.conn.commit()
        rows = data.account_license_plays(self.conn, "E_ACME")
        self.assertEqual([r["signal_id"] for r in rows], ["S_ACC1"])

    def test_facts_are_returned_for_badge_rendering(self):
        """R4.3: fact_ids must be selected/joined so the Products tab can
        badge a non-primary-sourced play — this column was never fetched
        before, so the tab structurally could not render that badge."""
        row = data.account_license_plays(self.conn, "E_ACME")[0]
        facts = row["facts"]
        self.assertEqual({f["fact_id"] for f in facts},
                         {"f_primary", "f_nonprimary"})
        quals = {f["fact_id"]: f["source_quality"] for f in facts}
        self.assertEqual(quals["f_nonprimary"], "non-primary")
        self.assertEqual(quals["f_primary"], "primary")
        # still no price_note anywhere in the fact projection (R4.3/R7.11)
        for f in facts:
            self.assertNotIn("price_note", f.keys())

    def test_no_facts_when_snapshot_cites_none(self):
        self.conn.execute(
            "INSERT INTO license_play_snapshots (signal_id, play_id, "
            "generated_at, generation_version, display_text) VALUES "
            "('S_ACC2','play1', ?, 'plays/1.0', 'No facts play')", (iso(NOW),))
        self.conn.commit()
        rows = {r["signal_id"]: r
                for r in data.account_license_plays(self.conn, "E_ACME")}
        self.assertEqual(rows["S_ACC2"]["facts"], [])


class TestAccountObligations(unittest.TestCase):
    """R8.3 Compliance Calendar: class-scoped obligations joined by subsector."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_matches_own_subsector_soonest_first(self):
        cal = data.account_obligations(self.conn, "E_ACME", now=NOW)
        self.assertEqual(cal["subsector"], "iou_electric")
        self.assertEqual([o["obligation_id"] for o in cal["obligations"]],
                         ["ob_cip", "ob_future"])

    def test_other_class_obligations_are_excluded(self):
        ids = [o["obligation_id"] for o in
               data.account_obligations(self.conn, "E_ACME", now=NOW)["obligations"]]
        self.assertNotIn("ob_pipe", ids)

    def test_subsector_match_is_exact_not_substring(self):
        """'public' must not match 'muni_public' — an over-broad predicate would
        put a rule on an account class it does not bind."""
        cal = data.account_obligations(self.conn, "E_DARK", now=NOW)
        self.assertEqual(cal["subsector"], "muni_public")
        self.assertEqual([o["obligation_id"] for o in cal["obligations"]],
                         ["ob_cip"])

    def test_unevaluable_rule_is_excluded_and_disclosed(self):
        cal = data.account_obligations(self.conn, "E_ACME", now=NOW)
        self.assertNotIn(
            "ob_bad", [o["obligation_id"] for o in cal["obligations"]])
        self.assertEqual(cal["unscoped"], 1)
        self.assertEqual(cal["total"], 5)

    def test_in_effect_is_computed_against_now(self):
        cal = data.account_obligations(self.conn, "E_ACME", now=NOW)
        by_id = {o["obligation_id"]: o for o in cal["obligations"]}
        self.assertTrue(by_id["ob_cip"]["in_effect"])
        self.assertFalse(by_id["ob_future"]["in_effect"])

    def test_compliance_date_is_never_derived(self):
        """R4.1: no derived obligation carries a compliance date, and the reader
        must not manufacture one from the effective date."""
        for o in data.account_obligations(
                self.conn, "E_ACME", now=NOW)["obligations"]:
            self.assertIsNone(o["compliance_date"])

    def test_mapped_products_resolve_to_names(self):
        by_id = {o["obligation_id"]: o for o in data.account_obligations(
            self.conn, "E_ACME", now=NOW)["obligations"]}
        self.assertEqual(by_id["ob_cip"]["product_names"],
                         [("p_sentinel", "Microsoft Sentinel")])
        self.assertEqual(by_id["ob_future"]["product_names"], [])

    def test_unknown_entity_returns_empty_shape(self):
        cal = data.account_obligations(self.conn, "nope", now=NOW)
        self.assertEqual(cal, {"subsector": "", "obligations": [],
                               "unscoped": 0, "excluded_by_entity": 0,
                               "total": 0})

    def test_applicability_scope_parsing(self):
        self.assertEqual(data.applicability_scope("subsector_in:a;b"),
                         ({"a", "b"}, set()))
        self.assertEqual(
            data.applicability_scope(
                "subsector_in:a;b|exclude_entities:E0155;E0156"),
            ({"a", "b"}, {"E0155", "E0156"}))
        self.assertIsNone(data.applicability_scope("entity_in:E0155"))
        self.assertIsNone(data.applicability_scope(None))

    def test_malformed_exclusion_clause_fails_closed(self):
        """The exclusion clause narrows the rule, so degrading an unreadable
        one to "no exclusions" would WIDEN the rule instead of closing it.
        Every unreadable form is unevaluable, and the obligation is dropped."""
        for rule in (
                # an exclusion clause that excludes nobody
                "subsector_in:a;b|exclude_entities:",
                "subsector_in:a;b|exclude_entities:;;",
                # a trailing separator with no clause behind it
                "subsector_in:a;b|",
                # a second clause this reader does not know
                "subsector_in:a;b|entity_in:E0155",
                # more clauses than the grammar defines
                "subsector_in:a;b|exclude_entities:E0155"
                "|exclude_entities:E0156"):
            with self.subTest(rule=rule):
                self.assertIsNone(data.applicability_scope(rule))

    def test_non_canonical_excluded_id_fails_closed(self):
        """A clause can be structurally perfect and still name nobody.

        An id that differs from the canonical ``E`` + four digits by
        whitespace, case or shape parses into a non-empty set that matches no
        real entity_id, so the account it was written to exclude is silently
        ADMITTED — the one direction this design exists to prevent. The writer
        emits canonical ids, so any other form means the predicate did not come
        from app/obligations.py and cannot be trusted to name every entity it
        should: unevaluable, not "no exclusions"."""
        for bad_id in (
                "E0155 ",           # trailing space
                " E0155",           # leading space
                "E0155\t",          # trailing tab
                "e0155",            # lowercase — and, paired below, mixed case
                "X0155",            # wrong prefix letter
                "E015",             # too few digits
                "E01555",           # too many digits
                "E015A",            # non-numeric suffix
                "E-155",            # non-numeric suffix, separator shape
                "0155"):            # no prefix at all
            # The bad id is never last. The real clause names seven, and a
            # trailing space in final position is absorbed by the rule-level
            # strip() below before the ids are ever split out.
            rule = ("subsector_in:renewables|exclude_entities:"
                    + bad_id + ";E0156")
            with self.subTest(rule=rule):
                self.assertIsNone(data.applicability_scope(rule))

    def test_trailing_whitespace_on_the_whole_rule_is_normalized_not_widened(
            self):
        """The reader strips the stored rule before parsing it, so whitespace
        in final position resolves to the canonical id rather than to a token
        that matches nobody. Pinned because it is the one whitespace case that
        does NOT fail closed, and it is safe only because stripping it yields
        the exclusion the writer meant."""
        self.assertEqual(
            data.applicability_scope(
                "subsector_in:renewables|exclude_entities:E0155;E0156 \n"),
            ({"renewables"}, {"E0155", "E0156"}))

    def test_well_formed_excluded_id_absent_from_the_watchlist_parses(self):
        """Shape is the parser's concern; existence is not. This reader has no
        DB access, so an id it cannot find is still an id it can evaluate — a
        stale exclusion excludes nobody and is pinned in
        tests/test_obligations.py against the seed CSV, not here."""
        self.assertEqual(
            data.applicability_scope(
                "subsector_in:renewables|exclude_entities:E0999"),
            ({"renewables"}, {"E0999"}))

    def _add_canonical_sibling(self):
        """An iou_electric account whose id is in the canonical ``E``+4-digit
        form the exclusion clause requires (the fixture's own ids are not)."""
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector) "
            "VALUES ('E0999','Canonical Grid Co','iou_electric')")

    def test_excluded_entity_is_dropped_from_its_own_subsector(self):
        """E_ACME and E0999 share a subsector; a rule naming E0999 in its
        exclusion clause must bind the first and not the second."""
        self._add_canonical_sibling()
        self.conn.execute(
            "INSERT INTO regulatory_obligations (obligation_id, source_url, "
            " regulator, rule_name, affected_scope, applicability_rule, "
            " effective_date, compliance_date, mapped_products, verified_at, "
            " signal_id, derived_at) VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
            ("ob_narrow", "http://fr/narrow", "Some Agency", "Narrowed rule",
             "owners and operators (registration not verified per account)",
             "subsector_in:iou_electric|exclude_entities:E0999",
             days_ago_date(3), "S_SEC", iso(NOW)))
        self.conn.commit()
        for entity_id, expected in (("E_ACME", ["ob_narrow"]), ("E0999", [])):
            cal = data.account_obligations(self.conn, entity_id, now=NOW)
            self.assertEqual(
                [o["obligation_id"] for o in cal["obligations"]
                 if o["obligation_id"] == "ob_narrow"], expected)
            # Excluded, not undecidable: the rule evaluated cleanly.
            self.assertEqual(cal["unscoped"], 1)

    def test_entity_exclusion_is_counted_separately_from_a_class_miss(self):
        """Two accounts in one subsector get different rows; without a count of
        the individual exclusions the reader cannot tell "this class has no
        obligation" from "this account was dropped from one" (R4.1)."""
        self._add_canonical_sibling()
        self.conn.execute(
            "INSERT INTO regulatory_obligations (obligation_id, source_url, "
            " regulator, rule_name, affected_scope, applicability_rule, "
            " effective_date, compliance_date, mapped_products, verified_at, "
            " signal_id, derived_at) VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
            ("ob_narrow", "http://fr/narrow", "Some Agency", "Narrowed rule",
             "owners and operators (registration not verified per account)",
             "subsector_in:iou_electric|exclude_entities:E0999",
             days_ago_date(3), "S_SEC", iso(NOW)))
        self.conn.commit()
        # dropped by name from a rule its own subsector admits
        excluded = data.account_obligations(self.conn, "E0999", now=NOW)
        self.assertEqual(excluded["excluded_by_entity"], 1)
        # same subsector, admitted: nothing to disclose
        admitted = data.account_obligations(self.conn, "E_ACME", now=NOW)
        self.assertEqual(admitted["excluded_by_entity"], 0)
        self.assertIn("ob_narrow",
                      [o["obligation_id"] for o in admitted["obligations"]])
        # another class entirely — a class miss is not an individual exclusion
        other = data.account_obligations(self.conn, "E_DARK", now=NOW)
        self.assertEqual(other["excluded_by_entity"], 0)
        # the pre-existing disclosures keep their meanings
        for cal in (excluded, admitted, other):
            self.assertEqual(cal["unscoped"], 1)
            self.assertEqual(cal["total"], 6)

    def test_obligation_with_a_malformed_exclusion_is_shown_to_nobody(self):
        self.conn.execute(
            "INSERT INTO regulatory_obligations (obligation_id, source_url, "
            " regulator, rule_name, affected_scope, applicability_rule, "
            " effective_date, compliance_date, mapped_products, verified_at, "
            " signal_id, derived_at) VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
            ("ob_broken", "http://fr/broken", "Some Agency", "Broken rule",
             "unknown", "subsector_in:iou_electric|exclude_entities:",
             days_ago_date(3), "S_SEC", iso(NOW)))
        self.conn.commit()
        for entity_id in ("E_ACME", "E_SUB", "E_DARK"):
            cal = data.account_obligations(self.conn, entity_id, now=NOW)
            self.assertNotIn(
                "ob_broken", [o["obligation_id"] for o in cal["obligations"]])
            self.assertEqual(cal["unscoped"], 2)
            self.assertEqual(cal["total"], 6)


class TestAccountRelationships(unittest.TestCase):
    """R8.3 Entity Graph: sourced edges only, in both directions."""

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def test_parent_sees_child_edge(self):
        rows = data.account_relationships(self.conn, "E_ACME")
        self.assertEqual([(r["direction"], r["entity_id"]) for r in rows],
                         [("child", "E_SUB")])
        self.assertEqual(rows[0]["relationship_type"], "subsidiary")

    def test_child_sees_parent_edge(self):
        rows = data.account_relationships(self.conn, "E_SUB")
        self.assertEqual([(r["direction"], r["entity_id"]) for r in rows],
                         [("parent", "E_ACME")])

    def test_seeded_parent_id_hint_is_not_an_edge(self):
        """E_SUB's watchlist parent_id is E_ACME, but only the stored
        entity_relationships row counts here — a hint with no source and no
        verification date must not render as a sourced edge."""
        self.conn.execute("DELETE FROM entity_relationships")
        self.conn.commit()
        self.assertEqual(data.account_relationships(self.conn, "E_SUB"), [])
        self.assertIsNotNone(data.account_header(self.conn, "E_SUB")["parent"])

    def test_account_with_no_edges(self):
        self.assertEqual(data.account_relationships(self.conn, "E_DARK"), [])


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


class TestExploreData(unittest.TestCase):
    """Explore reads (U8, R8.5, R4.1): analytics counts, evidence-gated facility
    points, and the state-density rollup. The 0.85 owner-confidence gate MUST
    live in the reader — an under-evidenced facility can never be returned."""

    def setUp(self):
        self.conn = fixture_conn()
        # Two facilities on the same TX-owning entity: one gated (0.9, kept),
        # one below the floor (0.5, must never be returned). Austin, TX coords.
        self.conn.execute(
            "INSERT INTO facility_assets (facility_id, source_id, "
            " facility_name, latitude, longitude, capacity_mw, "
            " owner_operator_entity_id, facility_owner_confidence) VALUES "
            "('F_GOOD','sp_ok','Austin Plant', 30.3, -97.7, 500, "
            " 'E_ACME', 0.9)")
        self.conn.execute(
            "INSERT INTO facility_assets (facility_id, source_id, "
            " facility_name, latitude, longitude, capacity_mw, "
            " owner_operator_entity_id, facility_owner_confidence) VALUES "
            "('F_WEAK','sp_ok','Guessed Plant', 31.0, -98.0, 200, "
            " 'E_ACME', 0.5)")
        # An incident signal so the incident-tier slice is non-empty.
        self.conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
            " signal_scope, trigger_id, event_date, headline, evidence_snippet, "
            " source_url, confidence, evidence_quality, "
            " incident_evidence_level, customer_facing_allowed, score, status) "
            "VALUES ('S_INC','re_acc','E_ACME','account','t_lead', ?, "
            "'Acme breach disclosed','ev','http://src',0.9,'IR',"
            "'confirmed',1,3.0,'active')", (days_ago_date(4),))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_analytics_counts_match_seeded_signals(self):
        counts = data.explore_analytics_counts(self.conn)
        # active signals: 3 account t_lead + S_INC (t_lead) + S_SEC + S_REG (t_reg);
        # S_DEAD is decayed and excluded.
        by_trigger = {r["key"]: r["count"] for r in counts["trigger"]}
        self.assertEqual(by_trigger["t_lead"], 4)   # 3 account + 1 incident
        self.assertEqual(by_trigger["t_reg"], 2)    # sector + regulatory
        by_scope = {r["key"]: r["count"] for r in counts["scope"]}
        self.assertEqual(by_scope["account"], 4)
        self.assertEqual(by_scope["sector"], 1)
        self.assertEqual(by_scope["regulatory_calendar"], 1)
        # Only the incident signal carries a tier.
        by_tier = {r["key"]: r["count"] for r in counts["incident_tier"]}
        self.assertEqual(by_tier, {"confirmed": 1})

    def test_analytics_excludes_decayed_by_default(self):
        by_trigger = {r["key"]: r["count"]
                      for r in data.explore_analytics_counts(self.conn)["trigger"]}
        # S_DEAD (decayed) would push t_lead to 5 if it leaked in.
        self.assertEqual(by_trigger["t_lead"], 4)

    def test_facility_gate_returns_only_high_confidence(self):
        points = data.explore_facility_points(self.conn)
        ids = {p["facility_id"] for p in points}
        self.assertIn("F_GOOD", ids)          # 0.9 -> kept
        self.assertNotIn("F_WEAK", ids)        # 0.5 -> gated out in the reader
        good = next(p for p in points if p["facility_id"] == "F_GOOD")
        self.assertEqual(good["entity_name"], "Acme Energy")
        self.assertEqual(good["subsector"], "iou_electric")

    def test_facility_gate_boundary_exactly_085(self):
        # A facility at exactly the floor (0.85) is admitted (>= gate).
        self.conn.execute(
            "INSERT INTO facility_assets (facility_id, latitude, longitude, "
            " owner_operator_entity_id, facility_owner_confidence) VALUES "
            "('F_EDGE', 30.0, -96.0, 'E_ACME', 0.85)")
        self.conn.commit()
        ids = {p["facility_id"] for p in data.explore_facility_points(self.conn)}
        self.assertIn("F_EDGE", ids)

    def test_facility_null_confidence_excluded(self):
        self.conn.execute(
            "INSERT INTO facility_assets (facility_id, latitude, longitude, "
            " owner_operator_entity_id) VALUES ('F_NULL', 30.0, -96.0, 'E_ACME')")
        self.conn.commit()
        ids = {p["facility_id"] for p in data.explore_facility_points(self.conn)}
        self.assertNotIn("F_NULL", ids)

    def test_facility_without_owner_excluded(self):
        self.conn.execute(
            "INSERT INTO facility_assets (facility_id, latitude, longitude, "
            " facility_owner_confidence) VALUES ('F_NO_OWNER', 30.0, -96.0, 0.95)")
        self.conn.commit()
        point_ids = {p["facility_id"] for p in data.explore_facility_points(self.conn)}
        density_ids = {r["facility_id"] for r in data.explore_state_density(self.conn)}
        self.assertNotIn("F_NO_OWNER", point_ids)
        self.assertNotIn("F_NO_OWNER", density_ids)

    def test_state_density_carries_owner_signal_count(self):
        rows = data.explore_state_density(self.conn)
        # Only the gated facility appears; its owner E_ACME has 3 active signals
        # (S_ACC1, S_ACC2, and S_INC — S_ACC3 is E_SUB, S_DEAD is decayed).
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity_id"], "E_ACME")
        self.assertEqual(rows[0]["signal_count"], 3)

    def test_empty_store_reads_zero_not_fabricated(self):
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.execute("PRAGMA foreign_keys=ON")
        apply_migrations(empty)
        counts = data.explore_analytics_counts(empty)
        self.assertEqual(counts, {"trigger": [], "scope": [], "incident_tier": []})
        self.assertEqual(data.explore_facility_points(empty), [])
        self.assertEqual(data.explore_state_density(empty), [])
        empty.close()


# -- R3.7 provenance + its R4.1 privacy gate ---------------------------------

# The real shape on this corpus: raw_event_id = "{source_id}:{native_id}" and an
# RSS classifier's native_id is the item guid, i.e. the article link — whose slug
# names the breached company.
VICTIM = "trezor"
VICTIM_RAW_EVENT_ID = (
    "bleepingcomputer:https://www.bleepingcomputer.com/news/security/"
    "trezor-discloses-data-breach-affecting-nearly-14-000-customers/")


def provenance_conn():
    """A raw event whose id embeds a victim-naming URL, classified by two
    co-tenant classifiers on one source (the case that makes the classifier_id
    join load-bearing), plus one account-scoped and one sector-scoped signal."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_inc','Incident',5,120)")
    conn.execute("INSERT INTO watchlist_entities (entity_id, name) "
                 "VALUES ('E_ACME','Acme Energy')")
    conn.execute("INSERT INTO source_policies (source_id, name, enabled, ttl, "
                 "access_method, evidence_rank) VALUES "
                 "('bleepingcomputer','BleepingComputer',1,3600,'rss',3)")
    conn.execute("INSERT INTO source_runs (run_id, source_id, started_at, "
                 "status, parser_version) VALUES "
                 "('run1','bleepingcomputer', ?, 'success', "
                 "'security_rss_fetch/2.0')", (iso(NOW),))
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, run_id, event_date, "
        "payload, url) VALUES (?, 'bleepingcomputer','run1', ?, '{}', "
        "'https://www.bleepingcomputer.com/news/security/trezor-discloses/')",
        (VICTIM_RAW_EVENT_ID, days_ago_date(1)))
    for sid, scope, entity in (("S_PEER", "sector", None),
                               ("S_OWN", "account", "E_ACME")):
        conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
            "signal_scope, trigger_id, event_date, headline, evidence_snippet, "
            "source_url, confidence, evidence_quality, "
            "customer_facing_allowed, score, status) "
            "VALUES (?,?,?,?, 't_inc', ?, 'An organization in the sector "
            "disclosed a breach','An organization disclosed a breach',"
            "'https://example.test/x', 0.8, 'IR', 0, 3.0, 'active')",
            (sid, VICTIM_RAW_EVENT_ID, entity, scope, days_ago_date(1)))
        conn.execute(
            "INSERT INTO signal_evidence (signal_id, raw_event_id, "
            "evidence_text, evidence_locator, evidence_rank, "
            "extraction_version) VALUES (?, ?, 'An organization disclosed a "
            "breach.', 'item', 3, 'incident_security_rss/1.0')",
            (sid, VICTIM_RAW_EVENT_ID))
    # Two classifiers bookkeep the SAME raw event; only one owns these signals.
    conn.executemany(
        "INSERT INTO classified_events (raw_event_id, classifier_id, "
        "parser_version, processed_at, signals_emitted) VALUES (?,?,?,?,1)",
        [(VICTIM_RAW_EVENT_ID, "incident_security_rss",
          "incident_security_rss/1.1", iso(NOW)),
         (VICTIM_RAW_EVENT_ID, "company_statement",
          "company_statement/9.9", iso(NOW + timedelta(hours=1)))])
    conn.commit()
    return conn


class TestIdentitySafeRawEventRef(unittest.TestCase):
    """R4.1 gate on the R3.7 reference: a name-free card must not print an id
    that names the victim."""

    def test_account_scope_passes_the_id_through(self):
        for scope in ("account", "parent"):
            ref = data.identity_safe_raw_event_ref(VICTIM_RAW_EVENT_ID, scope)
            self.assertEqual(ref["ref"], VICTIM_RAW_EVENT_ID)
            self.assertFalse(ref["hashed"])

    def test_non_account_scopes_are_hashed(self):
        for scope in ("sector", "subsector", "regulatory_calendar", None):
            ref = data.identity_safe_raw_event_ref(VICTIM_RAW_EVENT_ID, scope)
            self.assertTrue(ref["hashed"])
            self.assertRegex(ref["ref"], r"^[0-9a-f]{16}$")
            self.assertNotIn(VICTIM, ref["ref"])
            self.assertNotIn("http", ref["ref"])

    def test_hash_is_stable_and_distinguishing(self):
        a = data.identity_safe_raw_event_ref(VICTIM_RAW_EVENT_ID, "sector")
        b = data.identity_safe_raw_event_ref(VICTIM_RAW_EVENT_ID, "sector")
        c = data.identity_safe_raw_event_ref("other:evt", "sector")
        self.assertEqual(a["ref"], b["ref"])
        self.assertNotEqual(a["ref"], c["ref"])

    def test_missing_id_is_empty_not_a_hash_of_nothing(self):
        for value in (None, "", "   "):
            self.assertEqual(data.identity_safe_raw_event_ref(value, "sector"),
                             {"ref": "", "hashed": False})


class TestSignalProvenance(unittest.TestCase):
    """R3.7: signal_detail carries both parser versions."""

    def setUp(self):
        self.conn = provenance_conn()

    def tearDown(self):
        self.conn.close()

    def test_classifier_version_comes_from_the_owning_classifier(self):
        prov = data.signal_detail(self.conn, "S_PEER")["provenance"]
        # NOT company_statement/9.9 — that co-tenant processed the same raw
        # event but did not mint this signal.
        self.assertEqual(prov["classifier_id"], "incident_security_rss")
        self.assertEqual(prov["classifier_parser_version"],
                         "incident_security_rss/1.1")
        self.assertEqual(prov["extraction_version"],
                         "incident_security_rss/1.0")

    def test_fetch_version_comes_from_the_source_run(self):
        prov = data.signal_detail(self.conn, "S_OWN")["provenance"]
        self.assertEqual(prov["fetch_parser_version"], "security_rss_fetch/2.0")

    def test_unbookkept_signal_reports_none_not_a_guess(self):
        conn = fixture_conn()
        try:
            prov = data.signal_detail(conn, "S_ACC1")["provenance"]
            self.assertIsNone(prov["classifier_parser_version"])
            self.assertIsNone(prov["fetch_parser_version"])
        finally:
            conn.close()

    def test_scoring_config_version_is_selected_onto_the_card_row(self):
        signal = data.signal_detail(self.conn, "S_OWN")["signal"]
        self.assertIn("scoring_config_version", signal.keys())
        self.assertIsNone(signal["scoring_config_version"])


# -- R8.10: the aggregate reader -------------------------------------------

class TestAnalyticsCountsReader(unittest.TestCase):
    """A stale aggregate is never served, and never served silently.

    The load-bearing property of the R8.10 unit. Every path below asserts BOTH
    halves: the numbers handed back are the store's current truth, and the
    envelope tells the reader what the precomputed layer is doing.
    """

    def setUp(self):
        self.conn = fixture_conn()

    def tearDown(self):
        self.conn.close()

    def _tamper(self, count=999):
        """Overwrite the stored counts with a value the live store can never
        produce. If a later assertion sees this number, a stale aggregate WAS
        served — which is the failure this whole unit exists to prevent."""
        self.conn.execute("UPDATE signal_aggregates SET count = ?", (count,))

    def _stored_counts(self):
        return {(r["dimension"], r["key"]): r["count"] for r in
                self.conn.execute("SELECT dimension, key, count "
                                  "FROM signal_aggregates")}

    def test_fresh_aggregate_is_served_with_its_own_as_of(self):
        aggregates.refresh(self.conn, now=NOW)
        result = data.analytics_counts(self.conn, now=NOW)
        self.assertEqual(result["served_from"], "aggregate")
        self.assertFalse(result["aggregate_stale"])
        self.assertIsNone(result["stale_reason"])
        self.assertEqual(result["as_of"], NOW.isoformat())
        self.assertEqual(result["aggregate_as_of"], NOW.isoformat())
        self.assertTrue(result["as_of"].endswith("+00:00"))   # R10.2

    def test_fresh_aggregate_equals_the_live_computation(self):
        aggregates.refresh(self.conn, now=NOW)
        self.assertEqual(data.analytics_counts(self.conn, now=NOW)["counts"],
                         data.explore_analytics_counts(self.conn))

    def test_stale_aggregate_is_never_served_after_the_store_moves(self):
        # THE proving test. Refresh, then retract a signal: the row COUNT is
        # unchanged, so only the basis fingerprint catches it.
        aggregates.refresh(self.conn, now=NOW)
        self.conn.execute(
            "UPDATE signals SET status = 'retracted' WHERE signal_id = 'S_ACC1'")
        self._tamper()
        result = data.analytics_counts(self.conn, now=NOW)

        # 1. the numbers are today's truth, not the stored ones
        live = data.explore_analytics_counts(self.conn)
        self.assertEqual(result["counts"], live)
        self.assertNotIn(999, [row["count"] for rows in
                               result["counts"].values() for row in rows])
        self.assertIn(999, self._stored_counts().values())  # tamper still there

        # 2. and the reader is told, with the reason and how far behind it is
        self.assertEqual(result["served_from"], "live")
        self.assertTrue(result["aggregate_stale"])
        self.assertEqual(result["stale_reason"], "basis_changed")
        self.assertEqual(result["aggregate_as_of"], NOW.isoformat())
        self.assertEqual(result["refresh_command"], "python -m app.aggregates")

    def test_operator_retier_alone_invalidates_the_aggregate(self):
        # No new row, no status flip — only an R8.7 re-tier, which moves the
        # incident_tier bucket. A row-count basis would serve stale counts here.
        aggregates.refresh(self.conn, now=NOW)
        self.conn.execute(
            "UPDATE signals SET incident_evidence_level = 'corroborated' "
            "WHERE signal_id = 'S_ACC1'")
        self.conn.execute(
            "INSERT INTO incident_tier_edits (signal_id, old_level, new_level, "
            " old_cfa, new_cfa, ts) VALUES ('S_ACC1','confirmed',"
            "'corroborated',1,0,?)", (iso(NOW),))
        self._tamper()
        result = data.analytics_counts(self.conn, now=NOW)
        self.assertEqual(result["stale_reason"], "basis_changed")
        self.assertEqual(result["counts"],
                         data.explore_analytics_counts(self.conn))

    def test_never_refreshed_store_reads_live_and_says_so(self):
        result = data.analytics_counts(self.conn, now=NOW)
        self.assertEqual(result["served_from"], "live")
        self.assertEqual(result["stale_reason"], "missing")
        self.assertIsNone(result["aggregate_as_of"])
        self.assertEqual(result["counts"],
                         data.explore_analytics_counts(self.conn))

    def test_aggregate_past_the_nightly_window_expires(self):
        # Store untouched, so the basis still matches — only age is wrong. The
        # refresh silently not running is a real failure mode and must not read
        # as "fresh" forever.
        aggregates.refresh(self.conn, now=NOW)
        later = NOW + timedelta(seconds=data.AGGREGATE_MAX_AGE_SECONDS + 1)
        self._tamper()
        result = data.analytics_counts(self.conn, now=later)
        self.assertEqual(result["stale_reason"], "expired")
        self.assertEqual(result["counts"],
                         data.explore_analytics_counts(self.conn))
        # just inside the window it is still servable
        edge = NOW + timedelta(seconds=data.AGGREGATE_MAX_AGE_SECONDS - 1)
        self.assertFalse(
            data.analytics_counts(self.conn, now=edge)["aggregate_stale"])

    def test_future_dated_stamp_is_not_extra_fresh(self):
        aggregates.refresh(self.conn, now=NOW + timedelta(days=2))
        result = data.analytics_counts(self.conn, now=NOW)
        self.assertEqual(result["stale_reason"], "expired")
        self.assertEqual(result["served_from"], "live")

    def test_unreadable_stamp_is_not_servable(self):
        aggregates.refresh(self.conn, now=NOW)
        self.conn.execute("UPDATE aggregate_state SET computed_at = 'nonsense'")
        self._tamper()
        result = data.analytics_counts(self.conn, now=NOW)
        self.assertEqual(result["stale_reason"], "unreadable_stamp")
        self.assertEqual(result["counts"],
                         data.explore_analytics_counts(self.conn))

    def test_a_different_status_filter_is_never_answered_from_the_aggregate(self):
        # The nightly refresh precomputes active-only; answering an all-status
        # question from it would be a wrong answer wearing a fresh stamp.
        aggregates.refresh(self.conn, now=NOW)
        self._tamper()
        statuses = ("active", "decayed")
        result = data.analytics_counts(self.conn, statuses=statuses, now=NOW)
        self.assertEqual(result["stale_reason"], "status_filter_unsupported")
        self.assertEqual(result["counts"],
                         data.explore_analytics_counts(self.conn, statuses))
        # and the default filter is still served from the aggregate
        self.assertEqual(
            data.analytics_counts(self.conn, now=NOW)["served_from"],
            "aggregate")

    def test_computed_and_empty_is_not_never_computed(self):
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.execute("PRAGMA foreign_keys=ON")
        apply_migrations(empty)
        try:
            self.assertEqual(
                data.analytics_counts(empty, now=NOW)["stale_reason"],
                "missing")
            aggregates.refresh(empty, now=NOW)
            result = data.analytics_counts(empty, now=NOW)
            self.assertEqual(result["served_from"], "aggregate")
            self.assertFalse(result["aggregate_stale"])
            self.assertEqual(result["counts"],
                             {"trigger": [], "scope": [], "incident_tier": []})
            self.assertEqual(result["aggregate_as_of"], NOW.isoformat())
        finally:
            empty.close()

    def test_memo_is_request_scoped_not_process_global(self):
        # Inside one request the second read is answered from the memo...
        aggregates.refresh(self.conn, now=NOW)
        memo = {}
        first = data.analytics_counts(self.conn, now=NOW, memo=memo)
        self.conn.execute(
            "UPDATE signals SET status = 'retracted' WHERE signal_id = 'S_ACC1'")
        self.assertIs(data.analytics_counts(self.conn, now=NOW, memo=memo),
                      first)
        # ...and the NEXT request, with its own memo, sees the change. Nothing
        # is cached across requests, so a refresh can never be shadowed.
        fresh = data.analytics_counts(self.conn, now=NOW, memo={})
        self.assertTrue(fresh["aggregate_stale"])
        self.assertEqual(fresh["counts"],
                         data.explore_analytics_counts(self.conn))

    def test_memo_keys_on_the_status_filter(self):
        aggregates.refresh(self.conn, now=NOW)
        memo = {}
        default = data.analytics_counts(self.conn, now=NOW, memo=memo)
        other = data.analytics_counts(self.conn, statuses=("active", "decayed"),
                                      now=NOW, memo=memo)
        self.assertEqual(default["served_from"], "aggregate")
        self.assertEqual(other["served_from"], "live")
        self.assertEqual(len(memo), 2)


class TestConfigWriteConnLockResolution(unittest.TestCase):
    """R3.2/R3.1: config_write_conn's bare ``ingest_lock(lock_path)`` call must
    let GRIDSIGNALS_LOCK reach the writer — the one call site (of eleven) that
    previously resolved ``lock_path or LOCK_PATH`` to a concrete path BEFORE
    calling ingest_lock, so the env var never got a turn."""

    def test_env_var_is_honored_when_lock_path_is_not_passed(self):
        with tempfile.TemporaryDirectory(prefix="gs-cwc-lock-") as td:
            env_path = os.path.join(td, "env.lock")
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                conn = sqlite3.connect(db_path)
                apply_migrations(conn)
                conn.close()
                with mock.patch.dict(os.environ, {"GRIDSIGNALS_LOCK": env_path}):
                    with data.config_write_conn(db_path=db_path, lock_path=None) \
                            as conn:
                        self.assertTrue(
                            os.path.exists(env_path),
                            "GRIDSIGNALS_LOCK is documented as the lock path "
                            "but config_write_conn did not take its lock there")
                self.assertFalse(os.path.exists(env_path))
            finally:
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(db_path + suffix):
                        os.remove(db_path + suffix)

    def test_explicit_lock_path_still_beats_the_env_var(self):
        # Must NOT invert precedence: an explicit lock_path argument still
        # wins over GRIDSIGNALS_LOCK (ingest_lock's own resolution order).
        with tempfile.TemporaryDirectory(prefix="gs-cwc-lock-") as td:
            explicit = os.path.join(td, "explicit.lock")
            env_path = os.path.join(td, "env.lock")
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                conn = sqlite3.connect(db_path)
                apply_migrations(conn)
                conn.close()
                with mock.patch.dict(os.environ, {"GRIDSIGNALS_LOCK": env_path}):
                    with data.config_write_conn(
                            db_path=db_path, lock_path=explicit) as conn:
                        self.assertTrue(os.path.exists(explicit))
                        self.assertFalse(os.path.exists(env_path))
            finally:
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(db_path + suffix):
                        os.remove(db_path + suffix)


class TestPrecisionReportWindowing(unittest.TestCase):
    """R9.3/R9.5: precision_report windows its headline useful=/auto= rate,
    Gate G2, and the per-dimension tables to the UTC calendar month containing
    ``now`` — the same "month containing now" convention spotcheck_coverage
    already uses. Gate G1 stays on the FULL, unwindowed history (it measures
    cumulative evidence over a >=30-day span that can outlive one month)."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES ('src_a', 'Source A', 1)")
        self.conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days) "
            "VALUES ('leadership_change', 'Leadership change', 3, 90)")
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name) "
            "VALUES ('E_ACME', 'Acme Energy')")
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id) "
            "VALUES ('re1', 'src_a')")
        for sid in ("s_in", "s_out"):
            self.conn.execute(
                "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
                " signal_scope, trigger_id, status) VALUES "
                "(?, 're1', 'E_ACME', 'account', 'leadership_change', "
                " 'active')", (sid,))
        # s_in: inside the August 2026 reporting window. s_out: July 2026,
        # outside it.
        self.conn.execute(
            "INSERT INTO feedback (signal_id, verdict, ts) VALUES "
            "('s_in', 'useful', '2026-08-10T00:00:00Z')")
        self.conn.execute(
            "INSERT INTO feedback (signal_id, verdict, reason_code, ts) "
            "VALUES ('s_out', 'not_useful', 'other', '2026-07-05T00:00:00Z')")
        self.conn.execute(
            "INSERT INTO audit (signal_id, check_type, result, ts) VALUES "
            "('s_in', 'entity_match', 'pass', '2026-08-11T00:00:00Z')")
        self.conn.execute(
            "INSERT INTO audit (signal_id, check_type, result, ts) VALUES "
            "('s_out', 'entity_match', 'fail', '2026-07-06T00:00:00Z')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_rows_outside_the_reporting_month_do_not_affect_the_rate(self):
        report = data.precision_report(self.conn, now="2026-08-16T00:00:00Z")
        # only s_in (August) counts: 1 useful over n=1 -> 100%, not 50%
        self.assertEqual(report["useful_overall"], {"useful": 1, "total": 1,
                                                     "rate": 1.0})
        self.assertEqual(report["auto_overall"]["scored"], 1)
        self.assertEqual(report["auto_overall"]["accuracy"], 1.0)

    def test_rows_inside_the_reporting_month_do_affect_the_rate(self):
        # widen the window to July: now s_out (July) is the only in-window row
        report = data.precision_report(self.conn, now="2026-07-20T00:00:00Z")
        self.assertEqual(report["useful_overall"],
                         {"useful": 0, "total": 1, "rate": 0.0})

    def test_zero_in_window_rows_reports_distinctly_from_never_audited(self):
        # September has zero rows -> total=0 -> rate=None ("n/a"), never a
        # fabricated 0% and never conflated with "no data at all" (both
        # feedback rows exist, they're simply outside this window)
        report = data.precision_report(self.conn, now="2026-09-05T00:00:00Z")
        self.assertEqual(report["useful_overall"],
                         {"useful": 0, "total": 0, "rate": None})
        self.assertIsNone(report["auto_overall"]["accuracy"])
        self.assertEqual(report["auto_overall"]["scored"], 0)

    def test_g1_stays_on_the_full_unwindowed_history(self):
        # G1's per-trigger useful_n counts BOTH signals regardless of month -
        # it measures cumulative evidence, not a single reporting period.
        report = data.precision_report(self.conn, now="2026-08-16T00:00:00Z")
        self.assertEqual(
            report["g1"]["triggers"]["leadership_change"]["useful_n"], 2)

    def test_g2_is_windowed_the_same_as_the_headline(self):
        report = data.precision_report(self.conn, now="2026-08-16T00:00:00Z")
        self.assertEqual(report["g2"]["src_a"]["n"], 1)


class TestSourcePolicyRowsG2Windowing(unittest.TestCase):
    """source_policy_rows must window feedback/audit the SAME way
    precision_report does — the two-surface consistency invariant
    (Precision page vs. Admin source table, R9.5) breaks silently otherwise."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON;")
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES ('src_a', 'Source A', 1)")
        self.conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days) "
            "VALUES ('leadership_change', 'Leadership change', 3, 90)")
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name) "
            "VALUES ('E_ACME', 'Acme Energy')")
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id) "
            "VALUES ('re1', 'src_a')")
        self.conn.execute(
            "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
            " signal_scope, trigger_id, status) VALUES "
            "('s_out', 're1', 'E_ACME', 'account', 'leadership_change', "
            " 'active')")
        # A July feedback row only -- an August report must see NO rated
        # feedback for this source, matching precision_report's own window.
        self.conn.execute(
            "INSERT INTO feedback (signal_id, verdict, reason_code, ts) "
            "VALUES ('s_out', 'not_useful', 'other', '2026-07-05T00:00:00Z')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_out_of_window_feedback_does_not_populate_g2(self):
        # No rated feedback falls inside the August window, so g2 is None --
        # the SAME "no rated feedback for this source yet" shape a source with
        # zero feedback ever gets, not a zero-precision cell (R9.5).
        rows = {r["source_id"]: r for r in data.source_policy_rows(
            self.conn, now="2026-08-16T00:00:00Z")}
        self.assertIsNone(rows["src_a"]["g2"])

    def test_agrees_with_precision_reports_g2_for_the_same_now(self):
        # Widen to July so BOTH surfaces have a rated row for src_a in-window,
        # and assert they report the IDENTICAL cell (the two-surface
        # consistency invariant, R9.5).
        precision_g2 = data.precision_report(
            self.conn, now="2026-07-20T00:00:00Z")["g2"]["src_a"]
        admin_g2 = {r["source_id"]: r for r in data.source_policy_rows(
            self.conn, now="2026-07-20T00:00:00Z")}["src_a"]["g2"]
        self.assertEqual(precision_g2["n"], admin_g2["n"])
        self.assertEqual(precision_g2["precision"], admin_g2["precision"])


if __name__ == "__main__":
    unittest.main()
