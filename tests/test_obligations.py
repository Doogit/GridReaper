"""Regulatory obligation producer tests (R8.3, R7.2, R4.1).

Hermetic: real migrations against in-memory SQLite, FK enforcement on, canned
Federal Register payloads modelled on the rows actually stored today. No
network. Focus is the derivation contract - which signals qualify, which
fields are sourced vs deliberately NULL, and that a re-run is a no-op.
"""
import csv
import json
import sqlite3
import unittest
from pathlib import Path

from app.classify import runner as classify_runner
from app.classify.regulatory import PARSER_VERSION as REGULATORY_PARSER_VERSION
from app.classify.regulatory import classify_federal_register
from app.db.migrate import apply_migrations
from app.obligations import (APPLICABILITY, applicability_rule,
                             derive_obligations, rederive_applicability)
from app.ui import data

SEEDS = Path(__file__).resolve().parent.parent / "seeds"

PIPELINE = (Path(__file__).resolve().parent.parent / "deploy"
            / "ingest_pipeline.sh")

FERC_AGENCY = {"raw_name": "Federal Energy Regulatory Commission",
               "name": "Federal Energy Regulatory Commission",
               "id": 167, "slug": "federal-energy-regulatory-commission"}
DOE_AGENCY = {"raw_name": "DEPARTMENT OF ENERGY", "name": "Energy Department",
              "id": 136, "slug": "energy-department"}
TSA_AGENCY = {"raw_name": "TRANSPORTATION SECURITY ADMINISTRATION",
              "name": "Transportation Security Administration",
              "id": 480, "slug": "transportation-security-administration"}

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
        subsector_clause, sep, exclude_clause = rule.partition("|")
        prefix, _, subsectors = subsector_clause.partition(":")
        self.assertEqual(prefix, "subsector_in")
        self.assertEqual(
            subsectors.split(";"),
            list(APPLICABILITY["nerc_cip_revision"][2]))
        self.assertEqual(sep, "|")
        self.assertEqual(
            exclude_clause,
            "exclude_entities:"
            + ";".join(APPLICABILITY["nerc_cip_revision"][3]))

    def test_rule_with_no_exclusions_carries_no_exclusion_clause(self):
        """An empty exclusion list is never written: the reader treats an
        empty clause as malformed and would drop the obligation entirely."""
        self.assertEqual(APPLICABILITY["tsa_security_directive"][3], ())
        add_signal(self.conn, "tsa", dict(CIP_003_DOC, agencies=[TSA_AGENCY]),
                   trigger_id="tsa_security_directive")
        derive_obligations(self.conn)
        rule = obligations(self.conn)[0]["applicability_rule"]
        self.assertEqual(rule, "subsector_in:lng;midstream")
        self.assertEqual(data.applicability_scope(rule),
                         ({"lng", "midstream"}, set()))

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
        subsectors = sorted(data.applicability_scope(rule)[0])
        matched = [r["entity_id"] for r in self.conn.execute(
            "SELECT entity_id FROM watchlist_entities WHERE subsector IN "
            "({}) ORDER BY entity_id".format(", ".join("?" * len(subsectors))),
            subsectors)]
        self.assertEqual(matched, ["E1", "E2"])

    def test_cip_scope_drops_the_equipment_storage_subsector(self):
        """``storage`` holds one battery-systems integrator — it supplies BES
        assets rather than owning or operating them."""
        self.assertNotIn("storage", APPLICABILITY["nerc_cip_revision"][2])

    def test_reader_parses_the_rule_this_producer_writes(self):
        """The producer composes ``subsector_in:...|exclude_entities:...`` and
        the Account 360 Compliance Calendar parses it back. The format literals
        are spelled out in both modules, so this is the test that binds them —
        a drift on either side fails here rather than silently emptying the
        tab (or silently widening the rule)."""
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        rule = obligations(self.conn)[0]["applicability_rule"]
        self.assertEqual(
            data.applicability_scope(rule),
            (set(APPLICABILITY["nerc_cip_revision"][2]),
             set(APPLICABILITY["nerc_cip_revision"][3])))

    def test_scope_label_does_not_assert_registration(self):
        # NERC registration is not in the store; the label must hedge and
        # verified_at must stay NULL (R4.1).
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        row = obligations(self.conn)[0]
        self.assertIn("not verified per account", row["affected_scope"])
        self.assertIsNone(row["verified_at"])


