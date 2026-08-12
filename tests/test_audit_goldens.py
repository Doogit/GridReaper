"""Golden-set regression harness tests (R9.10).

Hermetic: real migrations against in-memory SQLite, FK on, a canned judge
client returning scripted JudgeResults keyed on the fixture signal_id. No
network. Also loads the real seeds/audit_goldens.csv against a temp DB to prove
it parses and is idempotent (trap-2 guard: nothing delete+regenerates it).
"""
import json
import os
import sqlite3
import tempfile
import unittest

from app.audit.client import JudgeResult
from app.audit.goldens import (
    GOLDEN_PASS_THRESHOLD, load_goldens, run_goldens)
from app.db.connection import get_connection
from app.db.load_seeds import load
from app.db.migrate import apply_migrations

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."))
GOLDENS_CSV = os.path.join(REPO_ROOT, "seeds", "audit_goldens.csv")


def _verdicts(**results):
    """Build a JudgeResult verdicts dict from check=result kwargs."""
    return {check: {"result": result, "notes": ""}
            for check, result in results.items()}


def ok_result(**results):
    return JudgeResult("ok", _verdicts(**results),
                       "canned-model", 0.0, 0, 0, "")


class CannedClient:
    """Duck-typed judge client. Returns a scripted JudgeResult keyed on the
    record's signal_id; unknown ids return an error result."""

    def __init__(self, scripted):
        # scripted: signal_id -> JudgeResult
        self._scripted = scripted

    def judge(self, record):
        return self._scripted.get(
            record.get("signal_id"),
            JudgeResult("error", None, "canned-model", 0.0, 0, 0,
                        "no script for signal"))


def _fixture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(conn)
    return conn


def _insert_golden(conn, golden_id, record, expected):
    conn.execute(
        "INSERT INTO audit_goldens (golden_id, signal_fixture, "
        "expected_results, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?)",
        (golden_id, json.dumps(record), json.dumps(expected),
         "generated-for-review", "2026-08-11T00:00:00Z"))


# Two controlled, self-contained goldens so the harness tests are deterministic.
POS_RECORD = {"signal_id": "sig_pos", "headline": "clean positive"}
POS_EXPECTED = {"entity_match": "pass", "classification": "pass"}
NEG_RECORD = {"signal_id": "sig_neg", "headline": "misclassified"}
NEG_EXPECTED = {"classification": "fail", "license_play_support": "not_applicable"}


class TestLoadGoldens(unittest.TestCase):
    def test_parses_json_columns(self):
        conn = _fixture_conn()
        _insert_golden(conn, "g_pos", POS_RECORD, POS_EXPECTED)
        goldens = load_goldens(conn)
        conn.close()
        self.assertEqual(len(goldens), 1)
        g = goldens[0]
        self.assertEqual(g["golden_id"], "g_pos")
        self.assertEqual(g["record"], POS_RECORD)
        self.assertEqual(g["expected"], POS_EXPECTED)
        self.assertEqual(g["reviewed_by"], "generated-for-review")

    def test_malformed_json_raises_with_golden_id(self):
        conn = _fixture_conn()
        conn.execute(
            "INSERT INTO audit_goldens (golden_id, signal_fixture, "
            "expected_results, reviewed_by, reviewed_at) VALUES "
            "('g_bad', '{not json', '{}', 'x', 'y')")
        with self.assertRaises(ValueError) as ctx:
            load_goldens(conn)
        conn.close()
        self.assertIn("g_bad", str(ctx.exception))


