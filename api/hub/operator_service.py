"""Local-only operator reconciliation; this module has no supplier dependency."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from uuid import UUID

from hub.crypto import value_hash
from hub.domain import Purchase, valid_request_id
from hub.repository import PurchaseRepository


class OperatorDecision(StrEnum):
    CONFIRM_FAILED = "confirm_failed"
    RECORD_SUCCESS = "record_success"


@dataclass(frozen=True)
class OperatorResolution:
    purchase: Purchase
    created: bool


def action_fingerprint(
    purchase_id: UUID,
    decision: OperatorDecision,
    reason: str,
    result_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "purchase_id": str(purchase_id),
            "decision": str(decision),
            "reason": reason,
            "result_hash": result_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class OperatorService:
    def __init__(self, repository: PurchaseRepository, data_secret: str):
        self.repository = repository
        self.data_secret = data_secret

    def resolve(
        self,
        purchase_id: UUID,
        operator_id: str,
        request_id: str,
        decision: OperatorDecision,
        reason: str,
        result_value: str = "",
    ) -> OperatorResolution:
        if not valid_request_id(request_id):
            raise ValueError("X-Request-ID must contain 1 to 128 safe characters")
        clean_reason = reason.strip()
        if len(clean_reason) < 3 or len(clean_reason) > 1000:
            raise ValueError("reason must contain 3 to 1000 characters")
        clean_result = result_value.strip()
        if decision == OperatorDecision.CONFIRM_FAILED and clean_result:
            raise ValueError("confirm_failed must not contain a supplier result")
        if decision == OperatorDecision.RECORD_SUCCESS and not clean_result:
            raise ValueError("record_success requires a recovered supplier result")
        if len(clean_result) > 5000:
            raise ValueError("supplier result must contain at most 5000 characters")

        digest = value_hash(clean_result) if clean_result else ""
        purchase, created = self.repository.resolve_attention(
            purchase_id,
            operator_id,
            request_id,
            action_fingerprint(purchase_id, decision, clean_reason, digest),
            str(decision),
            clean_reason,
            clean_result,
            digest,
            self.data_secret,
        )
        return OperatorResolution(purchase, created)
