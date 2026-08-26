"""PostgreSQL repository for durable, leased supplier purchases."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import json
from typing import Any, Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from hub.domain import Purchase, PurchaseRequest, PurchaseState


class IdempotencyConflict(ValueError):
    pass


class DuplicateSupplierResult(RuntimeError):
    pass


class PurchaseRepository(Protocol):
    def create_or_get(self, request: PurchaseRequest, fingerprint: str) -> tuple[Purchase, bool]: ...
    def get(self, purchase_id: UUID, consumer_id: str | None = None) -> Purchase | None: ...
    def claim_due(self, lease_sec: int) -> tuple[Purchase, UUID] | None: ...
    def mark_checked(self, purchase_id: UUID, lease_token: UUID, amount: Decimal, result: Any) -> Purchase: ...
    def mark_preflight_failed(self, purchase_id: UUID, lease_token: UUID, message: str) -> Purchase: ...
    def mark_payment_started(self, purchase_id: UUID, lease_token: UUID) -> Purchase: ...
    def mark_processing(self, purchase_id: UUID, lease_token: UUID, message: str, *, status_check: bool) -> Purchase: ...
    def mark_provider_result(
        self,
        purchase_id: UUID,
        lease_token: UUID,
        result: Any,
        state: PurchaseState,
        result_value: str,
        result_hash: str,
        data_secret: str,
        *,
        status_check: bool,
    ) -> Purchase: ...
    def mark_requires_attention(self, purchase_id: UUID, lease_token: UUID, message: str) -> Purchase: ...
    def read_result(self, purchase_id: UUID, consumer_id: str, data_secret: str) -> str | None: ...
    def list_events(self, purchase_id: UUID, consumer_id: str) -> list[dict[str, Any]]: ...
    def observability_summary(self, consumer_id: str, stale_after_sec: int) -> dict[str, Any]: ...


def _purchase(row: dict[str, Any]) -> Purchase:
    params = row.get("request_params")
    if isinstance(params, str):
        params = json.loads(params or "{}")
    return Purchase(
        id=row["id"],
        consumer_id=str(row["consumer_id"]),
        idempotency_key=str(row["idempotency_key"]),
        request_id=str(row.get("request_id") or row["id"]),
        provider_code=str(row["provider_code"]),
        service_id=int(row["service_id"]),
        account=str(row.get("account") or ""),
        params=params if isinstance(params, dict) else {},
        request_fingerprint=str(row["request_fingerprint"]),
        provider_operation_id=str(row["provider_operation_id"]),
        state=PurchaseState(str(row["state"])),
        amount=Decimal(str(row["amount"])) if row.get("amount") is not None else None,
        provider_status=int(row["provider_status"]) if row.get("provider_status") is not None else None,
        provider_message=str(row.get("provider_message") or ""),
        provider_transaction_id=str(row.get("provider_transaction_id") or ""),
        result_ciphertext=bytes(row["result_ciphertext"]) if row.get("result_ciphertext") is not None else None,
        result_hash=str(row.get("result_hash") or ""),
        status_check_attempts=int(row.get("status_check_attempts") or 0),
    )


class PostgresPurchaseRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    @staticmethod
    def _event(cursor, purchase_id: UUID, event_type: str, old: str | None, new: str | None) -> None:
        cursor.execute(
            """
            INSERT INTO supplier_hub.purchase_events(
                purchase_id, request_id, event_type, from_state, to_state, message
            )
            SELECT id, request_id, %s, %s, %s, ''
            FROM supplier_hub.purchases
            WHERE id=%s
            """,
            (event_type, old, new, purchase_id),
        )

    def create_or_get(self, request: PurchaseRequest, fingerprint: str) -> tuple[Purchase, bool]:
        purchase_id = uuid4()
        provider_operation_id = f"hub-{request.provider_code}-{purchase_id.hex}"
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO supplier_hub.purchases(
                        id, consumer_id, idempotency_key, request_id, request_fingerprint, provider_code,
                        service_id, account, request_params, provider_operation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (consumer_id, idempotency_key) DO NOTHING
                    RETURNING *
                    """,
                    (
                        purchase_id,
                        request.consumer_id,
                        request.idempotency_key,
                        request.request_id,
                        fingerprint,
                        request.provider_code,
                        request.service_id,
                        request.account,
                        json.dumps(request.params),
                        provider_operation_id,
                    ),
                )
                row = cursor.fetchone()
                created = row is not None
                if not row:
                    cursor.execute(
                        "SELECT * FROM supplier_hub.purchases WHERE consumer_id=%s AND idempotency_key=%s",
                        (request.consumer_id, request.idempotency_key),
                    )
                    row = cursor.fetchone()
                if not row:
                    raise RuntimeError("Purchase could not be created or loaded")
                if str(row["request_fingerprint"]) != fingerprint:
                    raise IdempotencyConflict("Idempotency key is already used with another request")
                if created:
                    self._event(cursor, purchase_id, "created", None, PurchaseState.CREATED)
            connection.commit()
        return _purchase(row), created

    def get(self, purchase_id: UUID, consumer_id: str | None = None) -> Purchase | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if consumer_id is None:
                    cursor.execute("SELECT * FROM supplier_hub.purchases WHERE id=%s", (purchase_id,))
                else:
                    cursor.execute(
                        "SELECT * FROM supplier_hub.purchases WHERE id=%s AND consumer_id=%s",
                        (purchase_id, consumer_id),
                    )
                row = cursor.fetchone()
            connection.commit()
        return _purchase(row) if row else None

    def list_events(self, purchase_id: UUID, consumer_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event.id, event.request_id, event.event_type,
                           event.from_state, event.to_state, event.created_at
                    FROM supplier_hub.purchase_events AS event
                    JOIN supplier_hub.purchases AS purchase ON purchase.id=event.purchase_id
                    WHERE event.purchase_id=%s AND purchase.consumer_id=%s
                    ORDER BY event.id
                    """,
                    (purchase_id, consumer_id),
                )
                rows = cursor.fetchall()
            connection.commit()
        return [dict(row) for row in rows]

    def observability_summary(self, consumer_id: str, stale_after_sec: int) -> dict[str, Any]:
        active_states = [
            str(PurchaseState.CREATED),
            str(PurchaseState.CHECKED),
            str(PurchaseState.PAYMENT_STARTED),
            str(PurchaseState.PROCESSING),
        ]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        count(*)::bigint AS total,
                        count(*) FILTER (WHERE state='created')::bigint AS created,
                        count(*) FILTER (WHERE state='checked')::bigint AS checked,
                        count(*) FILTER (WHERE state='payment_started')::bigint AS payment_started,
                        count(*) FILTER (WHERE state='processing')::bigint AS processing,
                        count(*) FILTER (WHERE state='succeeded')::bigint AS succeeded,
                        count(*) FILTER (WHERE state='failed')::bigint AS failed,
                        count(*) FILTER (WHERE state='requires_attention')::bigint AS requires_attention,
                        count(*) FILTER (WHERE state = ANY(%s))::bigint AS in_flight,
                        count(*) FILTER (
                            WHERE (
                                state IN ('created', 'checked')
                                AND next_attempt_at < now() - %s::interval
                            ) OR (
                                state IN ('payment_started', 'processing')
                                AND COALESCE(pay_started_at, created_at) < now() - %s::interval
                            )
                        )::bigint AS stale_in_flight,
                        COALESCE(
                            extract(epoch FROM (now() - min(created_at) FILTER (WHERE state = ANY(%s))))::bigint,
                            0
                        ) AS oldest_in_flight_age_sec
                    FROM supplier_hub.purchases
                    WHERE consumer_id=%s
                    """,
                    (
                        active_states,
                        timedelta(seconds=stale_after_sec),
                        timedelta(seconds=stale_after_sec),
                        active_states,
                        consumer_id,
                    ),
                )
                row = cursor.fetchone() or {}
            connection.commit()
        return {key: int(value or 0) for key, value in row.items()}

    def read_result(self, purchase_id: UUID, consumer_id: str, data_secret: str) -> str | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT pgp_sym_decrypt(result_ciphertext, %s)
                    FROM supplier_hub.purchases
                    WHERE id=%s AND consumer_id=%s AND state='succeeded' AND result_ciphertext IS NOT NULL
                    """,
                    (data_secret, purchase_id, consumer_id),
                )
                row = cursor.fetchone()
            connection.commit()
        return str(next(iter(row.values()))) if row else None

    def claim_due(self, lease_sec: int) -> tuple[Purchase, UUID] | None:
        lease_token = uuid4()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM supplier_hub.purchases
                        WHERE state IN ('created', 'checked', 'payment_started', 'processing')
                          AND next_attempt_at <= now()
                          AND (lease_until IS NULL OR lease_until < now())
                        ORDER BY next_attempt_at, created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE supplier_hub.purchases AS purchase
                    SET lease_token=%s, lease_until=now() + %s::interval, updated_at=now()
                    FROM candidate
                    WHERE purchase.id=candidate.id
                    RETURNING purchase.*
                    """,
                    (lease_token, timedelta(seconds=lease_sec)),
                )
                row = cursor.fetchone()
            connection.commit()
        return (_purchase(row), lease_token) if row else None

    def _transition(
        self,
        purchase_id: UUID,
        lease_token: UUID,
        *,
        allowed: tuple[PurchaseState, ...],
        target: PurchaseState,
        message: str = "",
        assignments: str = "",
        values: tuple[Any, ...] = (),
        release: bool = True,
    ) -> Purchase:
        allowed_values = tuple(str(item) for item in allowed)
        release_sql = ", lease_token=NULL, lease_until=NULL" if release else ""
        extra_sql = f", {assignments}" if assignments else ""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT state FROM supplier_hub.purchases
                    WHERE id=%s AND lease_token=%s AND state = ANY(%s)
                    FOR UPDATE
                    """,
                    (purchase_id, lease_token, list(allowed_values)),
                )
                current = cursor.fetchone()
                if not current:
                    raise RuntimeError("Purchase lease or state changed")
                old = str(current["state"])
                cursor.execute(
                    f"""
                    UPDATE supplier_hub.purchases
                    SET state=%s, provider_message=%s, updated_at=now(){extra_sql}{release_sql}
                    WHERE id=%s AND lease_token=%s
                    RETURNING *
                    """,
                    (str(target), message[:2000], *values, purchase_id, lease_token),
                )
                row = cursor.fetchone()
                self._event(cursor, purchase_id, "state_changed", old, str(target))
            connection.commit()
        if not row:
            raise RuntimeError("Purchase transition failed")
        return _purchase(row)

    def mark_checked(self, purchase_id: UUID, lease_token: UUID, amount: Decimal, result: Any) -> Purchase:
        return self._transition(
            purchase_id,
            lease_token,
            allowed=(PurchaseState.CREATED,),
            target=PurchaseState.CHECKED,
            message=result.message,
            assignments="amount=%s, provider_status=%s, provider_transaction_id=%s, provider_response=%s::jsonb, next_attempt_at=now()",
            values=(amount, result.status, result.provider_transaction_id, json.dumps(result.public_payload)),
        )

    def mark_preflight_failed(self, purchase_id: UUID, lease_token: UUID, message: str) -> Purchase:
        return self._transition(
            purchase_id,
            lease_token,
            allowed=(PurchaseState.CREATED,),
            target=PurchaseState.FAILED,
            message=message,
            assignments="last_error=%s, completed_at=now()",
            values=(message[:2000],),
        )

    def mark_payment_started(self, purchase_id: UUID, lease_token: UUID) -> Purchase:
        return self._transition(
            purchase_id,
            lease_token,
            allowed=(PurchaseState.CHECKED,),
            target=PurchaseState.PAYMENT_STARTED,
            assignments="pay_started_at=COALESCE(pay_started_at, now()), next_attempt_at=now() + interval '1 minute'",
            release=False,
        )

    def mark_processing(self, purchase_id: UUID, lease_token: UUID, message: str, *, status_check: bool) -> Purchase:
        attempt_sql = "status_check_attempts=status_check_attempts + 1, " if status_check else ""
        backoff_sql = (
            "CASE "
            "WHEN status_check_attempts < 3 THEN now() + interval '1 minute' "
            "WHEN status_check_attempts < 12 THEN now() + interval '5 minutes' "
            "ELSE now() + interval '30 minutes' END"
        )
        return self._transition(
            purchase_id,
            lease_token,
            allowed=(PurchaseState.PAYMENT_STARTED, PurchaseState.PROCESSING),
            target=PurchaseState.PROCESSING,
            message=message,
            assignments=f"{attempt_sql}last_error=%s, next_attempt_at={backoff_sql}",
            values=(message[:2000],),
        )

    def mark_provider_result(
        self,
        purchase_id: UUID,
        lease_token: UUID,
        result: Any,
        state: PurchaseState,
        result_value: str,
        result_hash: str,
        data_secret: str,
        *,
        status_check: bool,
    ) -> Purchase:
        attempt_sql = "status_check_attempts=status_check_attempts + 1, " if status_check else ""
        completed_sql = "now()" if state in {PurchaseState.SUCCEEDED, PurchaseState.FAILED} else "NULL"
        next_sql = (
            "CASE "
            "WHEN status_check_attempts < 3 THEN now() + interval '1 minute' "
            "WHEN status_check_attempts < 12 THEN now() + interval '5 minutes' "
            "ELSE now() + interval '30 minutes' END"
            if state == PurchaseState.PROCESSING
            else "now()"
        )
        try:
            return self._transition(
                purchase_id,
                lease_token,
                allowed=(PurchaseState.PAYMENT_STARTED, PurchaseState.PROCESSING),
                target=state,
                message=result.message,
                assignments=(
                    f"{attempt_sql}provider_status=%s, provider_transaction_id=%s, provider_response=%s::jsonb, "
                    "result_ciphertext=CASE WHEN %s<>'' THEN "
                    "pgp_sym_encrypt(%s, %s, 'cipher-algo=aes256, compress-algo=0') ELSE result_ciphertext END, "
                    f"result_hash=%s, last_error='', next_attempt_at={next_sql}, completed_at={completed_sql}"
                ),
                values=(
                    result.status,
                    result.provider_transaction_id,
                    json.dumps(result.public_payload),
                    result_value,
                    result_value,
                    data_secret,
                    result_hash,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateSupplierResult("Supplier returned a code already assigned to another purchase") from exc

    def mark_requires_attention(self, purchase_id: UUID, lease_token: UUID, message: str) -> Purchase:
        return self._transition(
            purchase_id,
            lease_token,
            allowed=(PurchaseState.PAYMENT_STARTED, PurchaseState.PROCESSING),
            target=PurchaseState.REQUIRES_ATTENTION,
            message=message,
            assignments="last_error=%s",
            values=(message[:2000],),
        )
