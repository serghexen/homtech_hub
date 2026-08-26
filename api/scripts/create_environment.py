"""Create a private, disabled-by-default environment file without printing secrets."""

from __future__ import annotations

import argparse
from pathlib import Path
import secrets


def token() -> str:
    return secrets.token_urlsafe(48)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--bind", default="127.0.0.1:8010")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite existing environment: {output}")

    database_password = token()
    seller_key = token()
    crm_key = token()
    data_secret = token()
    lines = [
        f"SUPPLIER_HUB_ENV_FILE={output.name}",
        f"SUPPLIER_HUB_BIND={args.bind}",
        "POSTGRES_DB=supplier_hub",
        "POSTGRES_USER=supplier_hub",
        f"POSTGRES_PASSWORD={database_password}",
        f"DATABASE_URL=postgresql://supplier_hub:{database_password}@postgres:5432/supplier_hub",
        f'SUPPLIER_HUB_CLIENTS_JSON={{"seller":"{seller_key}","crm":"{crm_key}"}}',
        f"SUPPLIER_HUB_DATA_SECRET={data_secret}",
        "SUPPLIER_HUB_PURCHASES_ENABLED=false",
        "SUPPLIER_HUB_WORKER_POLL_SEC=5",
        "SUPPLIER_HUB_LEASE_SEC=60",
        "SUPPLIER_HUB_MAX_STATUS_CHECKS=120",
        "INTERHUB_API_URL=https://api.interhub.ae",
        "INTERHUB_TOKEN=",
        "INTERHUB_TIMEOUT_SEC=20",
        "INTERHUB_SSL_VERIFY=true",
        "INTERHUB_CA_CERT_PATH=",
        "INTERHUB_PROXY_URL=",
        "INTERHUB_CALCULATE_PATH=/api/agent/payment/check/calculate",
        "INTERHUB_CHECK_PATH=/api/agent/payment/check",
        "INTERHUB_PAY_PATH=/api/agent/payment/pay",
        "INTERHUB_CHECK_STATUS_PATH=/api/agent/payment/check_status",
        "INTERHUB_DEPOSIT_PATH=/api/agent/deposit",
        "INTERHUB_PAY_ENABLED=false",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"created private environment at {output}")


if __name__ == "__main__":
    main()
