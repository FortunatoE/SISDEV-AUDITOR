"""Archive old completed import runs while retaining their private Blob originals.

Run without ``--apply`` for a dry run. The configured retention policy protects
the newest non-archived runs. Only runs with private source files are eligible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

import psycopg
from vercel.blob import BlobClient


DELETE_QUERIES = {
    "action_history": """DELETE FROM action_history WHERE id IN (
        SELECT h.id FROM action_history h JOIN reconciliations r
          ON r.id=h.reconciliation_id WHERE r.run_id=%s LIMIT %s
    )""",
    "document_decisions": "DELETE FROM document_decisions WHERE id IN (SELECT id FROM document_decisions WHERE run_id=%s LIMIT %s)",
    "reconciliations": "DELETE FROM reconciliations WHERE id IN (SELECT id FROM reconciliations WHERE run_id=%s LIMIT %s)",
    "actual_movements": "DELETE FROM actual_movements WHERE id IN (SELECT id FROM actual_movements WHERE run_id=%s LIMIT %s)",
    "expected_movements": "DELETE FROM expected_movements WHERE id IN (SELECT id FROM expected_movements WHERE run_id=%s LIMIT %s)",
    "audit_issues": "DELETE FROM audit_issues WHERE id IN (SELECT id FROM audit_issues WHERE run_id=%s LIMIT %s)",
    "source_records": "DELETE FROM source_records WHERE id IN (SELECT id FROM source_records WHERE run_id=%s LIMIT %s)",
}


def connection_url() -> str:
    value = os.getenv("SISDEV_MIGRATION_URL") or os.getenv("DATABASE_URL_UNPOOLED")
    if not value:
        raise SystemExit("SISDEV_MIGRATION_URL or DATABASE_URL_UNPOOLED is required")
    return value


def archive_run(connection: psycopg.Connection, run_id: int, admin_id: int, batch_size: int) -> int:
    run = connection.execute("SELECT * FROM import_runs WHERE id=%s", (run_id,)).fetchone()
    jobs = connection.execute(
        """SELECT id,source,source_file,blob_path,file_sha256,file_size,status,
                  processed_rows,inserted_rows,duplicate_rows,error_rows,total_rows,
                  created_at,finished_at
           FROM import_jobs WHERE run_id=%s ORDER BY id""",
        (run_id,),
    ).fetchall()
    originals = [dict(row) for row in jobs if str(row["blob_path"] or "").strip()]
    counts = {
        table: connection.execute(
            f"SELECT COUNT(*) AS total FROM {table} WHERE run_id=%s", (run_id,)
        ).fetchone()["total"]
        for table in DELETE_QUERIES
        if table != "action_history"
    }
    manifest = {
        "version": 1,
        "run_id": run_id,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "run": dict(run),
        "row_counts": counts,
        "original_files": originals,
        "restore_instruction": "Recriar um ciclo e reprocessar os arquivos privados relacionados.",
    }
    payload = json.dumps(manifest, ensure_ascii=False, default=str, separators=(",", ":")).encode()
    checksum = hashlib.sha256(payload).hexdigest()
    pathname = f"sisdev/archives/run-{run_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    with BlobClient() as client:
        blob = client.put(
            pathname, payload, access="private", add_random_suffix=True,
            content_type="application/json",
        )
    archive_id = None
    try:
        archive = connection.execute(
            """INSERT INTO run_archives(
               run_id,requested_by,manifest_path,manifest_checksum,status,
               retained_originals,released_rows,details_json
           ) VALUES (%s,%s,%s,%s,'PROCESSING',%s,0,%s)
           ON CONFLICT(run_id) DO UPDATE SET
             manifest_path=excluded.manifest_path,
             manifest_checksum=excluded.manifest_checksum,
             status='PROCESSING',retained_originals=excluded.retained_originals,
             details_json=excluded.details_json,completed_at=NULL
           RETURNING id""",
            (
                run_id, admin_id, blob.pathname, checksum, len(originals),
                json.dumps({"row_counts": counts}, ensure_ascii=False),
            ),
        ).fetchone()
        archive_id = archive["id"]
        connection.commit()
    except psycopg.errors.DiskFull:
        # At the hard Neon quota even an empty metadata table cannot allocate a
        # page. The private manifest already exists, so reclaim the old run in
        # place; import tables can reuse their own released pages immediately.
        connection.rollback()
        print(f"EMERGENCY run={run_id} manifest={blob.pathname}")

    released = 0
    for table, query in DELETE_QUERIES.items():
        while True:
            cursor = connection.execute(query, (run_id, batch_size))
            deleted = max(0, cursor.rowcount or 0)
            released += deleted
            if archive_id is not None:
                connection.execute(
                    "UPDATE run_archives SET processed_table=%s,released_rows=%s WHERE id=%s",
                    (table, released, archive_id),
                )
            connection.commit()
            if not deleted:
                break
    connection.execute("UPDATE import_runs SET status='ARCHIVED' WHERE id=%s", (run_id,))
    if archive_id is not None:
        connection.execute(
            """UPDATE run_archives SET status='COMPLETED',processed_table=NULL,
                      released_rows=%s,completed_at=CURRENT_TIMESTAMP WHERE id=%s""",
            (released, archive_id),
        )
    connection.commit()
    return released


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()
    with psycopg.connect(connection_url(), row_factory=psycopg.rows.dict_row) as connection:
        keep_row = connection.execute(
            "SELECT value FROM app_settings WHERE key='storage_keep_recent_runs'"
        ).fetchone()
        keep = max(1, int(keep_row["value"] if keep_row else 3))
        protected = {
            row["id"] for row in connection.execute(
                "SELECT id FROM import_runs WHERE status<>'ARCHIVED' ORDER BY id DESC LIMIT %s",
                (keep,),
            )
        }
        candidates = connection.execute(
            """SELECT r.id,COUNT(j.id) FILTER (WHERE COALESCE(j.blob_path,'')<>'') originals
               FROM import_runs r LEFT JOIN import_jobs j ON j.run_id=r.id
               WHERE r.status='SUCCESS'
               GROUP BY r.id ORDER BY r.id"""
        ).fetchall()
        candidates = [row for row in candidates if row["id"] not in protected and row["originals"]]
        print("PROTECTED", sorted(protected))
        print("ELIGIBLE", [row["id"] for row in candidates])
        if not args.apply:
            return
        admin = connection.execute(
            "SELECT id FROM app_users WHERE profile='ADMINISTRADOR' AND active=1 ORDER BY id LIMIT 1"
        ).fetchone()
        if not admin:
            raise SystemExit("An active administrator is required")
        for row in candidates:
            released = archive_run(connection, int(row["id"]), int(admin["id"]), args.batch_size)
            print(f"ARCHIVED run={row['id']} released_rows={released}")
        connection.autocommit = True
        for table in ("source_records", "expected_movements", "actual_movements", "reconciliations"):
            connection.execute(f"VACUUM (ANALYZE) {table}")


if __name__ == "__main__":
    main()
