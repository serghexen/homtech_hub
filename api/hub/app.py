"""FastAPI application for the internal Supplier Hub contract."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hmac import compare_digest
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from hub import __version__
from hub.config import Settings, load_settings
from hub.domain import Purchase, PurchaseRequest, valid_request_id
from hub.operator_service import OperatorDecision, OperatorService
from hub.providers.base import ProviderError
from hub.providers.interhub import InterHubProvider
from hub.repository import (
    DuplicateSupplierResult,
    IdempotencyConflict,
    OperatorActionConflict,
    PostgresPurchaseRepository,
    PurchaseNotAwaitingAttention,
)
from hub.service import PurchaseService


class PurchaseIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    provider_code: str = Field(default="interhub", min_length=1, max_length=40)
    service_id: int = Field(gt=0)
    max_amount: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    account: str = Field(default="", max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)
    quantity: int = Field(default=1, ge=1, le=1)


class PurchaseOut(BaseModel):
    id: UUID
    idempotency_key: str
    request_id: str
    provider_code: str
    service_id: int
    max_amount: str | None
    state: str
    amount: str | None
    provider_status: int | None
    provider_message: str
    result_available: bool
    blocks_fallback: bool
    status_check_attempts: int


class PurchaseResultOut(BaseModel):
    purchase_id: UUID
    value: str


class PurchaseEventOut(BaseModel):
    id: int
    request_id: str
    event_type: str
    from_state: str | None
    to_state: str | None
    created_at: datetime


class ObservabilitySummaryOut(BaseModel):
    total: int
    created: int
    checked: int
    payment_started: int
    processing: int
    succeeded: int
    failed: int
    requires_attention: int
    in_flight: int
    stale_in_flight: int
    oldest_in_flight_age_sec: int
    stale_after_sec: int


class OperatorPurchaseOut(BaseModel):
    id: UUID
    consumer_id: str
    request_id: str
    provider_code: str
    service_id: int
    max_amount: str | None
    provider_operation_id: str
    state: str
    amount: str | None
    provider_status: int | None
    provider_transaction_id: str
    status_check_attempts: int
    created_at: datetime | None
    updated_at: datetime | None
    pay_started_at: datetime | None
    completed_at: datetime | None
    last_error: str


class OperatorResolutionIn(BaseModel):
    decision: Literal["confirm_failed", "record_success"]
    reason: str = Field(min_length=3, max_length=1000)
    result_value: str = Field(default="", max_length=5000)


class OperatorResolutionOut(BaseModel):
    purchase: OperatorPurchaseOut
    action_created: bool


class OperatorActionOut(BaseModel):
    id: UUID
    operator_id: str
    request_id: str
    decision: str
    reason: str
    created_at: datetime


def normalize_request_id(value: str) -> str:
    request_id = value.strip()
    if not request_id:
        return str(uuid4())
    if request_id != value:
        raise ValueError("X-Request-ID must not contain surrounding whitespace")
    if not valid_request_id(request_id):
        raise ValueError("X-Request-ID must contain 1 to 128 safe characters")
    return request_id


def required_request_id(value: str) -> str:
    if not value:
        raise ValueError("X-Request-ID is required for operator decisions")
    return normalize_request_id(value)


def purchase_out(purchase: Purchase) -> PurchaseOut:
    return PurchaseOut(
        id=purchase.id,
        idempotency_key=purchase.idempotency_key,
        request_id=purchase.request_id,
        provider_code=purchase.provider_code,
        service_id=purchase.service_id,
        max_amount=str(purchase.max_amount) if purchase.max_amount is not None else None,
        state=str(purchase.state),
        amount=str(purchase.amount) if purchase.amount is not None else None,
        provider_status=purchase.provider_status,
        provider_message=purchase.provider_message,
        result_available=purchase.result_available,
        blocks_fallback=purchase.blocks_fallback,
        status_check_attempts=purchase.status_check_attempts,
    )


def operator_purchase_out(purchase: Purchase) -> OperatorPurchaseOut:
    return OperatorPurchaseOut(
        id=purchase.id,
        consumer_id=purchase.consumer_id,
        request_id=purchase.request_id,
        provider_code=purchase.provider_code,
        service_id=purchase.service_id,
        max_amount=str(purchase.max_amount) if purchase.max_amount is not None else None,
        provider_operation_id=purchase.provider_operation_id,
        state=str(purchase.state),
        amount=str(purchase.amount) if purchase.amount is not None else None,
        provider_status=purchase.provider_status,
        provider_transaction_id=purchase.provider_transaction_id,
        status_check_attempts=purchase.status_check_attempts,
        created_at=purchase.created_at,
        updated_at=purchase.updated_at,
        pay_started_at=purchase.pay_started_at,
        completed_at=purchase.completed_at,
        last_error=purchase.last_error,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or load_settings()
    repository = PostgresPurchaseRepository(configured.database_url)
    interhub = InterHubProvider(configured)
    service = PurchaseService(repository, {interhub.code: interhub}, configured)
    operator_service = OperatorService(repository, configured.data_secret)
    application = FastAPI(title="HomTech Supplier Hub", version=__version__)

    def authenticated_client(
        x_hub_client: str = Header(default=""),
        x_hub_key: str = Header(default=""),
    ) -> str:
        client_id = x_hub_client.strip()
        expected = configured.clients.get(client_id, "")
        if not expected or not compare_digest(expected, x_hub_key.strip()):
            raise HTTPException(status_code=401, detail="Invalid Supplier Hub credentials")
        return client_id

    def authenticated_operator(
        x_hub_operator: str = Header(default=""),
        x_hub_operator_key: str = Header(default=""),
    ) -> str:
        operator_id = x_hub_operator.strip()
        expected = configured.operators.get(operator_id, "")
        if not expected or not compare_digest(expected, x_hub_operator_key.strip()):
            raise HTTPException(status_code=401, detail="Invalid Supplier Hub operator credentials")
        return operator_id

    @application.get("/live")
    def live() -> dict[str, str]:
        return {"status": "ok", "service": "homtech-supplier-hub", "version": __version__}

    @application.get("/ready")
    def ready() -> dict[str, Any]:
        errors = configured.readiness_errors()
        if not errors:
            try:
                with psycopg.connect(configured.database_url, connect_timeout=3) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT to_regclass('supplier_hub.purchases')")
                        if cursor.fetchone()[0] is None:
                            errors.append("Supplier Hub database migrations are not applied")
            except Exception:
                errors.append("Supplier Hub database is unavailable")
        if errors:
            raise HTTPException(status_code=503, detail=errors)
        return {
            "status": "ready",
            "service": "homtech-supplier-hub",
            "purchases_enabled": configured.live_pay_allowed,
        }

    @application.get("/v1/providers/interhub/services")
    def provider_services(_client_id: str = Depends(authenticated_client)) -> dict[str, Any]:
        try:
            return {"items": interhub.services()}
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @application.get("/v1/providers/interhub/balance")
    def provider_balance(_client_id: str = Depends(authenticated_client)) -> dict[str, Any]:
        try:
            return interhub.balance()
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @application.post("/v1/purchases", response_model=PurchaseOut, status_code=202)
    def create_purchase(
        payload: PurchaseIn,
        response: Response,
        client_id: str = Depends(authenticated_client),
        x_request_id: str = Header(default=""),
    ) -> PurchaseOut:
        try:
            request_id = normalize_request_id(x_request_id)
            purchase, _created = service.enqueue(
                PurchaseRequest(
                    consumer_id=client_id,
                    idempotency_key=payload.idempotency_key.strip(),
                    request_id=request_id,
                    provider_code=payload.provider_code.strip().lower(),
                    service_id=payload.service_id,
                    max_amount=payload.max_amount,
                    account=payload.account,
                    params=payload.params,
                )
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["X-Request-ID"] = purchase.request_id
        return purchase_out(purchase)

    @application.get("/v1/purchases/{purchase_id}", response_model=PurchaseOut)
    def read_purchase(purchase_id: UUID, client_id: str = Depends(authenticated_client)) -> PurchaseOut:
        purchase = repository.get(purchase_id, client_id)
        if not purchase:
            raise HTTPException(status_code=404, detail="Purchase not found")
        return purchase_out(purchase)

    @application.get("/v1/purchases/{purchase_id}/events", response_model=list[PurchaseEventOut])
    def read_purchase_events(
        purchase_id: UUID,
        client_id: str = Depends(authenticated_client),
    ) -> list[PurchaseEventOut]:
        if not repository.get(purchase_id, client_id):
            raise HTTPException(status_code=404, detail="Purchase not found")
        return [PurchaseEventOut(**event) for event in repository.list_events(purchase_id, client_id)]

    @application.get("/v1/observability/summary", response_model=ObservabilitySummaryOut)
    def observability_summary(client_id: str = Depends(authenticated_client)) -> ObservabilitySummaryOut:
        values = repository.observability_summary(client_id, configured.stale_after_sec)
        return ObservabilitySummaryOut(**values, stale_after_sec=configured.stale_after_sec)

    @application.get("/v1/purchases/{purchase_id}/result", response_model=PurchaseResultOut)
    def read_purchase_result(
        purchase_id: UUID,
        client_id: str = Depends(authenticated_client),
    ) -> PurchaseResultOut:
        purchase = repository.get(purchase_id, client_id)
        if not purchase:
            raise HTTPException(status_code=404, detail="Purchase not found")
        if not purchase.result_available:
            raise HTTPException(status_code=409, detail="Purchase result is not available")
        try:
            value = repository.read_result(purchase.id, client_id, configured.data_secret)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Stored purchase result cannot be decrypted") from exc
        if value is None:
            raise HTTPException(status_code=409, detail="Purchase result is not available")
        return PurchaseResultOut(purchase_id=purchase.id, value=value)

    @application.get("/v1/operator/purchases", response_model=list[OperatorPurchaseOut])
    def operator_attention_list(
        limit: int = 50,
        offset: int = 0,
        _operator_id: str = Depends(authenticated_operator),
    ) -> list[OperatorPurchaseOut]:
        bounded_limit = max(1, min(limit, 100))
        bounded_offset = max(0, offset)
        return [
            operator_purchase_out(purchase)
            for purchase in repository.list_requires_attention(bounded_limit, bounded_offset)
        ]

    @application.get("/v1/operator/purchases/{purchase_id}", response_model=OperatorPurchaseOut)
    def operator_purchase_detail(
        purchase_id: UUID,
        _operator_id: str = Depends(authenticated_operator),
    ) -> OperatorPurchaseOut:
        purchase = repository.get(purchase_id)
        if not purchase:
            raise HTTPException(status_code=404, detail="Purchase not found")
        return operator_purchase_out(purchase)

    @application.post(
        "/v1/operator/purchases/{purchase_id}/resolve",
        response_model=OperatorResolutionOut,
    )
    def operator_resolve_purchase(
        purchase_id: UUID,
        payload: OperatorResolutionIn,
        operator_id: str = Depends(authenticated_operator),
        x_request_id: str = Header(default=""),
    ) -> OperatorResolutionOut:
        try:
            resolution = operator_service.resolve(
                purchase_id,
                operator_id,
                required_request_id(x_request_id),
                OperatorDecision(payload.decision),
                payload.reason,
                payload.result_value,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Purchase not found") from exc
        except (OperatorActionConflict, PurchaseNotAwaitingAttention, DuplicateSupplierResult) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return OperatorResolutionOut(
            purchase=operator_purchase_out(resolution.purchase),
            action_created=resolution.created,
        )

    @application.get(
        "/v1/operator/purchases/{purchase_id}/actions",
        response_model=list[OperatorActionOut],
    )
    def operator_purchase_actions(
        purchase_id: UUID,
        _operator_id: str = Depends(authenticated_operator),
    ) -> list[OperatorActionOut]:
        if not repository.get(purchase_id):
            raise HTTPException(status_code=404, detail="Purchase not found")
        return [
            OperatorActionOut(**action)
            for action in repository.list_operator_actions(purchase_id)
        ]

    return application


app = create_app()
