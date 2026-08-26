from __future__ import annotations

from pathlib import Path
import unittest


class MigrationTests(unittest.TestCase):
    def test_result_is_encrypted_by_pgcrypto_and_not_stored_as_text(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / "migrations" / "20260826_01_initial.sql").read_text(encoding="utf-8")
        repository = (root / "api" / "hub" / "repository.py").read_text(encoding="utf-8")
        self.assertIn("result_ciphertext bytea", migration)
        self.assertIn("pgp_sym_encrypt", repository)
        self.assertIn("pgp_sym_decrypt", repository)


if __name__ == "__main__":
    unittest.main()
