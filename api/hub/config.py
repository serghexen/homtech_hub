"""Environment-only configuration for the isolated Supplier Hub service."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class Settings:
    database_url: str
    clients: dict[str, str]
    operators: dict[str, str]
    data_secret: str
    purchases_enabled: bool
    worker_poll_sec: int
    lease_sec: int
    max_status_checks: int
    stale_after_sec: int
    interhub_api_url: str
    interhub_token: str
    interhub_timeout_sec: int
    interhub_ssl_verify: bool
    interhub_ca_cert_path: str
    interhub_proxy_url: str
    interhub_calculate_path: str
    interhub_check_path: str
    interhub_pay_path: str
    interhub_check_status_path: str
    interhub_deposit_path: str
    interhub_pay_enabled: bool

    @property
    def live_pay_allowed(self) -> bool:
        return self.purchases_enabled and self.interhub_pay_enabled

    def readiness_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.database_url:
            errors.append("DATABASE_URL is required")
        if len(self.data_secret) < 32:
            errors.append("SUPPLIER_HUB_DATA_SECRET must contain at least 32 characters")
        if not self.clients:
            errors.append("SUPPLIER_HUB_CLIENTS_JSON must configure at least one client")
        for client_id, secret in self.clients.items():
            if not client_id or len(secret) < 32:
                errors.append("each Supplier Hub client must have an id and a secret of at least 32 characters")
                break
        if not self.operators:
            errors.append("SUPPLIER_HUB_OPERATORS_JSON must configure at least one operator")
        for operator_id, secret in self.operators.items():
            if not operator_id or len(secret) < 32:
                errors.append("each Supplier Hub operator must have an id and a secret of at least 32 characters")
                break
        if self.live_pay_allowed and (not self.interhub_api_url or not self.interhub_token):
            errors.append("InterHub URL and token are required when live payments are enabled")
        return errors


def parse_clients(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): str(secret).strip() for key, secret in value.items() if str(key).strip()}


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "").strip(),
        clients=parse_clients(os.getenv("SUPPLIER_HUB_CLIENTS_JSON", "")),
        operators=parse_clients(os.getenv("SUPPLIER_HUB_OPERATORS_JSON", "")),
        data_secret=os.getenv("SUPPLIER_HUB_DATA_SECRET", "").strip(),
        purchases_enabled=env_bool("SUPPLIER_HUB_PURCHASES_ENABLED"),
        worker_poll_sec=env_int("SUPPLIER_HUB_WORKER_POLL_SEC", 5, 1, 300),
        lease_sec=env_int("SUPPLIER_HUB_LEASE_SEC", 60, 15, 900),
        max_status_checks=env_int("SUPPLIER_HUB_MAX_STATUS_CHECKS", 120, 1, 10_000),
        stale_after_sec=env_int("SUPPLIER_HUB_STALE_AFTER_SEC", 900, 60, 86_400),
        interhub_api_url=os.getenv("INTERHUB_API_URL", "https://api.interhub.ae").strip(),
        interhub_token=os.getenv("INTERHUB_TOKEN", "").strip(),
        interhub_timeout_sec=env_int("INTERHUB_TIMEOUT_SEC", 20, 5, 120),
        interhub_ssl_verify=env_bool("INTERHUB_SSL_VERIFY", True),
        interhub_ca_cert_path=os.getenv("INTERHUB_CA_CERT_PATH", "").strip(),
        interhub_proxy_url=os.getenv("INTERHUB_PROXY_URL", "").strip(),
        interhub_calculate_path=os.getenv("INTERHUB_CALCULATE_PATH", "/api/agent/payment/check/calculate").strip(),
        interhub_check_path=os.getenv("INTERHUB_CHECK_PATH", "/api/agent/payment/check").strip(),
        interhub_pay_path=os.getenv("INTERHUB_PAY_PATH", "/api/agent/payment/pay").strip(),
        interhub_check_status_path=os.getenv("INTERHUB_CHECK_STATUS_PATH", "/api/agent/payment/check_status").strip(),
        interhub_deposit_path=os.getenv("INTERHUB_DEPOSIT_PATH", "/api/agent/deposit").strip(),
        interhub_pay_enabled=env_bool("INTERHUB_PAY_ENABLED"),
    )
