"""Provider-independent purchase orchestration."""

from __future__ import annotations

from hashlib import sha256
from decimal import Decimal
import json
from typing import Mapping
from uuid import UUID

from hub.config import Settings
from hub.crypto import value_hash
from hub.domain import Purchase, PurchaseRequest, PurchaseState, valid_request_id
from hub.providers.base import ProviderError, ProviderResult, ProviderState, SupplierProvider
from hub.repository import DuplicateSupplierResult, PurchaseRepository


def request_fingerprint(request: PurchaseRequest) -> str:
    payload = {
        "provider_code": request.provider_code,
        "service_id": request.service_id,
        "max_amount": str(request.max_amount),
        "account": request.account,
        "params": request.params,
    }
    # Старые ваучерные fingerprint остаются побайтно совместимыми.
    if request.kind != "voucher":
        payload.update(kind=request.kind, requested_amount=str(request.requested_amount),
                       workspace_id=request.workspace_id, connection_id=request.connection_id)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


class PurchaseService:
    def __init__(
        self,
        repository: PurchaseRepository,
        providers: Mapping[str, SupplierProvider],
        settings: Settings,
    ):
        self.repository = repository
        self.providers = providers
        self.settings = settings

    def enqueue(self, request: PurchaseRequest) -> tuple[Purchase, bool]:
        # Проверяем заявку; прежняя цена сохраняется только для совместимости повторов.
        if request.provider_code not in self.providers:
            raise ValueError(f"Unknown provider: {request.provider_code}")
        if request.service_id <= 0:
            raise ValueError("service_id must be positive")
        if request.max_amount is not None and request.max_amount <= 0:
            raise ValueError("max_amount must be positive")
        if not request.idempotency_key.strip() or len(request.idempotency_key) > 200:
            raise ValueError("idempotency_key must contain 1 to 200 characters")
        if not valid_request_id(request.request_id):
            raise ValueError("request_id must contain 1 to 128 safe characters")
        if request.kind not in {"voucher", "steam_topup"}:
            raise ValueError("Unsupported purchase kind")
        if request.kind == "steam_topup":
            amount = request.requested_amount
            if (request.provider_code != "interhub" or request.service_id != 9361
                or not request.workspace_id or request.workspace_id <= 0 or request.connection_id is not None
                or request.params or not request.account.strip() or len(request.account) > 100
                or amount is None or not amount.is_finite() or amount < Decimal("16.99")
                or amount != amount.quantize(Decimal("0.01"))):
                raise ValueError("Invalid manual Steam topup")
            if not self.settings.topups_enabled or not self.repository.topup_allowed(request):
                raise ValueError("Steam topups are disabled for this workspace")
        elif request.requested_amount is not None or request.workspace_id is not None or request.connection_id is not None:
            raise ValueError("Topup fields require steam_topup kind")
        return self.repository.create_or_get(request, request_fingerprint(request))

    def process_claimed(self, purchase: Purchase, lease_token: UUID) -> Purchase:
        provider = self.providers.get(purchase.provider_code)
        if provider is None:
            if purchase.state == PurchaseState.CREATED:
                return self.repository.mark_preflight_failed(purchase.id, lease_token, "Provider is not configured")
            return self.repository.mark_requires_attention(purchase.id, lease_token, "Provider is not configured")

        if purchase.kind == "steam_topup" and purchase.state in {PurchaseState.CREATED, PurchaseState.CHECKED}:
            if not self.settings.topups_enabled or not self.repository.topup_allowed(purchase):
                self.repository.defer_topup(purchase.id, lease_token)
                return purchase
        if purchase.state == PurchaseState.CREATED:
            return self._preflight(purchase, lease_token, provider)
        if purchase.state == PurchaseState.CHECKED:
            return self._pay(purchase, lease_token, provider)
        if purchase.state in {PurchaseState.PAYMENT_STARTED, PurchaseState.PROCESSING}:
            return self._check_status(purchase, lease_token, provider)
        return purchase

    def _provider_request(self, purchase: Purchase, *, operation_id: str, amount: str | None = None) -> dict:
        payload = {
            "service_id": purchase.service_id,
            "account": purchase.account,
            "params": purchase.params,
            "agent_transaction_id": operation_id,
        }
        if amount is not None:
            payload["amount"] = amount
        return payload

    def _preflight(self, purchase: Purchase, lease_token: UUID, provider: SupplierProvider) -> Purchase:
        # Покупаем по актуальному расчёту поставщика, без сравнения с прежней ценой Seller.
        try:
            # TOP_UP повторяет рабочий контракт CRM: явная сумма в check, без calculate номинала.
            if purchase.kind == "steam_topup":
                amount = purchase.requested_amount
                checked = provider.check(self._provider_request(
                    purchase, operation_id=purchase.provider_operation_id, amount=str(amount)))
                if checked.public_payload.get("success") is not True or type(checked.public_payload.get("status")) is not int or checked.status != 0:
                    return self.repository.mark_preflight_failed(
                        purchase.id, lease_token, checked.message or "Steam account validation failed")
                return self.repository.mark_checked(purchase.id, lease_token, amount, checked)
            calculated = provider.calculate(
                self._provider_request(purchase, operation_id=f"{purchase.provider_operation_id}-calculate")
            )
            amount = calculated.fixed_amount
            if not calculated.success or amount <= 0:
                return self.repository.mark_preflight_failed(
                    purchase.id,
                    lease_token,
                    calculated.message or "Provider did not return a valid amount",
                )
            checked = provider.check(
                self._provider_request(
                    purchase,
                    operation_id=purchase.provider_operation_id,
                    amount=str(amount),
                )
            )
            if not checked.success:
                return self.repository.mark_preflight_failed(
                    purchase.id,
                    lease_token,
                    checked.message or "Provider rejected purchase validation",
                )
            return self.repository.mark_checked(purchase.id, lease_token, amount, checked)
        except ProviderError as exc:
            if purchase.kind == "steam_topup" and purchase.preflight_attempts < 4:
                return self.repository.retry_topup_check(purchase.id, lease_token, str(exc))
            return self.repository.mark_preflight_failed(purchase.id, lease_token, str(exc))

    def _pay(self, purchase: Purchase, lease_token: UUID, provider: SupplierProvider) -> Purchase:
        # Persist PAYMENT_STARTED before network I/O. From this point onward pay is never retried.
        started = self.repository.mark_payment_started(purchase.id, lease_token)
        try:
            result = provider.pay(started.provider_operation_id)
        except ProviderError as exc:
            return self.repository.mark_processing(started.id, lease_token, str(exc), status_check=False)
        return self._save_result(started, lease_token, result, status_check=False)

    def _check_status(self, purchase: Purchase, lease_token: UUID, provider: SupplierProvider) -> Purchase:
        if purchase.status_check_attempts >= self.settings.max_status_checks:
            return self.repository.mark_requires_attention(
                purchase.id,
                lease_token,
                "Provider status check limit reached; manual reconciliation is required",
            )
        try:
            result = provider.check_status(purchase.provider_operation_id)
        except ProviderError as exc:
            return self.repository.mark_processing(purchase.id, lease_token, str(exc), status_check=True)
        return self._save_result(purchase, lease_token, result, status_check=True)

    def _save_result(
        self,
        purchase: Purchase,
        lease_token: UUID,
        result: ProviderResult,
        *,
        status_check: bool,
    ) -> Purchase:
        if purchase.kind == "steam_topup" and (
            type(result.public_payload.get("success")) is not bool
            or type(result.public_payload.get("status")) is not int
        ):
            return self.repository.mark_processing(purchase.id, lease_token,
                "Unrecognized topup response; reconciliation required", status_check=status_check)
        state = {
            ProviderState.PROCESSING: PurchaseState.PROCESSING,
            ProviderState.SUCCEEDED: PurchaseState.SUCCEEDED,
            ProviderState.FAILED: PurchaseState.FAILED,
        }[result.state]
        result_value = ""
        digest = ""
        if state == PurchaseState.SUCCEEDED:
            if purchase.kind == "steam_topup":
                pass  # Успех пополнения подтверждается статусом поставщика, ваучера не будет.
            elif not result.secret_value:
                state = PurchaseState.PROCESSING
            else:
                result_value = result.secret_value
                digest = value_hash(result.secret_value)
        try:
            return self.repository.mark_provider_result(
                purchase.id,
                lease_token,
                result,
                state,
                result_value,
                digest,
                self.settings.data_secret,
                status_check=status_check,
            )
        except DuplicateSupplierResult as exc:
            return self.repository.mark_requires_attention(purchase.id, lease_token, str(exc))
