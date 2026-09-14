"""Read-only storage diagnostics for the configured Neon database."""

from __future__ import annotations

import os

import psycopg


def main() -> None:
    database_url = (
        os.environ.get("SISDEV_MIGRATION_URL")
        or os.environ.get("DATABASE_URL_UNPOOLED")
        or os.environ.get("DATABASE_URL")
    )
    if not database_url:
        raise SystemExit("A database connection is required")
    with psycopg.connect(database_url) as connection:
        size = connection.execute(
            "SELECT pg_database_size(current_database()), pg_size_pretty(pg_database_size(current_database()))"
        ).fetchone()
        print(f"DATABASE bytes={size[0]} pretty={size[1]}")
        for row in connection.execute(
            """SELECT relname,pg_total_relation_size(relid),
                      pg_size_pretty(pg_total_relation_size(relid)),n_live_tup,n_dead_tup
               FROM pg_stat_user_tables
               ORDER BY pg_total_relation_size(relid) DESC LIMIT 20"""
        ):
            print(
                f"TABLE name={row[0]} bytes={row[1]} pretty={row[2]} "
                f"live={row[3]} dead={row[4]}"
            )
        for row in connection.execute(
            """SELECT indexrelname,relname,pg_relation_size(indexrelid),
                      pg_size_pretty(pg_relation_size(indexrelid))
               FROM pg_stat_user_indexes
               ORDER BY pg_relation_size(indexrelid) DESC LIMIT 20"""
        ):
            print(f"INDEX name={row[0]} table={row[1]} bytes={row[2]} pretty={row[3]}")
        counts = connection.execute(
            """SELECT
                 (SELECT COUNT(*) FROM import_runs),
                 (SELECT COUNT(*) FROM import_batches),
                 (SELECT COUNT(*) FROM import_jobs),
                 (SELECT COUNT(*) FROM source_records)"""
        ).fetchone()
        print(
            f"COUNTS runs={counts[0]} batches={counts[1]} "
            f"jobs={counts[2]} source_records={counts[3]}"
        )


if __name__ == "__main__":
    main()
