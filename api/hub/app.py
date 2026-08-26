"""FastAPI application for the internal Supplier Hub contract."""

from __future__ import annotations

from hmac import compare_digest
from typing import Any
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from hub import __version__
from hub.config import Settings, load_settings
from hub.domain import Purchase, PurchaseRequest
from hub.providers.base import ProviderError
from hub.providers.interhub import InterHubProvider
from hub.repository import IdempotencyConflict, PostgresPurchaseRepository
from hub.service import PurchaseService


class PurchaseIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    provider_code: str = Field(default="interhub", min_length=1, max_length=40)
    service_id: int = Field(gt=0)
    account: str = Field(default="", max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)
    quantity: int = Field(default=1, ge=1, le=1)


class PurchaseOut(BaseModel):
    id: UUID
    idempotency_key: str
    provider_code: str
    service_id: int
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


def purchase_out(purchase: Purchase) -> PurchaseOut:
    return PurchaseOut(
        id=purchase.id,
        idempotency_key=purchase.idempotency_key,
        provider_code=purchase.provider_code,
        service_id=purchase.service_id,
        state=str(purchase.state),
        amount=str(purchase.amount) if purchase.amount is not None else None,
        provider_status=purchase.provider_status,
        provider_message=purchase.provider_message,
        result_available=purchase.result_available,
        blocks_fallback=purchase.blocks_fallback,
        status_check_attempts=purchase.status_check_attempts,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or load_settings()
    repository = PostgresPurchaseRepository(configured.database_url)
    interhub = InterHubProvider(configured)
    service = PurchaseService(repository, {interhub.code: interhub}, configured)
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
    def create_purchase(payload: PurchaseIn, client_id: str = Depends(authenticated_client)) -> PurchaseOut:
        try:
            purchase, _created = service.enqueue(
                PurchaseRequest(
                    consumer_id=client_id,
                    idempotency_key=payload.idempotency_key.strip(),
                    provider_code=payload.provider_code.strip().lower(),
                    service_id=payload.service_id,
                    account=payload.account,
                    params=payload.params,
                )
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return purchase_out(purchase)

    @application.get("/v1/purchases/{purchase_id}", response_model=PurchaseOut)
    def read_purchase(purchase_id: UUID, client_id: str = Depends(authenticated_client)) -> PurchaseOut:
        purchase = repository.get(purchase_id, client_id)
        if not purchase:
            raise HTTPException(status_code=404, detail="Purchase not found")
        return purchase_out(purchase)

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

    return application


app = create_app()
