"""Add an operator credential to an existing private environment without revealing it."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import tempfile


SETTING = "SUPPLIER_HUB_OPERATORS_JSON"


def ensure_operator(target: Path, operator_id: str) -> bool:
    target = target.resolve()
    if not target.is_file():
        raise RuntimeError(f"Environment file does not exist: {target}")
    original = target.read_text(encoding="utf-8")
    existing = [line for line in original.splitlines() if line.startswith(f"{SETTING}=")]
    if existing:
        try:
            configured = json.loads(existing[-1].split("=", 1)[1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Existing {SETTING} is invalid; refusing to replace it") from exc
        if not isinstance(configured, dict) or not configured:
            raise RuntimeError(f"Existing {SETTING} is empty; refusing to replace it")
        if any(not str(key).strip() or len(str(value)) < 32 for key, value in configured.items()):
            raise RuntimeError(f"Existing {SETTING} contains an invalid credential")
        return False

    value = json.dumps({operator_id: secrets.token_urlsafe(48)}, separators=(",", ":"))
    updated = original
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += f"{SETTING}={value}\n"
    mode = target.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(mode or 0o600)
    os.replace(temporary, target)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--operator-id", default="operator")
    args = parser.parse_args()
    changed = ensure_operator(Path(args.target), args.operator_id.strip())
    print("operator credential added" if changed else "operator credential already configured")


if __name__ == "__main__":
    main()
