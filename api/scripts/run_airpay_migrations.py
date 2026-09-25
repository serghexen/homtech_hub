"""Применяет только новые SQL-миграции после безопасной базовой отметки старой схемы."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg


MIGRATIONS_LOCK = "airpay_schema_migrations"


def migration_checksum(content: str) -> str:
    # Считает контрольную сумму, чтобы уже применённый файл нельзя было незаметно изменить.
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def split_sql_statements(content: str) -> list[str]:
    # Делит SQL без разрыва строк, литералов и блоков DO $$...$$ для нетранзакционных миграций.
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    dollar_quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0

    while index < len(content):
        char = content[index]
        next_char = content[index + 1] if index + 1 < len(content) else ""

        if line_comment:
            current.append(char)
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            current.append(char)
            if char == "*" and next_char == "/":
                current.append(next_char)
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if dollar_quote:
            if content.startswith(dollar_quote, index):
                current.extend(dollar_quote)
                index += len(dollar_quote)
                dollar_quote = None
            else:
                current.append(char)
                index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                if next_char == quote:
                    current.append(next_char)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "-" and next_char == "-":
            current.extend((char, next_char))
            index += 2
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            current.extend((char, next_char))
            index += 2
            block_comment = True
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            index += 1
            continue
        if char == "$":
            closing = content.find("$", index + 1)
            tag = content[index : closing + 1] if closing >= 0 else ""
            if tag and all(part.isalnum() or part == "_" for part in tag[1:-1]):
                current.extend(tag)
                dollar_quote = tag
                index += len(tag)
                continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1

    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def is_no_transaction_migration(content: str) -> bool:
    # Разрешает CREATE/DROP INDEX CONCURRENTLY, которым PostgreSQL запрещает общую транзакцию.
    return content.lstrip().startswith("-- migrate:no-transaction")


def ensure_tracking_table(conn) -> bool:
    # Создаёт журнал один раз и возвращает признак первого запуска мигратора.
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('supplier_hub_airpay.schema_migrations')")
        is_first_run = cur.fetchone()[0] is None
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_hub_airpay.schema_migrations (
              migration_name text PRIMARY KEY,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    return is_first_run


def record_legacy_baseline(conn, root: Path) -> None:
    # Отмечает старые ручные SQL-файлы, не выполняя их повторно на рабочей базе.
    for migration_path in sorted(root.glob("*.sql")):
        content = migration_path.read_text(encoding="utf-8")
        migration_name = f"legacy/{migration_path.name}"
        checksum = migration_checksum(content)
        applied_checksum = read_applied_checksum(conn, migration_name)
        if applied_checksum:
            if applied_checksum != checksum:
                raise RuntimeError(f"Legacy migration was changed after baseline: {migration_name}")
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO supplier_hub_airpay.schema_migrations(migration_name, checksum)
                VALUES (%s, %s)
                ON CONFLICT (migration_name) DO NOTHING
                """,
                (migration_name, checksum),
            )


def read_applied_checksum(conn, migration_name: str) -> str | None:
    # Возвращает сохранённую сумму, чтобы повторный запуск был идемпотентным.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT checksum FROM supplier_hub_airpay.schema_migrations WHERE migration_name=%s",
            (migration_name,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def apply_runtime_migration(conn, migration_path: Path) -> bool:
    # Применяет новую миграцию ровно один раз и сохраняет отметку только после её успеха.
    content = migration_path.read_text(encoding="utf-8")
    checksum = migration_checksum(content)
    migration_name = f"runtime/{migration_path.name}"
    applied_checksum = read_applied_checksum(conn, migration_name)
    if applied_checksum:
        if applied_checksum != checksum:
            raise RuntimeError(f"Migration was changed after apply: {migration_name}")
        return False

    if is_no_transaction_migration(content):
        for statement in split_sql_statements(content):
            if statement.startswith("-- migrate:no-transaction"):
                statement = statement.split("\n", 1)[1] if "\n" in statement else ""
            if statement.strip():
                with conn.cursor() as cur:
                    cur.execute(statement)
    else:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(content)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO supplier_hub_airpay.schema_migrations(migration_name, checksum) VALUES (%s, %s)",
            (migration_name, checksum),
        )
    return True


def run_migrations(migrations_root: Path) -> int:
    # Сериализует миграции и ограничивает ожидание блокировок, не затрагивая данные приложения.
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")
    runtime_dir = migrations_root / "runtime"
    if not runtime_dir.is_dir():
        raise RuntimeError(f"Runtime migrations directory not found: {runtime_dir}")

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute('CREATE SCHEMA IF NOT EXISTS supplier_hub_airpay')
        with conn.cursor() as cur:
            cur.execute("SET lock_timeout TO '5s'")
            cur.execute("SET statement_timeout TO '15min'")
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (MIGRATIONS_LOCK,))
        try:
            is_first_run = ensure_tracking_table(conn)
            if is_first_run:
                print("Created migration tracking table.")
            # Повторяем идемпотентную отметку после сбоя, чтобы неполная первая базовая линия восстановилась.
            record_legacy_baseline(conn, migrations_root)
            if is_first_run:
                print("Legacy SQL migrations were marked as baseline.")
            for migration_path in sorted(runtime_dir.glob("*.sql")):
                if apply_runtime_migration(conn, migration_path):
                    print(f"Applied {migration_path.name}")
                else:
                    print(f"Already applied {migration_path.name}")
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (MIGRATIONS_LOCK,))
    return 0


def main() -> int:
    # Принимает путь тома с SQL-файлами, чтобы один образ работал в dev и production.
    parser = argparse.ArgumentParser(description="Apply Gamesales runtime database migrations")
    parser.add_argument("migrations_root", nargs="?", default="/migrations")
    args = parser.parse_args()
    return run_migrations(Path(args.migrations_root))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
