"""Non-reversible fingerprints for supplier results.

Encryption itself is performed by PostgreSQL pgcrypto inside the isolated Hub database.
"""

from __future__ import annotations

from hashlib import sha256


def value_hash(value: str) -> str:
    return sha256(f"supplier-result:v1:{value}".encode("utf-8")).hexdigest()
