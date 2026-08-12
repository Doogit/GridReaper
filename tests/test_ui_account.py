"""Account 360 page tests (R8.3, R6.6, R7.6, R9.1).

Hermetic AppTest against a temp-file SQLite DB built by apply_migrations +
fixture rows (a shared file DB is required — :memory: can't be shared across
the connections the page opens). Covers: header (identifiers, coverage badge,
child relationship), child-sees-parent, an entity WITH an account signal
rendering a Timeline row + a Signals card, a dark zero-signal entity rendering
low-coverage framing with no crash on the empty tabs, and an entity selector
that never raises. Every case asserts no uncaught exception (AppTest.exception
is an ElementList in this Streamlit build, so we assert it is empty).
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from streamlit.testing.v1 import AppTest

from app.db.migrate import apply_migrations

PAGE = "app/ui/pages/3_Account_360.py"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def seed(conn):
    # triggers
    conn.execute("INSERT INTO triggers (trigger_id, name, base_strength, "
                 "decay_half_life_days) VALUES ('t_lead','Leadership',4,90)")

    # Acme (parent, edgar-visible, has ids + an account signal), its subsidiary
    # (child, dark), and a dark zero-signal muni.
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, cik, ticker, "
        "subsector, parent_id, richness, coverage_flag, gov_cloud_likelihood, "
        "tenant_cloud_environment) VALUES "
        "('E_ACME','Acme Energy','0000123','ACME','iou_electric',NULL,'high',"
        "'edgar-visible','possible','commercial')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, parent_id, "
        "richness, coverage_flag, gov_cloud_likelihood, tenant_cloud_environment)"
        " VALUES ('E_SUB','Acme Grid Sub','iou_electric','E_ACME','medium',"
        "'dark','unknown','unknown')")
    conn.execute(
        "INSERT INTO watchlist_entities (entity_id, name, subsector, parent_id, "
        "richness, coverage_flag, gov_cloud_likelihood, tenant_cloud_environment)"
        " VALUES ('E_DARK','Dark Muni Co','muni_public',NULL,'low','dark',"
        "'likely','gcc_high')")
    conn.execute(
        "INSERT INTO entity_relationships (parent_entity_id, child_entity_id, "
        "relationship_type) VALUES ('E_ACME','E_SUB','subsidiary')")

    # product + play + a primary fact for the snapshot provenance
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
        "'http://primary','2026-07-01')")

    # raw event -> account signal on E_ACME (evidence + snapshot)
    conn.execute(
        "INSERT INTO raw_events (raw_event_id, source_id, event_date, payload, "
        "url) VALUES ('re_acc',NULL,'2026-07-27',"
        "'{\"title\": \"Acme names new CISO\"}','http://acme/8k')")
    conn.execute(
        "INSERT INTO signals (signal_id, raw_event_id, entity_id, signal_scope, "
        "trigger_id, event_date, headline, evidence_snippet, source_url, "
        "confidence, evidence_quality, customer_facing_allowed, score, status) "
        "VALUES ('S_ACC1','re_acc','E_ACME','account','t_lead','2026-07-27',"
        "'Acme names new CISO','Acme names new CISO','http://acme/8k',0.9,'IR',"
        "1,4.0,'active')")
    conn.execute(
        "INSERT INTO signal_evidence (signal_id, raw_event_id, evidence_text, "
        "evidence_locator, evidence_rank) VALUES ('S_ACC1','re_acc',"
        "'Acme Energy appointed a new Chief Information Security Officer.',"
        "'para 2', 1)")
    conn.execute(
        "INSERT INTO license_play_snapshots (signal_id, play_id, fact_ids, "
        "generated_at, generation_version, display_text, outreach_safe_text) "
        "VALUES ('S_ACC1','play1','[\"f_primary\"]', ?, 'plays/1.0', "
        "'Recommended path: Adopt E5 grant', "
        "'Given the recent development, a licensing check may be timely.')",
        (iso(NOW),))

    # badge legend the card reads labels from
    conn.executemany(
        "INSERT INTO badge_legend (badge_kind, code, label, description) "
        "VALUES (?,?,?,?)",
        [("evidence_quality", "IR", "Investor Report", "From an investor filing"),
         ("source_quality", "non-primary", "Non-primary",
          "Not an authoritative source (R4.3)")])


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    seed(conn)
    conn.commit()
    conn.close()
    return path


class TestAccount360(unittest.TestCase):
    def setUp(self):
        self.path = make_db()
        os.environ["GRIDSIGNALS_DB"] = self.path

    def tearDown(self):
        os.environ.pop("GRIDSIGNALS_DB", None)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _run(self):
        return AppTest.from_file(PAGE, default_timeout=30).run()

    def _no_exc(self, at):
        self.assertEqual(
            list(at.exception), [],
            msg=[(e.type, e.message) for e in at.exception])

    def _select(self, at, label_contains):
        """Drive the entity selector to the option containing a substring."""
        opts = at.selectbox[0].options
        target = next(o for o in opts if label_contains in o)
        return at.selectbox[0].select(target).run()

    def _text(self, at):
        """All rendered markdown/caption/subheader/title text concatenated."""
        blobs = []
        for coll in (at.markdown, at.caption, at.subheader, at.title,
                     at.warning):
            for el in coll:
                blobs.append(str(el.value))
        return "\n".join(blobs)

    def test_default_selection_renders_without_exception(self):
        at = self._run()
        self._no_exc(at)
        # a deterministic first option (name-ordered: "Acme Energy") is selected
        self.assertTrue(len(at.selectbox[0].options) == 3)

    def test_header_shows_identifiers_subsector_coverage_and_child(self):
        at = self._run()
        at = self._select(at, "Acme Energy  ·  E_ACME")
        self._no_exc(at)
        text = self._text(at)
        self.assertIn("Acme Energy", text)
        self.assertIn("iou_electric", text)       # subsector badge
        self.assertIn("0000123", text)             # CIK identifier
        self.assertIn("possible", text)            # gov-cloud likelihood
        self.assertIn("Acme Grid Sub", text)       # child from relationships

    def test_child_page_shows_parent(self):
        at = self._run()
        at = self._select(at, "Acme Grid Sub  ·  E_SUB")
        self._no_exc(at)
        text = self._text(at)
        self.assertIn("Parent:", text)
        self.assertIn("Acme Energy", text)

    def test_entity_with_signal_renders_timeline_and_card(self):
        at = self._run()
        at = self._select(at, "Acme Energy  ·  E_ACME")
        self._no_exc(at)
        text = self._text(at)
        # timeline row + card headline both come from the seeded account signal
        self.assertIn("Acme names new CISO", text)
        # card provenance / play text proves render_signal_card ran
        self.assertIn("Adopt E5 grant", text)

    def test_dark_zero_signal_entity_low_coverage_no_crash(self):
        at = self._run()
        at = self._select(at, "Dark Muni Co  ·  E_DARK")
        self._no_exc(at)
        text = self._text(at)
        # useful header still present
        self.assertIn("Dark Muni Co", text)
        self.assertIn("muni_public", text)
        self.assertIn("likely", text)              # gov-cloud posture shown
        # low-coverage framing, never "no activity"
        self.assertIn("low coverage", text)
        # honest empty tabs, no exception
        self.assertIn("low volume by design", text)

    def test_selecting_across_entities_never_crashes(self):
        at = self._run()
        for label in ("Acme Energy  ·  E_ACME", "Acme Grid Sub  ·  E_SUB",
                      "Dark Muni Co  ·  E_DARK"):
            at = self._select(at, label)
            self._no_exc(at)


if __name__ == "__main__":
    unittest.main()
