"""InterHub HTTP adapter extracted from the working CRM integration."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import socket
import ssl
from typing import Any
import urllib.error
import urllib.request

from hub.config import Settings
from hub.providers.base import ProviderError, ProviderResult, ProviderState


SECRET_KEYS = {"gift_code", "code", "pin", "voucher", "password", "secret"}


def as_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def provider_state(success: bool, status: int) -> ProviderState:
    if success and status == 1:
        return ProviderState.PROCESSING
    if success and status == 0:
        return ProviderState.SUCCEEDED
    return ProviderState.FAILED


def extract_secret(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    params = payload.get("params")
    if isinstance(params, dict):
        for key in ("gift_code", "code", "pin", "voucher"):
            value = str(params.get(key) or "").strip()
            if value:
                return value
    for key in ("gift_code", "code", "pin", "voucher"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def redact_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        str(key): "[REDACTED]" if str(key).lower() in SECRET_KEYS else redact_payload(item)
        for key, item in value.items()
    }


class InterHubProvider:
    code = "interhub"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _ensure_configured(self) -> None:
        if not self.settings.interhub_api_url:
            raise ProviderError("InterHub API URL is not configured")
        if not self.settings.interhub_token:
            raise ProviderError("InterHub token is not configured")

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        self._ensure_configured()
        url = self.settings.interhub_api_url.rstrip("/") + path
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "token": self.settings.interhub_token,
        }
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="GET" if body is None else "POST")
        context = (
            ssl.create_default_context(cafile=self.settings.interhub_ca_cert_path or None)
            if self.settings.interhub_ssl_verify
            else ssl._create_unverified_context()
        )
        try:
            if self.settings.interhub_proxy_url:
                proxy = urllib.request.ProxyHandler(
                    {"http": self.settings.interhub_proxy_url, "https": self.settings.interhub_proxy_url}
                )
                opener = urllib.request.build_opener(proxy, urllib.request.HTTPSHandler(context=context))
                response_context = opener.open(request, timeout=self.settings.interhub_timeout_sec)
            else:
                response_context = urllib.request.urlopen(
                    request,
                    timeout=self.settings.interhub_timeout_sec,
                    context=context,
                )
            with response_context as response:
                raw = response.read() or b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProviderError("InterHub returned invalid JSON") from exc
        except urllib.error.HTTPError as exc:
            # Provider bodies can contain voucher material. Keep the durable error useful without persisting the body.
            raise ProviderError(f"InterHub {path} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"InterHub {path} is unavailable: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderError(f"InterHub {path} timed out") from exc

    @staticmethod
    def _normalize(payload: Any) -> ProviderResult:
        data = payload if isinstance(payload, dict) else {}
        success = bool(data.get("success"))
        try:
            status = int(data.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        return ProviderResult(
            state=provider_state(success, status),
            success=success,
            status=status,
            message=str(data.get("message") or "")[:2000],
            amount=as_decimal(data.get("amount")),
            fixed_amount=as_decimal(data.get("fixed_amount")),
            provider_transaction_id=str(data.get("transaction_id") or "")[:200],
            secret_value=extract_secret(data),
            public_payload=redact_payload(data),
        )

    @staticmethod
    def _collect_services(payload: Any, bucket: list[dict[str, Any]]) -> None:
        if isinstance(payload, list):
            for item in payload:
                InterHubProvider._collect_services(item, bucket)
            return
        if not isinstance(payload, dict):
            return
        if (payload.get("id") is not None or payload.get("service_id") is not None) and any(
            key in payload for key in ("name", "service_name", "type")
        ):
            bucket.append(payload)
        for key in ("data", "items", "services", "result"):
            nested = payload.get(key)
            if isinstance(nested, (list, dict)):
                InterHubProvider._collect_services(nested, bucket)

    def services(self) -> list[dict[str, Any]]:
        raw_services: list[dict[str, Any]] = []
        self._collect_services(self._request("/api/agent/service/list"), raw_services)
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        for service in raw_services:
            try:
                service_id = int(service.get("id") if service.get("id") is not None else service.get("service_id"))
            except (TypeError, ValueError):
                continue
            if service_id <= 0 or service_id in seen:
                continue
            seen.add(service_id)
            fields = []
            for raw_field in service.get("fields") if isinstance(service.get("fields"), list) else []:
                if not isinstance(raw_field, dict) or not str(raw_field.get("name") or raw_field.get("field") or "").strip():
                    continue
                fields.append(
                    {
                        "name": str(raw_field.get("name") or raw_field.get("field")).strip(),
                        "type": str(raw_field.get("type") or "TEXT").upper(),
                        "required": bool(raw_field.get("required")),
                        "value_list": raw_field.get("value_list") if isinstance(raw_field.get("value_list"), list) else [],
                    }
                )
            result.append(
                {
                    "service_id": service_id,
                    "title": str(service.get("name") or service.get("service_name") or f"Service #{service_id}").strip(),
                    "category": str(service.get("category_name") or service.get("category") or "").strip(),
                    "type": str(service.get("type") or "").upper(),
                    "min_amount": str(as_decimal(service.get("min_amount"))),
                    "max_amount": str(as_decimal(service.get("max_amount"))),
                    "fields": fields,
                }
            )
        return result

    def balance(self) -> dict[str, Any]:
        payload = self._request(self.settings.interhub_deposit_path)
        data = payload if isinstance(payload, dict) else {}
        currencies = {"643": "RUB", "949": "TRY", "840": "USD", "978": "EUR"}
        currency = str(data.get("currency") or "").upper()
        return {
            "balance": str(as_decimal(data.get("balance"))),
            "currency": currencies.get(currency, currency),
            "over_balance": str(as_decimal(data.get("over_balance"))),
            "over_limit": str(as_decimal(data.get("over_limit"))),
        }

    def calculate(self, payload: dict[str, Any]) -> ProviderResult:
        return self._normalize(self._request(self.settings.interhub_calculate_path, payload))

    def check(self, payload: dict[str, Any]) -> ProviderResult:
        return self._normalize(self._request(self.settings.interhub_check_path, payload))

    def pay(self, provider_operation_id: str) -> ProviderResult:
        return self._normalize(
            self._request(self.settings.interhub_pay_path, {"agent_transaction_id": provider_operation_id})
        )

    def check_status(self, provider_operation_id: str) -> ProviderResult:
        return self._normalize(
            self._request(self.settings.interhub_check_status_path, {"agent_transaction_id": provider_operation_id})
        )
