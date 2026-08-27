"""Apply the idempotent SISDEV import schema to a configured Neon database."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg


def main() -> None:
    database_url = (
        os.environ.get("SISDEV_MIGRATION_URL")
        or os.environ.get("DATABASE_URL_UNPOOLED")
        or os.environ.get("POSTGRES_URL_NON_POOLING")
    )
    if not database_url:
        raise SystemExit("SISDEV_MIGRATION_URL or DATABASE_URL_UNPOOLED is required")

    migration_name = sys.argv[1] if len(sys.argv) > 1 else "import_jobs.sql"
    migration_path = Path(migration_name)
    if not migration_path.is_absolute():
        candidate = Path(__file__).resolve().parent / migration_path
        migration_path = candidate if candidate.exists() else Path.cwd() / migration_path
    migration_path = migration_path.resolve()
    allowed = Path(__file__).resolve().parent
    migration_path.relative_to(allowed)
    if migration_path.suffix.lower() != ".sql" or not migration_path.is_file():
        raise SystemExit("Migration must be an existing .sql file in sql/")
    sql = migration_path.read_text(encoding="utf-8")
    with psycopg.connect(database_url) as connection:
        connection.execute(sql)
        connection.commit()
        if migration_path.name == "security_access.sql":
            security_tables = connection.execute(
                """SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema='public' AND table_name IN
                   ('app_users','user_scopes','auth_sessions','login_attempts','audit_log','backup_registry')"""
            ).fetchone()[0]
            user_count = connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0]
            print(
                f"migration_ok file={migration_path.name} "
                f"security_tables={security_tables} users={user_count}"
            )
            return
    print(f"migration_ok file={migration_path.name}")


if __name__ == "__main__":
    main()
