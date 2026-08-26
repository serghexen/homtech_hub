"""Verify operator reconciliation against synthetic rows without contacting a supplier."""

from __future__ import annotations

from decimal import Decimal
import json
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg

from hub.config import load_settings
from hub.domain import PurchaseRequest
from hub.repository import PostgresPurchaseRepository
from hub.service import request_fingerprint


def request_json(
    base_url: str,
    path: str,
    *,
    operator_id: str = "",
    operator_key: str = "",
    client_id: str = "",
    client_key: str = "",
    request_id: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if operator_id:
        headers["X-Hub-Operator"] = operator_id
        headers["X-Hub-Operator-Key"] = operator_key
    if client_id:
        headers["X-Hub-Client"] = client_id
        headers["X-Hub-Key"] = client_key
    if request_id:
        headers["X-Request-ID"] = request_id
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(
        base_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads((exc.read() or b"{}").decode("utf-8"))


def synthetic_attention(repository: PostgresPurchaseRepository, label: str) -> UUID:
    request = PurchaseRequest(
        consumer_id="seller",
        idempotency_key=f"operator-smoke:{label}:{uuid4()}",
        request_id=f"operator-smoke:{uuid4()}",
        provider_code="interhub",
        service_id=1,
        max_amount=Decimal("1.00"),
        params={"synthetic_operator_smoke": True},
    )
    purchase, _ = repository.create_or_get(request, request_fingerprint(request))
    with psycopg.connect(repository.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE supplier_hub.purchases
                SET state='requires_attention', pay_started_at=now(), updated_at=now(),
                    last_error='synthetic operator smoke row'
                WHERE id=%s
                """,
                (purchase.id,),
            )
        connection.commit()
    return purchase.id


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
    settings = load_settings()
    if settings.live_pay_allowed or settings.purchases_enabled or settings.interhub_pay_enabled:
        raise RuntimeError("Operator smoke test refuses to run unless both payment switches are disabled")
    operator_id = "operator"
    operator_key = settings.operators.get(operator_id, "")
    client_key = settings.clients.get("seller", "")
    if not operator_key or not client_key:
        raise RuntimeError("Synthetic test credentials are not configured")

    repository = PostgresPurchaseRepository(settings.database_url)
    purchase_ids: list[UUID] = []
    synthetic_code = f"SYNTHETIC-ONLY-{uuid4()}"
    try:
        success_id = synthetic_attention(repository, "success")
        failed_id = synthetic_attention(repository, "failed")
        purchase_ids.extend([success_id, failed_id])

        status, _ = request_json(base_url, "/v1/operator/purchases")
        if status != 401:
            raise AssertionError(f"unauthorized operator list returned {status}, expected 401")

        status, items = request_json(
            base_url,
            "/v1/operator/purchases",
            operator_id=operator_id,
            operator_key=operator_key,
        )
        listed = {UUID(str(item["id"])) for item in items} if status == 200 else set()
        if success_id not in listed or failed_id not in listed:
            raise AssertionError("synthetic attention rows are missing from the operator queue")

        action_request_id = f"operator-smoke:resolve:{uuid4()}"
        success_payload = {
            "decision": "record_success",
            "reason": "Synthetic result recovered during isolated staging test",
            "result_value": synthetic_code,
        }
        status, resolved = request_json(
            base_url,
            f"/v1/operator/purchases/{success_id}/resolve",
            operator_id=operator_id,
            operator_key=operator_key,
            request_id=action_request_id,
            payload=success_payload,
        )
        if status != 200 or resolved["purchase"]["state"] != "succeeded" or not resolved["action_created"]:
            raise AssertionError(f"record_success returned {status}: {resolved}")
        if "result_value" in json.dumps(resolved):
            raise AssertionError("operator response exposed the recovered result field")

        status, repeated = request_json(
            base_url,
            f"/v1/operator/purchases/{success_id}/resolve",
            operator_id=operator_id,
            operator_key=operator_key,
            request_id=action_request_id,
            payload=success_payload,
        )
        if status != 200 or repeated["action_created"]:
            raise AssertionError("operator action retry was not idempotent")

        status, actions = request_json(
            base_url,
            f"/v1/operator/purchases/{success_id}/actions",
            operator_id=operator_id,
            operator_key=operator_key,
        )
        if status != 200 or len(actions) != 1 or actions[0]["decision"] != "record_success":
            raise AssertionError("operator audit history is missing the recorded decision")
        if synthetic_code in json.dumps(actions):
            raise AssertionError("operator audit history exposed the recovered result")

        status, result = request_json(
            base_url,
            f"/v1/purchases/{success_id}/result",
            client_id="seller",
            client_key=client_key,
        )
        if status != 200 or result.get("value") != synthetic_code:
            raise AssertionError("consumer could not retrieve the recovered encrypted result")

        status, failed = request_json(
            base_url,
            f"/v1/operator/purchases/{failed_id}/resolve",
            operator_id=operator_id,
            operator_key=operator_key,
            request_id=f"operator-smoke:failed:{uuid4()}",
            payload={
                "decision": "confirm_failed",
                "reason": "Synthetic confirmation that the provider did not charge",
            },
        )
        if status != 200 or failed["purchase"]["state"] != "failed":
            raise AssertionError(f"confirm_failed returned {status}: {failed}")

        with psycopg.connect(settings.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT result_ciphertext FROM supplier_hub.purchases WHERE id=%s",
                    (success_id,),
                )
                raw = bytes(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM supplier_hub.operator_actions WHERE purchase_id = ANY(%s)",
                    (purchase_ids,),
                )
                action_count = int(cursor.fetchone()[0])
            connection.commit()
        if synthetic_code.encode("utf-8") in raw:
            raise AssertionError("operator result is stored as plaintext")
        if action_count != 2:
            raise AssertionError(f"expected two operator audit actions, got {action_count}")
        print("operator synthetic smoke test passed; no supplier request was made")
    finally:
        if purchase_ids:
            with psycopg.connect(settings.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM supplier_hub.operator_actions WHERE purchase_id = ANY(%s)",
                        (purchase_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM supplier_hub.purchase_events WHERE purchase_id = ANY(%s)",
                        (purchase_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM supplier_hub.purchases WHERE id = ANY(%s)",
                        (purchase_ids,),
                    )
                connection.commit()


if __name__ == "__main__":
    main()
