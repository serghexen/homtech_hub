#!/usr/bin/env python3
"""Safely copy the InterHub subset from Docker env JSON into a Hub env file.

The source JSON is read from stdin so provider credentials never appear in the
command line or an intermediate file. Payment kill switches are always forced
off by this script.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


INTERHUB_KEYS = (
    "INTERHUB_API_URL",
    "INTERHUB_TOKEN",
    "INTERHUB_TIMEOUT_SEC",
    "INTERHUB_SSL_VERIFY",
    "INTERHUB_CA_CERT_PATH",
    "INTERHUB_PROXY_URL",
    "INTERHUB_CALCULATE_PATH",
    "INTERHUB_CHECK_PATH",
    "INTERHUB_PAY_PATH",
    "INTERHUB_CHECK_STATUS_PATH",
    "INTERHUB_DEPOSIT_PATH",
)
FORCED_VALUES = {
    "SUPPLIER_HUB_PURCHASES_ENABLED": "false",
    "INTERHUB_PAY_ENABLED": "false",
}


def parse_docker_environment(raw: str) -> dict[str, str]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Docker environment input is not valid JSON") from exc
    if not isinstance(values, list):
        raise RuntimeError("Docker environment input must be a JSON list")
    result: dict[str, str] = {}
    for item in values:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            result[key] = value
    return result


def update_environment(content: str, source: dict[str, str]) -> tuple[str, list[str]]:
    if not source.get("INTERHUB_API_URL") or not source.get("INTERHUB_TOKEN"):
        raise RuntimeError("CRM InterHub URL or token is missing")

    replacements = {key: source[key] for key in INTERHUB_KEYS if key in source}
    replacements.update(FORCED_VALUES)
    lines = content.splitlines()
    seen: set[str] = set()
    updated: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else ""
        if key in replacements:
            updated.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            updated.append(line)
    for key, value in replacements.items():
        if key not in seen:
            updated.append(f"{key}={value}")
    return "\n".join(updated) + "\n", sorted(replacements)


def atomic_write(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, mode)
        handle.write(content)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    source = parse_docker_environment(sys.stdin.read())
    content = args.target.read_text(encoding="utf-8")
    updated, keys = update_environment(content, source)
    atomic_write(args.target, updated)
    print("Updated keys without displaying values: " + ", ".join(keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
