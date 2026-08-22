"""Apply the idempotent SISDEV import schema to a configured Neon database."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg


def main() -> None:
    database_url = os.environ.get("SISDEV_MIGRATION_URL")
    if not database_url:
        raise SystemExit("SISDEV_MIGRATION_URL is required")

    sql = Path(__file__).with_name("import_jobs.sql").read_text(encoding="utf-8")
    with psycopg.connect(database_url) as connection:
        connection.execute(sql)
        connection.commit()
        counts = connection.execute(
            """SELECT
                 (SELECT COUNT(*) FROM import_batches),
                 (SELECT COUNT(*) FROM import_jobs),
                 (SELECT COUNT(*) FROM reconciliation_mappings)"""
        ).fetchone()
    print(
        "migration_ok "
        f"batches={counts[0]} jobs={counts[1]} mappings={counts[2]}"
    )


if __name__ == "__main__":
    main()
