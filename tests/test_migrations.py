"""Regression checks for immutable migration files."""
import hashlib
import os
import unittest


MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app",
    "db",
    "migrations",
)

EXPECTED_CHECKSUMS = {
    "0001_initial.sql":
        "4c5dcbe57463479b8b3aa6c83b7ef4451ccc7cd177925e37ed02a7f47f1b3b25",
    "0002_match_decision_parser_version.sql":
        "76385203f7b3190790f549bd11c7b967c5b83ee39b5c9a16b422968c299f34af",
}


class TestMigrationChecksums(unittest.TestCase):
    def test_applied_migration_files_are_immutable(self):
        for filename, expected in EXPECTED_CHECKSUMS.items():
            with self.subTest(migration=filename):
                path = os.path.join(MIGRATIONS_DIR, filename)
                with open(path, encoding="utf-8") as fh:
                    digest = hashlib.sha256(fh.read().encode("utf-8")).hexdigest()
                self.assertEqual(digest, expected)


if __name__ == "__main__":
    unittest.main()
