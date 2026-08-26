from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

from hub.config import Settings
from hub.domain import Purchase, PurchaseRequest, PurchaseState
from hub.providers.base import ProviderError, ProviderResult, ProviderState
from hub.repository import DuplicateSupplierResult, IdempotencyConflict


def settings(**overrides) -> Settings:
    base = Settings(
        database_url="postgresql://unused",
        clients={"seller": "s" * 32},
        data_secret="d" * 32,
        purchases_enabled=False,
        worker_poll_sec=1,
        lease_sec=60,
        max_status_checks=3,
        interhub_api_url="https://api.interhub.test",
        interhub_token="token",
        interhub_timeout_sec=20,
        interhub_ssl_verify=True,
        interhub_ca_cert_path="",
        interhub_proxy_url="",
        interhub_calculate_path="/calculate",
        interhub_check_path="/check",
        interhub_pay_path="/pay",
        interhub_check_status_path="/status",
        interhub_deposit_path="/deposit",
        interhub_pay_enabled=False,
    )
    return replace(base, **overrides)


class MemoryRepository:
    def __init__(self):
        self.items: dict[UUID, Purchase] = {}
        self.by_key: dict[tuple[str, str], UUID] = {}
        self.reject_duplicate_result = False

    def create_or_get(self, request: PurchaseRequest, fingerprint: str):
        key = (request.consumer_id, request.idempotency_key)
        existing_id = self.by_key.get(key)
        if existing_id:
            existing = self.items[existing_id]
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflict("Idempotency conflict")
            return existing, False
        purchase_id = uuid4()
        purchase = Purchase(
            id=purchase_id,
            consumer_id=request.consumer_id,
            idempotency_key=request.idempotency_key,
            provider_code=request.provider_code,
            service_id=request.service_id,
            account=request.account,
            params=request.params,
            request_fingerprint=fingerprint,
            provider_operation_id=f"hub-test-{purchase_id.hex}",
            state=PurchaseState.CREATED,
        )
        self.items[purchase_id] = purchase
        self.by_key[key] = purchase_id
        return purchase, True

    def get(self, purchase_id, consumer_id=None):
        item = self.items.get(purchase_id)
        if item and (consumer_id is None or item.consumer_id == consumer_id):
            return item
        return None

    def mark_checked(self, purchase_id, _lease_token, amount, result):
        item = self.items[purchase_id]
        item.state = PurchaseState.CHECKED
        item.amount = amount
        item.provider_status = result.status
        item.provider_message = result.message
        return item

    def mark_preflight_failed(self, purchase_id, _lease_token, message):
        item = self.items[purchase_id]
        item.state = PurchaseState.FAILED
        item.provider_message = message
        return item

    def mark_payment_started(self, purchase_id, _lease_token):
        item = self.items[purchase_id]
        if item.state != PurchaseState.CHECKED:
            raise RuntimeError("unsafe repeated pay")
        item.state = PurchaseState.PAYMENT_STARTED
        return item

    def mark_processing(self, purchase_id, _lease_token, message, *, status_check):
        item = self.items[purchase_id]
        item.state = PurchaseState.PROCESSING
        item.provider_message = message
        if status_check:
            item.status_check_attempts += 1
        return item

    def mark_provider_result(
        self,
        purchase_id,
        _lease_token,
        result,
        state,
        result_value,
        result_hash,
        _data_secret,
        *,
        status_check,
    ):
        if self.reject_duplicate_result and result_value:
            raise DuplicateSupplierResult("duplicate supplier result")
        item = self.items[purchase_id]
        item.state = state
        item.provider_status = result.status
        item.provider_message = result.message
        item.provider_transaction_id = result.provider_transaction_id
        item.result_ciphertext = result_value.encode("utf-8") if result_value else None
        item.result_hash = result_hash
        if status_check:
            item.status_check_attempts += 1
        return item

    def read_result(self, purchase_id, consumer_id, _data_secret):
        item = self.get(purchase_id, consumer_id)
        return item.result_ciphertext.decode("utf-8") if item and item.result_ciphertext else None

    def mark_requires_attention(self, purchase_id, _lease_token, message):
        item = self.items[purchase_id]
        item.state = PurchaseState.REQUIRES_ATTENTION
        item.provider_message = message
        return item


class FakeProvider:
    code = "interhub"

    def __init__(self):
        self.calculate_result = ProviderResult(
            ProviderState.SUCCEEDED,
            True,
            0,
            "calculated",
            fixed_amount=Decimal("10.50"),
        )
        self.check_result = ProviderResult(ProviderState.SUCCEEDED, True, 0, "checked")
        self.pay_results: list[ProviderResult | Exception] = []
        self.status_results: list[ProviderResult | Exception] = []
        self.pay_calls = 0
        self.status_calls = 0

    def services(self):
        return []

    def balance(self):
        return {}

    def calculate(self, _payload):
        return self.calculate_result

    def check(self, _payload):
        return self.check_result

    def pay(self, _provider_operation_id):
        self.pay_calls += 1
        value = self.pay_results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def check_status(self, _provider_operation_id):
        self.status_calls += 1
        value = self.status_results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def paid(code: str = "CODE-123") -> ProviderResult:
    return ProviderResult(
        ProviderState.SUCCEEDED,
        True,
        0,
        "paid",
        provider_transaction_id="provider-1",
        secret_value=code,
        public_payload={"params": {"gift_code": "[REDACTED]"}},
    )


def processing() -> ProviderResult:
    return ProviderResult(ProviderState.PROCESSING, True, 1, "processing")


__all__ = [
    "FakeProvider",
    "IdempotencyConflict",
    "MemoryRepository",
    "ProviderError",
    "paid",
    "processing",
    "settings",
]
