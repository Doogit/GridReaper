"""Regulatory obligation producer tests (R8.3, R7.2, R4.1).

Hermetic: real migrations against in-memory SQLite, FK enforcement on, canned
Federal Register payloads modelled on the rows actually stored today. No
network. Focus is the derivation contract - which signals qualify, which
fields are sourced vs deliberately NULL, and that a re-run is a no-op.
"""
import json
import sqlite3
import unittest
from pathlib import Path

from app.db.migrate import apply_migrations
from app.obligations import APPLICABILITY, derive_obligations

PIPELINE = (Path(__file__).resolve().parent.parent / "deploy"
            / "ingest_pipeline.sh")

FERC_AGENCY = {"raw_name": "Federal Energy Regulatory Commission",
               "name": "Federal Energy Regulatory Commission",
               "id": 167, "slug": "federal-energy-regulatory-commission"}
DOE_AGENCY = {"raw_name": "DEPARTMENT OF ENERGY", "name": "Energy Department",
              "id": 136, "slug": "energy-department"}

# Modelled on federal_register:2026-05711 (names CIP-003 in its own title).
CIP_003_DOC = {
    "document_number": "2026-05711",
    "title": ("Order No. 918; Critical Infrastructure Protection Reliability "
              "Standard CIP-003-11-Cyber Security-Security Management "
              "Controls"),
    "type": "Rule",
    "abstract": ("The Commission approves the proposed Critical "
                 "Infrastructure Protection (CIP) Reliability Standard "
                 "CIP-003-11."),
    "publication_date": "2026-03-24",
    "agencies": [DOE_AGENCY, FERC_AGENCY],
    "effective_on": "2026-05-26",
    "comments_close_on": None,
}
# Modelled on federal_register:2025-18394 (supply-chain rule, names no CIP-###).
UNNAMED_STD_DOC = {
    "document_number": "2025-18394",
    "title": "Supply Chain Risk Management Reliability Standards Revisions",
    "type": "Rule",
    "abstract": ("The Commission directs NERC to develop new or modified "
                 "Reliability Standards addressing supply chain risk "
                 "management plans."),
    "publication_date": "2025-09-23",
    "agencies": [DOE_AGENCY, FERC_AGENCY],
    "effective_on": "2025-11-24",
    "comments_close_on": None,
}
# A proposed rule whose only anchor is a comment deadline: no compliance clock.
NO_CLOCK_DOC = {
    "document_number": "2026-09999",
    "title": ("Notice of Proposed Rulemaking; Critical Infrastructure "
              "Protection Reliability Standard CIP-015-1"),
    "type": "Proposed Rule",
    "abstract": "Comments are invited on the proposed Reliability Standard.",
    "publication_date": "2026-04-01",
    "agencies": [DOE_AGENCY, FERC_AGENCY],
    "effective_on": None,
    "comments_close_on": "2026-06-01",
}
# A NERC standards-page snapshot: same trigger, structurally different payload.
PAGE_SNAPSHOT = {
    "page_url": "https://www.nerc.com/pa/Stand/Pages/CIPStandards.aspx",
    "text": "CIP Standards Currently Enforced CIP-013-2 Supply Chain",
    "fetched_date": "2026-04-02",
}

CIP_PRODUCT_MAP = [
    ("CIP-003", "Security management controls", "purview;sentinel"),
    ("CIP-013", "Supply chain risk management", "def_easm;def_ti"),
]


def fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    for source_id in ("federal_register", "nerc_pages"):
        conn.execute(
            "INSERT INTO source_policies (source_id, name, evidence_rank) "
            "VALUES (?, ?, 1)", (source_id, source_id))
    for trigger_id in ("nerc_cip_revision", "tsa_security_directive",
                       "nerc_enforcement"):
        conn.execute(
            "INSERT INTO triggers (trigger_id, name, base_strength, "
            " decay_half_life_days, mvp_flag, evidence_quality, "
            " allowed_scopes) VALUES (?, ?, 5, 600, 1, 'IR', ?)",
            (trigger_id, trigger_id, json.dumps(["sector"])))
    for cip_standard, topic, product_ids in CIP_PRODUCT_MAP:
        conn.execute(
            "INSERT INTO cip_product_map (cip_standard, topic, product_ids, "
            " outreach_angle) VALUES (?, ?, ?, '')",
            (cip_standard, topic, product_ids))
    conn.commit()
    return conn


