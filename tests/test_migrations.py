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

    def test_observability_migration_adds_request_correlation(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / "migrations" / "20260826_02_observability.sql").read_text(encoding="utf-8")
        repository = (root / "api" / "hub" / "repository.py").read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS request_id", migration)
        self.assertIn("supplier_hub_purchase_events_request_idx", migration)
        self.assertIn("WHERE consumer_id=%s", repository)
        self.assertIn("stale_in_flight", repository)

    def test_operator_actions_have_separate_idempotency_and_audit_storage(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / "migrations" / "20260826_03_operator_reconciliation.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("supplier_hub.operator_actions", migration)
        self.assertIn("UNIQUE (operator_id, request_id)", migration)
        self.assertIn("WHERE state='requires_attention'", migration)


if __name__ == "__main__":
    unittest.main()
