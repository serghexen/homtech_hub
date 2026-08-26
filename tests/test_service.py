from __future__ import annotations

import unittest
from uuid import uuid4

from hub.domain import PurchaseRequest, PurchaseState
from hub.providers.base import ProviderError
from hub.service import PurchaseService
from tests.helpers import FakeProvider, IdempotencyConflict, MemoryRepository, paid, processing, settings


class PurchaseServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.provider = FakeProvider()
        self.settings = settings()
        self.service = PurchaseService(self.repository, {self.provider.code: self.provider}, self.settings)
        self.request = PurchaseRequest(
            consumer_id="seller",
            idempotency_key="order-1:item-1:unit-1",
            request_id="request-1",
            provider_code="interhub",
            service_id=100,
            params={"nominal": "10"},
        )

    def test_enqueue_is_idempotent_for_identical_request(self):
        first, created = self.service.enqueue(self.request)
        second, created_again = self.service.enqueue(self.request)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, second.id)

    def test_idempotent_retry_keeps_original_request_id(self):
        first, _ = self.service.enqueue(self.request)
        retried = PurchaseRequest(**{**self.request.__dict__, "request_id": "request-2"})
        second, created = self.service.enqueue(retried)
        self.assertFalse(created)
        self.assertEqual(first.request_id, "request-1")
        self.assertEqual(second.request_id, "request-1")

    def test_idempotency_key_rejects_changed_request(self):
        self.service.enqueue(self.request)
        changed = PurchaseRequest(**{**self.request.__dict__, "service_id": 101})
        with self.assertRaises(IdempotencyConflict):
            self.service.enqueue(changed)

    def test_unsafe_request_id_is_rejected_before_repository(self):
        unsafe = PurchaseRequest(**{**self.request.__dict__, "request_id": "unsafe\nrequest"})
        with self.assertRaises(ValueError):
            self.service.enqueue(unsafe)

    def test_pay_timeout_is_reconciled_without_second_pay(self):
        self.provider.pay_results = [ProviderError("timeout after pay")]
        self.provider.status_results = [paid("RECOVERED-CODE")]
        purchase, _ = self.service.enqueue(self.request)
        lease = uuid4()

        purchase = self.service.process_claimed(purchase, lease)
        self.assertEqual(purchase.state, PurchaseState.CHECKED)
        purchase = self.service.process_claimed(purchase, lease)
        self.assertEqual(purchase.state, PurchaseState.PROCESSING)
        purchase = self.service.process_claimed(purchase, lease)

        self.assertEqual(purchase.state, PurchaseState.SUCCEEDED)
        self.assertEqual(self.provider.pay_calls, 1)
        self.assertEqual(self.provider.status_calls, 1)
        self.assertEqual(
            self.repository.read_result(purchase.id, purchase.consumer_id, self.settings.data_secret),
            "RECOVERED-CODE",
        )

    def test_processing_result_blocks_fallback_until_status_finishes(self):
        self.provider.pay_results = [processing()]
        self.provider.status_results = [paid()]
        purchase, _ = self.service.enqueue(self.request)
        lease = uuid4()
        purchase = self.service.process_claimed(purchase, lease)
        purchase = self.service.process_claimed(purchase, lease)
        self.assertEqual(purchase.state, PurchaseState.PROCESSING)
        self.assertTrue(purchase.blocks_fallback)
        purchase = self.service.process_claimed(purchase, lease)
        self.assertEqual(purchase.state, PurchaseState.SUCCEEDED)

    def test_paid_without_code_keeps_polling(self):
        self.provider.pay_results = [paid("")]
        purchase, _ = self.service.enqueue(self.request)
        lease = uuid4()
        purchase = self.service.process_claimed(purchase, lease)
        purchase = self.service.process_claimed(purchase, lease)
        self.assertEqual(purchase.state, PurchaseState.PROCESSING)
        self.assertFalse(purchase.result_available)
        self.assertEqual(self.provider.pay_calls, 1)

    def test_preflight_failure_is_safe_terminal_failure(self):
        self.provider.calculate_result = self.provider.calculate_result.__class__(
            self.provider.calculate_result.state,
            False,
            0,
            "invalid service",
        )
        purchase, _ = self.service.enqueue(self.request)
        purchase = self.service.process_claimed(purchase, uuid4())
        self.assertEqual(purchase.state, PurchaseState.FAILED)
        self.assertFalse(purchase.blocks_fallback)
        self.assertEqual(self.provider.pay_calls, 0)

    def test_duplicate_supplier_code_requires_manual_reconciliation(self):
        self.repository.reject_duplicate_result = True
        self.provider.pay_results = [paid("DUPLICATE-CODE")]
        purchase, _ = self.service.enqueue(self.request)
        lease = uuid4()
        purchase = self.service.process_claimed(purchase, lease)
        purchase = self.service.process_claimed(purchase, lease)
        self.assertEqual(purchase.state, PurchaseState.REQUIRES_ATTENTION)
        self.assertTrue(purchase.blocks_fallback)
        self.assertEqual(self.provider.pay_calls, 1)


if __name__ == "__main__":
    unittest.main()
