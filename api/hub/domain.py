"""Provider-independent purchase contracts and state rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID


class PurchaseState(StrEnum):
    CREATED = "created"
    CHECKED = "checked"
    PAYMENT_STARTED = "payment_started"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REQUIRES_ATTENTION = "requires_attention"


BLOCKS_FALLBACK = {
    PurchaseState.PAYMENT_STARTED,
    PurchaseState.PROCESSING,
    PurchaseState.SUCCEEDED,
    PurchaseState.REQUIRES_ATTENTION,
}


@dataclass(frozen=True)
class PurchaseRequest:
    consumer_id: str
    idempotency_key: str
    provider_code: str
    service_id: int
    account: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Purchase:
    id: UUID
    consumer_id: str
    idempotency_key: str
    provider_code: str
    service_id: int
    account: str
    params: dict[str, Any]
    request_fingerprint: str
    provider_operation_id: str
    state: PurchaseState
    amount: Decimal | None = None
    provider_status: int | None = None
    provider_message: str = ""
    provider_transaction_id: str = ""
    result_ciphertext: bytes | None = None
    result_hash: str = ""
    status_check_attempts: int = 0

    @property
    def result_available(self) -> bool:
        return self.state == PurchaseState.SUCCEEDED and self.result_ciphertext is not None

    @property
    def blocks_fallback(self) -> bool:
        return self.state in BLOCKS_FALLBACK
