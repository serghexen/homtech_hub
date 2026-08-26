from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from hub.app import create_app, normalize_request_id, operator_purchase_out, required_request_id
from hub.domain import Purchase, PurchaseState
from tests.helpers import settings


class AppContractTests(unittest.TestCase):
    def test_request_id_accepts_safe_correlation_value(self):
        self.assertEqual(normalize_request_id("seller:order-1/item-2"), "seller:order-1/item-2")

    def test_request_id_is_generated_when_header_is_absent(self):
        self.assertIsInstance(UUID(normalize_request_id("")), UUID)

    def test_request_id_rejects_log_injection_and_oversized_values(self):
        for value in ("unsafe\nvalue", "a" * 129, " space"):
            with self.subTest(value=value[:20]):
                with self.assertRaises(ValueError):
                    normalize_request_id(value)

    def test_operator_decision_requires_explicit_request_id(self):
        with self.assertRaises(ValueError):
            required_request_id("")
        self.assertEqual(required_request_id("operator:decision-1"), "operator:decision-1")

    def test_operator_history_and_audited_result_routes_are_mounted(self):
        app = create_app(settings())
        routes = {(route.path, tuple(sorted(route.methods or ()))) for route in app.routes}
        self.assertIn(("/v1/operator/transactions", ("GET",)), routes)
        self.assertIn(("/v1/operator/purchases/{purchase_id}/events", ("GET",)), routes)
        self.assertIn(("/v1/operator/purchases/{purchase_id}/result", ("POST",)), routes)

    def test_operator_history_exposes_result_availability_but_not_plaintext(self):
        purchase = Purchase(
            id=UUID("b0a7b2f4-950d-4359-a718-0da94b651528"),
            consumer_id="seller",
            idempotency_key="order-1",
            request_id="seller:order-1",
            provider_code="interhub",
            service_id=11125,
            max_amount=Decimal("464.53"),
            account="",
            params={"nominal": "28632"},
            request_fingerprint="fingerprint",
            provider_operation_id="hub-interhub-1",
            state=PurchaseState.SUCCEEDED,
            amount=Decimal("464.53"),
            result_ciphertext=b"encrypted",
            created_at=datetime.now(timezone.utc),
        )

        output = operator_purchase_out(purchase)
        payload = output.model_dump() if hasattr(output, "model_dump") else output.dict()

        self.assertEqual(payload["nominal_id"], "28632")
        self.assertTrue(payload["result_available"])
        self.assertNotIn("result", payload)


if __name__ == "__main__":
    unittest.main()