class CipApplicabilitySetTest(unittest.TestCase):
    """The narrowed CIP set, end to end and against the real watchlist.

    The affected_scope label says "Bulk Electric System owners and operators".
    "Registration not verified" hedges uncertainty about a plausible operator;
    it does not license admitting equipment makers, who have no BES role to be
    unverified about.
    """

    # Named here as well as in app/obligations.py so a silent edit to the
    # excluded list fails this test instead of quietly widening the rule.
    EQUIPMENT_MAKERS = ("E0155", "E0156", "E0158", "E0159", "E0160", "E0161",
                        "E0164")
    # The company each exclusion was judged against, as a distinctive
    # lowercase substring of the seed row's name. The exclusion is keyed by
    # entity_id but justified by WHO that id is; this is what binds the two.
    EQUIPMENT_MAKER_COMPANIES = {
        "E0155": "first solar",
        "E0156": "enphase",
        "E0158": "bloom energy",
        "E0159": "sunrun",
        "E0160": "array technologies",
        "E0161": "plug power",
        "E0164": "ge vernova",
    }
    # The renewables entities that stay in: project owners/operators.
    RENEWABLE_OPERATORS = ("E0152", "E0153", "E0154", "E0162", "E0163")
    ADMITTED_ENTITY_COUNT = 89
    WATCHLIST_ENTITY_COUNT = 172

    def setUp(self):
        self.conn = fixture_conn()
        self.addCleanup(self.conn.close)

    def _calendar(self, entity_id, name, subsector):
        self.conn.execute(
            "INSERT INTO watchlist_entities (entity_id, name, subsector) "
            "VALUES (?, ?, ?)", (entity_id, name, subsector))
        self.conn.commit()
        return data.account_obligations(self.conn, entity_id)

    def test_excluded_entity_list_is_exactly_the_equipment_makers(self):
        self.assertEqual(APPLICABILITY["nerc_cip_revision"][3],
                         self.EQUIPMENT_MAKERS)

    def test_each_exclusion_still_names_the_company_it_was_justified_against(
            self):
        """Binds each excluded id to WHO it is in the read-only seed.

        The exclusions are keyed by entity_id, but every one of them is a
        judgment about a company: this is an equipment maker, not a BES
        owner/operator. Nothing in the store carries that judgment - it lives
        in a trailing comment in app/obligations.py, which is documentation,
        not a constraint. Re-point one of these ids at a different company in
        the seed and the admitted count is still 88, the subsector is still
        'renewables', and every other test in this file still passes while a
        real operator's CIP obligation is silently suppressed. This is the
        test that turns that RED."""
        with (SEEDS / "watchlist_entities.csv").open(
                newline="", encoding="utf-8") as fh:
            by_id = {e["entity_id"]: e for e in csv.DictReader(fh)}

        for entity_id in self.EQUIPMENT_MAKERS:
            company = self.EQUIPMENT_MAKER_COMPANIES[entity_id]
            with self.subTest(entity_id=entity_id):
                row = by_id.get(entity_id)
                self.assertIsNotNone(
                    row,
                    f"{entity_id} is excluded from the CIP rule but no longer "
                    f"exists in the watchlist, so the exclusion now removes "
                    f"nobody. It was excluded as {company!r}; find where that "
                    f"company lives now and re-key the exclusion.")
                self.assertIn(
                    company, row["name"].lower(),
                    f"{entity_id} is {row['name']!r} in the seed, but the CIP "
                    f"exclusion was justified against {company!r}. The id has "
                    f"been re-pointed at a different company, so the rule is "
                    f"now suppressing an account nobody judged. Re-make the "
                    f"equipment-maker-vs-BES-operator call for {row['name']!r} "
                    f"and update app/obligations.py - this is a category "
                    f"judgment, not a number to update.")
                self.assertEqual(
                    row["subsector"], "renewables",
                    f"{entity_id} ({row['name']}) is now "
                    f"{row['subsector']!r}, not 'renewables'. The exclusion "
                    f"exists only because the 'renewables' label over-admits "
                    f"equipment makers; under another label it is either "
                    f"redundant or is narrowing a class it was never judged "
                    f"against.")

    def test_renewable_operator_still_sees_the_cip_obligation(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        # E0154 Ormat Technologies — a geothermal project owner/operator.
        cal = self._calendar("E0154", "A renewables generator", "renewables")
        self.assertEqual([o["obligation_id"] for o in cal["obligations"]],
                         ["obligation:cip003"])
        self.assertEqual(cal["unscoped"], 0)
        self.assertEqual(cal["excluded_by_entity"], 0)

    def test_equipment_maker_in_the_same_subsector_does_not(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        # E0159 Sunrun — same 'renewables' label, an installer not an operator.
        cal = self._calendar("E0159", "A solar installer", "renewables")
        # An honest empty calendar, not a crash and not an unscoped disclosure:
        # the rule WAS evaluated, and it does not bind this account. The
        # exclusion is counted so the tab can say why it differs from E0154's.
        self.assertEqual(cal, {"subsector": "renewables", "obligations": [],
                               "unscoped": 0, "excluded_by_entity": 1,
                               "total": 1})

    def test_storage_integrator_no_longer_matches(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        # E0157 Fluence Energy — the sole 'storage' member, an integrator.
        cal = self._calendar("E0157", "A storage integrator", "storage")
        self.assertEqual(cal["obligations"], [])
        self.assertEqual(cal["unscoped"], 0)
        # a class the rule never admitted, so nothing to disclose as an
        # individual exclusion — this empty tab really is "none for this class"
        self.assertEqual(cal["excluded_by_entity"], 0)

    def test_an_electric_utility_is_unaffected_by_the_narrowing(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        cal = self._calendar("E0001", "An electric utility", "iou_electric")
        self.assertEqual([o["obligation_id"] for o in cal["obligations"]],
                         ["obligation:cip003"])

    def test_narrowed_rule_admits_89_of_the_172_watchlist_entities(self):
        """Pins the count against the real seed CSV, so 96 -> 89 is measured
        rather than asserted. The seed file is read-only; this test reads it."""
        with (SEEDS / "watchlist_entities.csv").open(
                newline="", encoding="utf-8") as fh:
            entities = list(csv.DictReader(fh))
        self.assertEqual(len(entities), self.WATCHLIST_ENTITY_COUNT)

        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        rule = obligations(self.conn)[0]["applicability_rule"]
        admitted_subsectors, excluded = data.applicability_scope(rule)

        admitted = [e["entity_id"] for e in entities
                    if e["subsector"] in admitted_subsectors
                    and e["entity_id"] not in excluded]
        self.assertEqual(len(admitted), self.ADMITTED_ENTITY_COUNT)

        renewables = [e["entity_id"] for e in entities
                      if e["subsector"] == "renewables"]
        self.assertEqual(sorted(renewables),
                         sorted(self.RENEWABLE_OPERATORS
                                + self.EQUIPMENT_MAKERS))
        for entity_id in self.RENEWABLE_OPERATORS:
            self.assertIn(entity_id, admitted)
        for entity_id in self.EQUIPMENT_MAKERS:
            self.assertNotIn(entity_id, admitted)
        # 'storage' has exactly one member and the rule no longer names it.
        storage = [e["entity_id"] for e in entities
                   if e["subsector"] == "storage"]
        self.assertEqual(storage, ["E0157"])
        self.assertNotIn("storage", admitted_subsectors)


class RederiveApplicabilityTest(unittest.TestCase):
    """U20: applicability_rule/affected_scope are CODE-derived, not payload
    fields, so they get an explicit re-derive path instead of riding the
    payload-freezing insert-or-skip contract that protects regulator/
    rule_name/effective_date/mapped_products."""

    def setUp(self):
        self.conn = fixture_conn()
        self.addCleanup(self.conn.close)

    def test_refreshes_stale_fields_and_freezes_payload_fields(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        before = obligations(self.conn)[0]
        stale_rule = "subsector_in:stale_subsector"
        stale_scope = "a stale scope label from an old predicate version"
        self.conn.execute(
            "UPDATE regulatory_obligations "
            "SET applicability_rule = ?, affected_scope = ? "
            "WHERE obligation_id = ?",
            (stale_rule, stale_scope, before["obligation_id"]))
        self.conn.commit()

        counts = rederive_applicability(self.conn)
        self.assertEqual(counts,
                         {"rederived": 1, "unchanged": 0, "skipped": 0,
                          "rows_seen": 1})

        after = obligations(self.conn)[0]
        _, expected_scope, subsectors, excluded = (
            APPLICABILITY["nerc_cip_revision"])
        self.assertEqual(after["applicability_rule"],
                         applicability_rule(subsectors, excluded))
        self.assertEqual(after["affected_scope"], expected_scope)
        self.assertNotEqual(after["applicability_rule"], stale_rule)
        self.assertNotEqual(after["affected_scope"], stale_scope)
        # Payload fields and provenance are byte-identical.
        self.assertEqual(after["rule_name"], before["rule_name"])
        self.assertEqual(after["effective_date"], before["effective_date"])
        self.assertEqual(after["regulator"], before["regulator"])
        self.assertEqual(after["mapped_products"], before["mapped_products"])
        self.assertEqual(after["derived_at"], before["derived_at"])

    def test_bare_rerun_is_a_noop_when_nothing_changed(self):
        add_signal(self.conn, "cip003", CIP_003_DOC)
        derive_obligations(self.conn)
        before = [tuple(r) for r in obligations(self.conn)]

        counts = rederive_applicability(self.conn)
        self.assertEqual(counts,
                         {"rederived": 0, "unchanged": 1, "skipped": 0,
                          "rows_seen": 1})
        self.assertEqual([tuple(r) for r in obligations(self.conn)], before)

    def test_row_with_no_resolvable_signal_is_left_alone(self):
        # The 0013 migration made signal_id nullable specifically so ADD
        # COLUMN was legal on a populated table; a NULL row (or one whose
        # signal has since been retired/removed) is exactly the case this
        # narrow field refresh must skip rather than delete or error on.
        self.conn.execute(
            "INSERT INTO regulatory_obligations (obligation_id, source_url, "
            " regulator, rule_name, affected_scope, applicability_rule, "
            " effective_date, mapped_products, signal_id, derived_at) "
            "VALUES ('obligation:orphan', 'https://example.test/doc', "
            " 'Some Regulator', 'Some Rule', 'a hand-written scope label', "
            " 'subsector_in:iou_electric', '2026-01-01', NULL, NULL, "
            " '2026-01-01T00:00:00+00:00')")
        self.conn.commit()
        before = [tuple(r) for r in obligations(self.conn)]

        counts = rederive_applicability(self.conn)
        self.assertEqual(counts,
                         {"rederived": 0, "unchanged": 0, "skipped": 1,
                          "rows_seen": 1})
        self.assertEqual([tuple(r) for r in obligations(self.conn)], before)

    def test_normal_insert_or_skip_path_is_unaffected_by_rederive(self):
        # Adding --rederive must not change what a plain derive_obligations()
        # run does: still insert-or-skip, still zero rewrites on a re-run.
        add_signal(self.conn, "cip003", CIP_003_DOC)
        first = derive_obligations(self.conn)
        self.assertEqual(first["obligations_new"], 1)
        rederive_applicability(self.conn)
        second = derive_obligations(self.conn)
        self.assertEqual(second["obligations_new"], 0)
        self.assertEqual(second["obligations_existing"], 1)


class ClassifyToObligationEndToEndTest(unittest.TestCase):
    """R8/U7: the concrete proof that a real Federal Register document runs
    all the way through classify_federal_register -> run_classifier ->
    derive_obligations to a regulatory_obligations row - not just that a
    hand-inserted signal derives correctly (ApplicabilityTest above), but
    that the classifier itself now produces that signal from a raw event.
    """

    TSA_AGENCY = {"raw_name": "TRANSPORTATION SECURITY ADMINISTRATION",
                  "name": "Transportation Security Administration",
                  "slug": "transportation-security-administration"}

    def setUp(self):
        self.conn = fixture_conn()
        self.addCleanup(self.conn.close)

    def _fr_event(self, i, **overrides):
        doc = {
            "document_number": f"2026-0000{i}",
            "title": "", "type": "Rule", "abstract": None,
            "publication_date": "2026-08-11",
            "agencies": [self.TSA_AGENCY], "agency_names": [],
            "docket_ids": [], "cfr_references": [],
            "effective_on": None, "comments_close_on": None,
        }
        doc.update(overrides)
        raw_event_id = f"federal_register:{i}"
        self.conn.execute(
            "INSERT INTO raw_events (raw_event_id, source_id, event_date, "
            " payload, url, first_seen_at) "
            "VALUES (?, 'federal_register', ?, ?, ?, ?)",
            (raw_event_id, doc["publication_date"], json.dumps(doc),
             f"https://www.federalregister.gov/documents/{doc['document_number']}",
             f"2026-08-01T00:00:0{i}Z"))
        self.conn.commit()
        return raw_event_id

    def _classify(self):
        return classify_runner.run_classifier(
            self.conn, "regulatory", "federal_register",
            classify_federal_register, REGULATORY_PARSER_VERSION)

    def test_pipeline_security_directive_derives_an_lng_midstream_obligation(
            self):
        self._fr_event(
            1, title="Enhancing Pipeline Cyber Risk Management",
            abstract=("TSA is codifying security directive requirements "
                      "for pipeline owner/operators. Owner/operators must "
                      "establish cybersecurity requirements and report "
                      "incidents."),
            effective_on="2026-10-01")
        classify_counts = self._classify()
        self.assertEqual(classify_counts["signals_new"], 1)

        before = obligations(self.conn)
        self.assertEqual(before, [])
        derive_counts = derive_obligations(self.conn)
        self.assertEqual(derive_counts["obligations_new"], 1)

        after = obligations(self.conn)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["applicability_rule"],
                         "subsector_in:lng;midstream")
        self.assertEqual(after[0]["regulator"],
                         "Transportation Security Administration")

    def test_rail_only_security_directive_derives_no_obligation(self):
        # R4.1: TSA_TERMS alone would have matched this (it names "rail
        # security"), but it names no pipeline/LNG facility - the
        # classifier's pipeline/LNG gate (app/classify/tsa_sd.py) must drop
        # it before it ever reaches derive_obligations, not rely on
        # derive_obligations to filter it after the fact.
        self._fr_event(
            1, title="Enhancing Rail Cyber Risk Management",
            abstract=("TSA is codifying security directive requirements "
                      "for rail owner/operators. Owner/operators must "
                      "establish cybersecurity requirements and report "
                      "incidents."),
            effective_on="2026-10-01")
        classify_counts = self._classify()
        self.assertEqual(classify_counts["signals_new"], 0)

        derive_counts = derive_obligations(self.conn)
        self.assertEqual(derive_counts["signals_seen"], 0)
        self.assertEqual(obligations(self.conn), [])

    def test_rerun_of_classify_and_derive_is_idempotent(self):
        self._fr_event(
            1, title="Enhancing Pipeline Cyber Risk Management",
            abstract=("TSA is codifying security directive requirements "
                      "for pipeline owner/operators. Owner/operators must "
                      "establish cybersecurity requirements and report "
                      "incidents."),
            effective_on="2026-10-01")
        self._classify()
        derive_obligations(self.conn)
        before = [tuple(r) for r in obligations(self.conn)]

        # A bare rerun (no --force) skips events already bookkept at this
        # parser_version (app/classify/runner.py) - it reprocesses nothing,
        # rather than reprocessing and finding the signal "existing".
        second_classify = self._classify()
        self.assertEqual(second_classify["events_processed"], 0)
        self.assertEqual(second_classify["signals_new"], 0)
        second_derive = derive_obligations(self.conn)
        self.assertEqual(second_derive["obligations_new"], 0)
        self.assertEqual(second_derive["obligations_existing"], 1)
        self.assertEqual([tuple(r) for r in obligations(self.conn)], before)


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
