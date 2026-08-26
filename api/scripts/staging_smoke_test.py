"""Exercise API idempotency and PostgreSQL encryption without contacting a supplier."""

from __future__ import annotations

from decimal import Decimal
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg

from hub.config import load_settings
from hub.domain import PurchaseState
from hub.providers.base import ProviderResult, ProviderState
from hub.repository import PostgresPurchaseRepository


def request_json(
    base_url: str,
    path: str,
    *,
    client_id: str = "",
    client_key: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if client_id:
        headers["X-Hub-Client"] = client_id
        headers["X-Hub-Key"] = client_key
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(base_url.rstrip("/") + path, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads((exc.read() or b"{}").decode("utf-8"))


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
    settings = load_settings()
    if settings.live_pay_allowed:
        raise RuntimeError("Smoke test refuses to run while live payments are enabled")
    client_id = "seller"
    client_key = settings.clients.get(client_id, "")
    if not client_key:
        raise RuntimeError("seller smoke-test client is not configured")

    status, _ = request_json(base_url, "/v1/purchases/00000000-0000-0000-0000-000000000000")
    if status != 401:
        raise AssertionError(f"unauthorized request returned {status}, expected 401")

    idempotency_key = f"staging-smoke:{uuid4()}"
    payload = {
        "idempotency_key": idempotency_key,
        "provider_code": "interhub",
        "service_id": 1,
        "params": {"smoke_test": True},
    }
    purchase_id: UUID | None = None
    try:
        status, created = request_json(
            base_url,
            "/v1/purchases",
            client_id=client_id,
            client_key=client_key,
            payload=payload,
        )
        if status != 202 or created.get("state") != "created":
            raise AssertionError(f"create returned {status}: {created}")
        purchase_id = UUID(str(created["id"]))

        status, repeated = request_json(
            base_url,
            "/v1/purchases",
            client_id=client_id,
            client_key=client_key,
            payload=payload,
        )
        if status != 202 or repeated.get("id") != str(purchase_id):
            raise AssertionError("identical idempotent request did not return the original purchase")

        conflicting = {**payload, "service_id": 2}
        status, _ = request_json(
            base_url,
            "/v1/purchases",
            client_id=client_id,
            client_key=client_key,
            payload=conflicting,
        )
        if status != 409:
            raise AssertionError(f"idempotency conflict returned {status}, expected 409")

        repository = PostgresPurchaseRepository(settings.database_url)
        claimed = repository.claim_due(settings.lease_sec)
        if not claimed or claimed[0].id != purchase_id:
            raise AssertionError("created purchase was not leased by the PostgreSQL worker query")
        purchase, lease_token = claimed
        checked = ProviderResult(ProviderState.SUCCEEDED, True, 0, "smoke checked")
        purchase = repository.mark_checked(purchase.id, lease_token, Decimal("1.00"), checked)

        claimed = repository.claim_due(settings.lease_sec)
        if not claimed or claimed[0].id != purchase_id:
            raise AssertionError("checked purchase was not leased for payment transition")
        purchase, lease_token = claimed
        purchase = repository.mark_payment_started(purchase.id, lease_token)
        test_code = f"SMOKE-{uuid4()}"
        paid = ProviderResult(
            ProviderState.SUCCEEDED,
            True,
            0,
            "smoke paid",
            provider_transaction_id="smoke-provider-operation",
            secret_value=test_code,
            public_payload={"params": {"gift_code": "[REDACTED]"}},
        )
        purchase = repository.mark_provider_result(
            purchase.id,
            lease_token,
            paid,
            PurchaseState.SUCCEEDED,
            test_code,
            f"smoke-{uuid4().hex}",
            settings.data_secret,
            status_check=False,
        )
        if repository.read_result(purchase.id, client_id, settings.data_secret) != test_code:
            raise AssertionError("encrypted result did not decrypt to the original smoke-test value")
        with psycopg.connect(settings.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT result_ciphertext FROM supplier_hub.purchases WHERE id=%s", (purchase.id,))
                raw = bytes(cursor.fetchone()[0])
            connection.commit()
        if test_code.encode("utf-8") in raw:
            raise AssertionError("supplier result is stored as plaintext")
        print(f"staging smoke test passed for purchase {purchase.id}")
    finally:
        if purchase_id is not None:
            with psycopg.connect(settings.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM supplier_hub.purchase_events WHERE purchase_id=%s", (purchase_id,))
                    cursor.execute("DELETE FROM supplier_hub.purchases WHERE id=%s", (purchase_id,))
                connection.commit()


if __name__ == "__main__":
    main()
