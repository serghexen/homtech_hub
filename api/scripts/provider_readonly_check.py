#!/usr/bin/env python3
"""Verify InterHub GET endpoints through Hub without exposing balance or secrets."""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hub.config import load_settings


def get_json(base_url: str, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    request = Request(base_url.rstrip("/") + path, headers=headers or {}, method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads((response.read() or b"{}").decode("utf-8"))
    except HTTPError as exc:
        return exc.code, None


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
    settings = load_settings()
    if settings.live_pay_allowed:
        raise RuntimeError("Read-only provider check refuses to run while live payments are enabled")
    if not settings.clients:
        raise RuntimeError("No Hub client is configured")
    client_id, client_key = next(iter(settings.clients.items()))
    headers = {"Accept": "application/json", "X-Hub-Client": client_id, "X-Hub-Key": client_key}

    status, ready = get_json(base_url, "/ready")
    if status != 200 or not isinstance(ready, dict) or ready.get("purchases_enabled") is not False:
        raise RuntimeError(f"Hub readiness/payment guard check failed with HTTP {status}")

    status, services = get_json(base_url, "/v1/providers/interhub/services", headers)
    items = services.get("items") if isinstance(services, dict) else None
    if status != 200 or not isinstance(items, list) or not items:
        raise RuntimeError(f"InterHub services check failed with HTTP {status}")

    status, balance = get_json(base_url, "/v1/providers/interhub/balance", headers)
    required_balance_keys = {"balance", "currency", "over_balance", "over_limit"}
    if status != 200 or not isinstance(balance, dict) or not required_balance_keys.issubset(balance):
        raise RuntimeError(f"InterHub balance check failed with HTTP {status}")

    currency = str(balance.get("currency") or "unknown")
    print(f"Read-only provider check passed: services={len(items)}, balance_currency={currency}, payments=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
