"""Apply immutable SQL migrations to the Supplier Hub database."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg


def main() -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        raise RuntimeError("No Supplier Hub migrations found")
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS supplier_hub")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS supplier_hub.schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            connection.commit()
        for path in files:
            version = path.stem
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM supplier_hub.schema_migrations WHERE version=%s", (version,))
                if cursor.fetchone():
                    continue
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO supplier_hub.schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING",
                    (version,),
                )
            connection.commit()
            print(f"applied {version}")


if __name__ == "__main__":
    main()