class TestRunGoldens(unittest.TestCase):
    def _two_golden_conn(self):
        conn = _fixture_conn()
        _insert_golden(conn, "g_pos", POS_RECORD, POS_EXPECTED)
        _insert_golden(conn, "g_neg", NEG_RECORD, NEG_EXPECTED)
        return conn

    def test_all_match_reports_full_pass(self):
        conn = self._two_golden_conn()
        goldens = load_goldens(conn)
        conn.close()
        client = CannedClient({
            "sig_pos": ok_result(entity_match="pass", classification="pass"),
            "sig_neg": ok_result(classification="fail",
                                 license_play_support="not_applicable"),
        })
        report = run_goldens(client, goldens)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["errored"], [])
        self.assertEqual(report["pass_rate"], 1.0)
        self.assertTrue(report["meets_threshold"])
        self.assertEqual(report["threshold"], GOLDEN_PASS_THRESHOLD)

    def test_disagreement_reports_specific_failed_golden(self):
        conn = self._two_golden_conn()
        goldens = load_goldens(conn)
        conn.close()
        # Judge disagrees on g_neg's classification (says pass, expected fail).
        client = CannedClient({
            "sig_pos": ok_result(entity_match="pass", classification="pass"),
            "sig_neg": ok_result(classification="pass",
                                 license_play_support="not_applicable"),
        })
        report = run_goldens(client, goldens)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(len(report["failed"]), 1)
        failed = report["failed"][0]
        self.assertEqual(failed["golden_id"], "g_neg")
        self.assertEqual(failed["mismatches"],
                         [{"check": "classification",
                           "expected": "fail", "got": "pass"}])

    def test_meets_threshold_flips_around_threshold(self):
        # 10 goldens; 9 pass -> 0.90 meets; 8 pass -> 0.80 blocked.
        conn = _fixture_conn()
        for i in range(10):
            _insert_golden(conn, f"g{i}", {"signal_id": f"s{i}"},
                           {"classification": "pass"})
        goldens = load_goldens(conn)
        conn.close()

        def client_with_n_pass(n_pass):
            scripted = {}
            for i in range(10):
                res = "pass" if i < n_pass else "fail"
                scripted[f"s{i}"] = ok_result(classification=res)
            return CannedClient(scripted)

        r9 = run_goldens(client_with_n_pass(9), goldens)
        self.assertAlmostEqual(r9["pass_rate"], 0.90)
        self.assertTrue(r9["meets_threshold"])

        r8 = run_goldens(client_with_n_pass(8), goldens)
        self.assertAlmostEqual(r8["pass_rate"], 0.80)
        self.assertFalse(r8["meets_threshold"])

    def test_errored_status_counts_as_not_passed(self):
        conn = self._two_golden_conn()
        goldens = load_goldens(conn)
        conn.close()
        # g_neg's judge errors (e.g. transport/parse failure).
        client = CannedClient({
            "sig_pos": ok_result(entity_match="pass", classification="pass"),
            "sig_neg": JudgeResult("error", None, "canned-model", 0.0, 0, 0,
                                   "parse error"),
        })
        report = run_goldens(client, goldens)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(len(report["errored"]), 1)
        self.assertEqual(report["errored"][0]["golden_id"], "g_neg")
        self.assertEqual(report["errored"][0]["status"], "error")
        self.assertEqual(report["pass_rate"], 0.5)
        self.assertFalse(report["meets_threshold"])


class TestRealSeedLoadsAndIsIdempotent(unittest.TestCase):
    """Trap-2 guard: the real CSV loads via the loader with matching counts and
    a re-run does not duplicate or destructively regenerate audit_goldens."""

    def _csv_row_count(self):
        import csv
        with open(GOLDENS_CSV, newline="", encoding="utf-8") as fh:
            return sum(1 for _ in csv.DictReader(fh))

    def test_real_csv_loads_and_reloads_stable(self):
        expected_n = self._csv_row_count()
        self.assertGreaterEqual(expected_n, 6)  # R9.10 spread
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "goldens_test.db")

            rc1 = load(db_path)
            self.assertEqual(rc1, 0)  # no mismatch, no FK skips
            conn = get_connection(db_path)
            n1 = conn.execute(
                "SELECT COUNT(*) FROM audit_goldens").fetchone()[0]
            conn.close()
            self.assertEqual(n1, expected_n)

            # Re-run: idempotent, no duplication (PK upsert), count stable.
            rc2 = load(db_path)
            self.assertEqual(rc2, 0)
            conn = get_connection(db_path)
            n2 = conn.execute(
                "SELECT COUNT(*) FROM audit_goldens").fetchone()[0]
            # Every golden's JSON columns parse.
            goldens = load_goldens(conn)
            conn.close()
            self.assertEqual(n2, expected_n)
            self.assertEqual(len(goldens), expected_n)


if __name__ == "__main__":
    unittest.main()
