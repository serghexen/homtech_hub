from __future__ import annotations

from pathlib import Path
import unittest
from uuid import uuid4

from hub.domain import PurchaseState
from hub.operator_service import OperatorDecision, OperatorService
from tests.helpers import MemoryRepository


class OperatorServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.purchase_id = uuid4()
        purchase = self.repository.make_purchase(
            self.purchase_id,
            state=PurchaseState.REQUIRES_ATTENTION,
        )
        self.repository.items[purchase.id] = purchase
        self.service = OperatorService(self.repository, "d" * 32)

    def test_confirm_failed_is_idempotent(self):
        first = self.service.resolve(
            self.purchase_id,
            "operator",
            "operator:failed:1",
            OperatorDecision.CONFIRM_FAILED,
            "Confirmed with provider: no charge",
        )
        repeated = self.service.resolve(
            self.purchase_id,
            "operator",
            "operator:failed:1",
            OperatorDecision.CONFIRM_FAILED,
            "Confirmed with provider: no charge",
        )
        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.purchase.state, PurchaseState.FAILED)

    def test_record_success_requires_a_result_and_never_exposes_provider_dependency(self):
        with self.assertRaises(ValueError):
            self.service.resolve(
                self.purchase_id,
                "operator",
                "operator:success:1",
                OperatorDecision.RECORD_SUCCESS,
                "Recovered from provider console",
            )
        source = (Path(__file__).resolve().parents[1] / "api" / "hub" / "operator_service.py").read_text()
        self.assertNotIn("provider.pay", source)
        self.assertNotIn("check_status", source)

        resolution = self.service.resolve(
            self.purchase_id,
            "operator",
            "operator:success:1",
            OperatorDecision.RECORD_SUCCESS,
            "Recovered from provider console",
            "SYNTHETIC-CODE",
        )
        self.assertEqual(resolution.purchase.state, PurchaseState.SUCCEEDED)
        self.assertEqual(resolution.purchase.result_ciphertext, b"SYNTHETIC-CODE")

    def test_decision_validation_rejects_unsafe_payloads(self):
        with self.assertRaises(ValueError):
            self.service.resolve(
                self.purchase_id,
                "operator",
                "",
                OperatorDecision.CONFIRM_FAILED,
                "valid reason",
            )
        with self.assertRaises(ValueError):
            self.service.resolve(
                self.purchase_id,
                "operator",
                "operator:bad:1",
                OperatorDecision.CONFIRM_FAILED,
                "valid reason",
                "MUST-NOT-BE-HERE",
            )


if __name__ == "__main__":
    unittest.main()
