#!/usr/bin/env python3
"""Export minimal, irreversibly sanitized InterHub contract fixtures.

This script is designed to run inside the production CRM API container. It only
performs short read-only SQL queries and optional GET requests to the InterHub
catalog and balance endpoints. Raw provider responses are sanitized in memory
and are never written to disk or printed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import ssl
from typing import Any
import urllib.error
import urllib.request

import psycopg


SECRET_FRAGMENTS = (
    "account",
    "code",
    "credential",
    "gift",
    "password",
    "pin",
    "secret",
    "token",
    "transaction",
    "voucher",
)
IDENTIFIER_FRAGMENTS = ("id", "identifier", "reference")
AMOUNT_FRAGMENTS = ("amount", "balance", "commission", "comission", "limit", "price")
SAFE_ENUM_KEYS = {"currency", "state", "status", "success", "type", "required"}
MAX_LIST_ITEMS = 3


def _normalized_scalar(key: str, value: Any) -> Any:
    normalized_key = key.lower()
    if value is None or isinstance(value, bool):
        return value
    if normalized_key == "status":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if normalized_key == "success":
        return bool(value)
    if any(fragment in normalized_key for fragment in SECRET_FRAGMENTS):
        return "[REDACTED]"
    if any(fragment in normalized_key for fragment in IDENTIFIER_FRAGMENTS):
        return 1001 if isinstance(value, (int, float)) else "sample-id"
    if any(fragment in normalized_key for fragment in AMOUNT_FRAGMENTS):
        return 100 if isinstance(value, (int, float)) else "100"
    if normalized_key == "currency":
        currency = str(value).upper()
        return currency if currency in {"RUB", "USD", "EUR", "TRY", "643", "840", "949", "978"} else "RUB"
    if normalized_key == "message":
        return "Sample provider message"
    if normalized_key in {"name", "field"}:
        return "sample_field"
    if normalized_key in {"service_name", "title"}:
        return "Sample service"
    if normalized_key in {"category", "category_name"}:
        return "Sample category"
    if normalized_key == "type":
        value_type = str(value).upper()
        return value_type if value_type in {"TEXT", "SELECT", "NUMBER", "PHONE", "EMAIL"} else "TEXT"
    if isinstance(value, (int, float)):
        return 1
    if isinstance(value, str):
        return "sample-value"
    return value


def sanitize(value: Any, *, key: str = "") -> Any:
    """Preserve the JSON contract while replacing all potentially identifying values."""
    if isinstance(value, dict):
        return {str(item_key): sanitize(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, key=key) for item in value[:MAX_LIST_ITEMS]]
    return _normalized_scalar(key, value)


def assert_sanitized(value: Any) -> None:
    """Fail closed if a sensitive field retained any non-redacted scalar value."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if any(fragment in normalized_key for fragment in SECRET_FRAGMENTS):
                if isinstance(item, (str, int, float)) and item not in {"[REDACTED]", ""}:
                    raise RuntimeError(f"Sensitive fixture field was not redacted: {key}")
            assert_sanitized(item)
    elif isinstance(value, list):
        for item in value:
            assert_sanitized(item)


def _database_url() -> str:
    value = str(os.getenv("DATABASE_URL") or os.getenv("DB_DSN") or "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL or DB_DSN is required")
    return value


def load_database_samples() -> dict[str, Any]:
    samples: dict[str, Any] = {}
    query = """
        SELECT provider_response
        FROM app.interhub_transactions
        WHERE state = %s AND provider_response IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
    """
    with psycopg.connect(_database_url(), autocommit=False) as connection:
        with connection.cursor() as cursor:
            cursor.execute("BEGIN TRANSACTION READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '3s'")
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            for state in ("checked", "paid", "failed"):
                cursor.execute(query, (state,))
                row = cursor.fetchone()
                if row and isinstance(row[0], dict):
                    samples[state] = sanitize(row[0])
            connection.rollback()
    return samples


def _ssl_context() -> ssl.SSLContext:
    verify = str(os.getenv("INTERHUB_SSL_VERIFY", "true")).strip().lower() not in {"0", "false", "no", "off"}
    ca_path = str(os.getenv("INTERHUB_CA_CERT_PATH") or "").strip()
    return ssl.create_default_context(cafile=ca_path or None) if verify else ssl._create_unverified_context()


def _safe_get(path: str) -> Any:
    base_url = str(os.getenv("INTERHUB_API_URL") or "").strip()
    token = str(os.getenv("INTERHUB_TOKEN") or "").strip()
    if not base_url or not token:
        raise RuntimeError("InterHub URL and token are required for live GET fixtures")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"Accept": "application/json", "token": token},
        method="GET",
    )
    proxy_url = str(os.getenv("INTERHUB_PROXY_URL") or "").strip()
    context = _ssl_context()
    timeout = max(5, min(30, int(os.getenv("INTERHUB_TIMEOUT_SEC", "20"))))
    try:
        if proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}),
                urllib.request.HTTPSHandler(context=context),
            )
            response_context = opener.open(request, timeout=timeout)
        else:
            response_context = urllib.request.urlopen(request, timeout=timeout, context=context)
        with response_context as response:
            raw = response.read() or b"{}"
        return json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Safe InterHub GET failed for {path}") from exc


def load_live_get_samples() -> dict[str, Any]:
    deposit_path = str(os.getenv("INTERHUB_DEPOSIT_PATH") or "/api/agent/deposit").strip()
    return {
        "services": sanitize(_safe_get("/api/agent/service/list")),
        "balance": sanitize(_safe_get(deposit_path)),
    }


def write_fixtures(output_dir: Path, fixtures: dict[str, Any]) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, payload in fixtures.items():
        assert_sanitized(payload)
        destination = output_dir / f"{name}.json"
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(destination.name)
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--include-live-get", action="store_true")
    args = parser.parse_args()

    fixtures = load_database_samples()
    if args.include_live_get:
        fixtures.update(load_live_get_samples())
    written = write_fixtures(args.output, fixtures)
    print(f"Wrote {len(written)} sanitized fixture files: {', '.join(sorted(written))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