def add_signal(conn, signal_id, payload, source_id="federal_register",
               trigger_id="nerc_cip_revision", status="active"):
    """Store a raw event + the sector signal a classifier minted from it."""
    raw_event_id = f"{source_id}:{signal_id}"
    url = f"https://www.federalregister.gov/documents/{signal_id}"
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, source_native_id, "
        " fetched_at, event_date, payload, url) "
        "VALUES (?, ?, ?, '2026-08-15T00:00:00+00:00', ?, ?, ?)",
        (raw_event_id, source_id, signal_id,
         payload.get("publication_date") if isinstance(payload, dict) else "",
         json.dumps(payload), url))
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, "
        " signal_scope, trigger_id, event_date, headline, evidence_snippet, "
        " source_url, confidence, status) "
        "VALUES (?, ?, NULL, 'sector', ?, ?, 'headline', 'snippet', ?, 0.8, ?)",
        (signal_id, raw_event_id, trigger_id,
         payload.get("publication_date") if isinstance(payload, dict) else "",
         url, status))
    conn.commit()
    return signal_id


def obligations(conn):
    return conn.execute(
        "SELECT * FROM regulatory_obligations ORDER BY obligation_id"
    ).fetchall()


class DeriveObligationsTest(unittest.TestCase):
    def setUp(self):
        self.conn = fixture_conn()
        self.addCleanup(self.conn.close)

    def test_derives_a_row_per_signal_with_a_compliance_clock(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        add_signal(self.conn, "supply", UNNAMED_STD_DOC)
        counts = derive_obligations(self.conn)
        self.assertEqual(counts["obligations_new"], 2)
        rows = obligations(self.conn)
        self.assertEqual([r["obligation_id"] for r in rows],
                         ["obligation:cip003", "obligation:supply"])
        cip003 = rows[0]
        self.assertEqual(cip003["regulator"],
                         "Federal Energy Regulatory Commission")
        self.assertEqual(cip003["rule_name"], CIP_003_DOC["title"])
        self.assertEqual(cip003["effective_date"], "2026-05-26")
        self.assertEqual(rows[1]["effective_date"], "2025-11-24")
        self.assertEqual(
            cip003["source_url"],
            "https://www.federalregister.gov/documents/cip003")

    def test_mapped_products_only_for_standards_the_document_names(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        add_signal(self.conn, "supply", UNNAMED_STD_DOC)
        derive_obligations(self.conn)
        rows = obligations(self.conn)
        # CIP-003 appears verbatim in the title -> its mapped products.
        self.assertEqual(rows[0]["mapped_products"], "purview;sentinel")
        # The supply-chain rule names no CIP-### standard. Inferring CIP-013
        # from the topic wording would be a fabricated mapping (R4.1).
        self.assertIsNone(rows[1]["mapped_products"])

    def test_unsourced_fields_stay_null(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        row = obligations(self.conn)[0]
        # The fetched metadata carries no compliance date, and nothing here
        # was operator-verified.
        self.assertIsNone(row["compliance_date"])
        self.assertIsNone(row["verified_at"])
        self.assertTrue(row["derived_at"].endswith("+00:00"))

    def test_every_row_traces_back_to_a_stored_signal(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        add_signal(self.conn, "supply", UNNAMED_STD_DOC)
        derive_obligations(self.conn)
        orphans = self.conn.execute(
            "SELECT o.obligation_id FROM regulatory_obligations o "
            "LEFT JOIN signals s ON s.signal_id = o.signal_id "
            "WHERE o.signal_id IS NULL OR s.signal_id IS NULL").fetchall()
        self.assertEqual(orphans, [])

    def test_rerun_adds_no_duplicates(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        before = [tuple(r) for r in obligations(self.conn)]
        counts = derive_obligations(self.conn)
        self.assertEqual(counts["obligations_new"], 0)
        self.assertEqual(counts["obligations_existing"], 1)
        self.assertEqual([tuple(r) for r in obligations(self.conn)], before)

    def test_empty_store_produces_no_rows_and_does_not_crash(self):
        counts = derive_obligations(self.conn)
        self.assertEqual(counts,
                         {"obligations_new": 0, "obligations_existing": 0,
                          "signals_seen": 0, "skipped_no_clock": 0})
        self.assertEqual(obligations(self.conn), [])

    def test_signal_without_an_effective_date_yields_nothing(self):
        add_signal(self.conn, "nprm", NO_CLOCK_DOC)
        counts = derive_obligations(self.conn)
        self.assertEqual(counts["skipped_no_clock"], 1)
        self.assertEqual(obligations(self.conn), [])

    def test_page_snapshot_payload_is_skipped_not_crashed(self):
        add_signal(self.conn, "pagediff", PAGE_SNAPSHOT,
                   source_id="nerc_pages")
        counts = derive_obligations(self.conn)
        self.assertEqual(counts["signals_seen"], 1)
        self.assertEqual(counts["skipped_no_clock"], 1)
        self.assertEqual(obligations(self.conn), [])

    def test_enforcement_and_inactive_signals_are_not_obligations(self):
        # An enforcement notice is a past action, not a forward obligation.
        add_signal(self.conn, "enf", CIP_003_DOC,
                   trigger_id="nerc_enforcement")
        # A retracted signal must not leave a live deadline behind.
        add_signal(self.conn, "gone", CIP_003_DOC, status="retracted")
        counts = derive_obligations(self.conn)
        self.assertEqual(counts["signals_seen"], 0)
        self.assertEqual(obligations(self.conn), [])


class ApplicabilityTest(unittest.TestCase):
    """The obligation is class-scoped; applicability_rule is how a reader
    joins it to accounts (no signal has ever carried an entity_id)."""

    def setUp(self):
        self.conn = fixture_conn()
        self.addCleanup(self.conn.close)

    def test_applicability_rule_is_a_subsector_predicate(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        rule = obligations(self.conn)[0]["applicability_rule"]
        prefix, _, subsectors = rule.partition(":")
        self.assertEqual(prefix, "subsector_in")
        self.assertEqual(
            subsectors.split(";"),
            list(APPLICABILITY["nerc_cip_revision"][2]))

    def test_cip_scope_selects_electric_accounts_only(self):
        for entity_id, name, subsector in (
                ("E1", "An electric utility", "iou_electric"),
                ("E2", "A grid operator", "rto_iso"),
                ("E3", "A refiner", "refiner"),
                ("E4", "A gas pipeline", "midstream")):
            self.conn.execute(
                "INSERT INTO watchlist_entities (entity_id, name, subsector) "
                "VALUES (?, ?, ?)", (entity_id, name, subsector))
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        rule = obligations(self.conn)[0]["applicability_rule"]
        subsectors = rule.partition(":")[2].split(";")
        matched = [r["entity_id"] for r in self.conn.execute(
            "SELECT entity_id FROM watchlist_entities WHERE subsector IN "
            "({}) ORDER BY entity_id".format(", ".join("?" * len(subsectors))),
            subsectors)]
        self.assertEqual(matched, ["E1", "E2"])

    def test_scope_label_does_not_assert_registration(self):
        # NERC registration is not in the store; the label must hedge and
        # verified_at must stay NULL (R4.1).
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        row = obligations(self.conn)[0]
        self.assertIn("not verified per account", row["affected_scope"])
        self.assertIsNone(row["verified_at"])


class PipelineWiringTest(unittest.TestCase):
    def test_obligations_run_after_classifiers_and_before_scoring(self):
        pipeline = PIPELINE.read_text(encoding="utf-8")
        self.assertIn("app.obligations", pipeline,
                      "pipeline dropped the obligation producer (R8.3)")
        self.assertLess(
            pipeline.index("app.classify.regulatory"),
            pipeline.index("app.obligations"),
            "obligations derive from classified regulatory signals",
        )
        self.assertLess(
            pipeline.index("app.obligations"), pipeline.index("app.scoring"),
            "obligations do not depend on scores and run before scoring",
        )


if __name__ == "__main__":
    unittest.main()
