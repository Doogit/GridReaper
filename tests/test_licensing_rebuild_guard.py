"""Pattern B guard for license_facts across a licensing rebuild (R8.7; R7.6).

license_facts is owned by app.licensing.rebuild(), not load_seeds. Once an
operator edits a fact's editable column or adds an operator fact, a rebuild()
must NOT clobber the edit or drop the added fact - only the matrix-derived
identity columns refresh. This is the license-facts analogue of the seed
loader's update_cols guard.

File-DB (not :memory:) because we seed the full matrix via load() then run
rebuild() twice against the same on-disk DB; :memory: would not persist. No
network - the loader only reads seed CSVs.
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

from app.db.load_seeds import load
from app.db.connection import get_connection
from app import licensing


class TestRebuildFactGuard(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db = os.path.join(self.tmpdir, "guard.db")
        load(db_path=self.db)                 # full seed matrix
        conn = get_connection(self.db)
        licensing.rebuild(conn)               # first build of license_facts
        conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_operator_edit_and_added_fact_survive_rebuild(self):
        conn = self._conn()
        # An edited fact (editable column) and an operator-added fact.
        edited = conn.execute(
            "SELECT fact_id, price_note FROM license_facts "
            "WHERE sku_or_plan IS NOT NULL ORDER BY fact_id LIMIT 1").fetchone()
        fid = edited["fact_id"]
        conn.execute("UPDATE license_facts SET price_note = 'OPERATOR EDIT', "
                     "verified_date = '2026-08-01' WHERE fact_id = ?", (fid,))
        # An unedited transform fact whose matrix-derived identity column we
        # CORRUPT directly, so the post-rebuild assertion is load-bearing: it can
        # only pass if the ON CONFLICT DO UPDATE genuinely refreshes segment.
        other = conn.execute(
            "SELECT fact_id, segment FROM license_facts "
            "WHERE fact_id != ? AND sku_or_plan IS NOT NULL "
            "ORDER BY fact_id LIMIT 1", (fid,)).fetchone()
        other_id, other_segment = other["fact_id"], other["segment"]
        conn.execute("UPDATE license_facts SET segment = 'WRONGSEG' "
                     "WHERE fact_id = ?", (other_id,))
        # Operator-added fact (sku_or_plan NULL is the operator marker).
        conn.execute(
            "INSERT INTO license_facts (fact_id, product_id, price_note) "
            "VALUES ('F_OP', NULL, 'operator only')")
        conn.commit()
        conn.close()

        conn = get_connection(self.db)
        licensing.rebuild(conn)               # second build must preserve edits
        conn.close()

        conn = self._conn()
        after_edit = conn.execute(
            "SELECT price_note, verified_date FROM license_facts "
            "WHERE fact_id = ?", (fid,)).fetchone()
        op = conn.execute(
            "SELECT price_note FROM license_facts WHERE fact_id='F_OP'").fetchone()
        after_other = conn.execute(
            "SELECT segment FROM license_facts WHERE fact_id = ?",
            (other_id,)).fetchone()
        conn.close()

        # Operator's edited editable columns survived the rebuild.
        self.assertEqual(after_edit["price_note"], "OPERATOR EDIT")
        self.assertEqual(after_edit["verified_date"], "2026-08-01")
        # Operator-added fact still exists (not dropped).
        self.assertIsNotNone(op)
        self.assertEqual(op["price_note"], "operator only")
        # The corrupted matrix-derived identity column was RESTORED by the
        # rebuild's DO UPDATE SET - proves identity columns genuinely refresh
        # (column-scoped guard), while the operator's editable-column edit above
        # stayed frozen. Together they lock the exact column partition.
        self.assertEqual(after_other["segment"], other_segment)
        self.assertNotEqual(after_other["segment"], "WRONGSEG")


if __name__ == "__main__":
    unittest.main()
