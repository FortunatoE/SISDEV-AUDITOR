"""Database access shared by the desktop app and Vercel workers.

The application supports SQLite locally and PostgreSQL (Neon) in production.
This module deliberately exposes a small DB-API compatible wrapper so the rest
of the auditor can use ``?`` placeholders in both environments.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(
    os.getenv("SISDEV_DB_PATH", str(ROOT / "banco" / "sisdev_auditor.sqlite"))
).expanduser()

REQUIRED_IMPORT_SOURCES = (
    "sap_entry_current",
    "sap_exit_current",
    "sap_entry_history",
    "sap_exit_history",
    "sap_stock",
    "sisdev_stock",
    "sisdev_movement",
    "agrotis_recipe",
)

_SCHEMA_LOCK = threading.RLock()
_SCHEMA_READY: set[tuple[str, str]] = set()


def postgres_migrations_enabled() -> bool:
    """Keep schema changes out of normal serverless request handling.

    Production migrations are applied explicitly from ``sql/``. Running DDL
    during login makes every cold start depend on free database storage and can
    take the whole API down when the Neon quota is reached.
    """

    configured = str(os.getenv("SISDEV_AUTO_MIGRATE") or "").strip().lower()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return not bool(os.getenv("VERCEL"))


def _base_statements(id_type: str, timestamp_type: str) -> list[str]:
    return [
        f"""CREATE TABLE IF NOT EXISTS import_runs (
            id {id_type} PRIMARY KEY,
            started_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP,
            finished_at {timestamp_type},
            status TEXT NOT NULL,
            summary_json TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS source_records (
            id {id_type} PRIMARY KEY,
            run_id BIGINT NOT NULL,
            source TEXT NOT NULL,
            source_file TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, source, row_number)
        )""",
        f"""CREATE TABLE IF NOT EXISTS audit_issues (
            id {id_type} PRIMARY KEY,
            run_id BIGINT NOT NULL,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            reference TEXT,
            message TEXT NOT NULL,
            details_json TEXT,
            created_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS expected_movements (
            id {id_type} PRIMARY KEY,
            run_id BIGINT NOT NULL,
            source_record_id BIGINT,
            nf TEXT,
            series TEXT,
            direction TEXT,
            doc_date TEXT,
            sap_material TEXT,
            material_key TEXT,
            lot TEXT,
            manufacturer_lot TEXT,
            quantity DOUBLE PRECISION,
            unit TEXT,
            center TEXT,
            cnpj TEXT,
            status TEXT NOT NULL DEFAULT 'PENDENTE'
        )""",
        f"""CREATE TABLE IF NOT EXISTS actual_movements (
            id {id_type} PRIMARY KEY,
            run_id BIGINT NOT NULL,
            source_record_id BIGINT,
            nf TEXT,
            series TEXT,
            movement_type TEXT,
            movement_date TEXT,
            product TEXT,
            product_key TEXT,
            lot TEXT,
            quantity DOUBLE PRECISION,
            volume DOUBLE PRECISION,
            unit TEXT,
            cnpj TEXT,
            status TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS reconciliations (
            id {id_type} PRIMARY KEY,
            run_id BIGINT NOT NULL,
            expected_id BIGINT,
            actual_id BIGINT,
            status TEXT NOT NULL,
            diagnosis TEXT NOT NULL,
            details_json TEXT,
            confidence TEXT NOT NULL,
            created_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS action_history (
            id {id_type} PRIMARY KEY,
            reconciliation_id BIGINT,
            action TEXT NOT NULL,
            user_name TEXT,
            reason TEXT,
            created_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP
        )""",
    ]


IMPORT_JOB_COLUMNS: dict[str, str] = {
    "created_by": "BIGINT",
    "scope_centers_json": "TEXT",
    "scope_properties_json": "TEXT",
    "batch_id": "BIGINT",
    "run_id": "BIGINT",
    "source": "TEXT",
    "source_file": "TEXT",
    "blob_path": "TEXT",
    "blob_url": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'QUEUED'",
    "cursor_row": "INTEGER NOT NULL DEFAULT 0",
    "batch_size": "INTEGER NOT NULL DEFAULT 1000",
    "processed_rows": "INTEGER NOT NULL DEFAULT 0",
    "inserted_rows": "INTEGER NOT NULL DEFAULT 0",
    "duplicate_rows": "INTEGER NOT NULL DEFAULT 0",
    "error_rows": "INTEGER NOT NULL DEFAULT 0",
    "total_rows": "INTEGER NOT NULL DEFAULT 0",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "max_attempts": "INTEGER NOT NULL DEFAULT 3",
    "workflow_run_id": "TEXT",
    "warning_json": "TEXT",
    "error_message": "TEXT",
    "started_at": "TIMESTAMP",
    "heartbeat_at": "TIMESTAMP",
    "finished_at": "TIMESTAMP",
    "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "updated_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
}

IMPORT_BATCH_COLUMNS: dict[str, str] = {
    "created_by": "BIGINT",
    "scope_centers_json": "TEXT",
    "scope_properties_json": "TEXT",
}


def _orchestration_create_statements(id_type: str, timestamp_type: str) -> list[str]:
    required = ",".join(REQUIRED_IMPORT_SOURCES)
    return [
        f"""CREATE TABLE IF NOT EXISTS import_batches (
            id {id_type} PRIMARY KEY,
            run_id BIGINT NOT NULL,
            created_by BIGINT,
            scope_centers_json TEXT,
            scope_properties_json TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN',
            required_sources TEXT NOT NULL DEFAULT '{required}',
            reconciliation_workflow_run_id TEXT,
            error_message TEXT,
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reconciled_at {timestamp_type}
        )""",
        f"""CREATE TABLE IF NOT EXISTS import_jobs (
            id {id_type} PRIMARY KEY,
            batch_id BIGINT,
            run_id BIGINT,
            created_by BIGINT,
            scope_centers_json TEXT,
            scope_properties_json TEXT,
            source TEXT NOT NULL,
            source_file TEXT,
            blob_path TEXT NOT NULL,
            blob_url TEXT,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            cursor_row INTEGER NOT NULL DEFAULT 0,
            batch_size INTEGER NOT NULL DEFAULT 1000,
            processed_rows INTEGER NOT NULL DEFAULT 0,
            inserted_rows INTEGER NOT NULL DEFAULT 0,
            duplicate_rows INTEGER NOT NULL DEFAULT 0,
            error_rows INTEGER NOT NULL DEFAULT 0,
            total_rows INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            workflow_run_id TEXT,
            warning_json TEXT,
            error_message TEXT,
            started_at {timestamp_type},
            heartbeat_at {timestamp_type},
            finished_at {timestamp_type},
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS import_job_events (
            id {id_type} PRIMARY KEY,
            job_id BIGINT NOT NULL,
            status TEXT NOT NULL,
            cursor_row INTEGER NOT NULL DEFAULT 0,
            processed_rows INTEGER NOT NULL DEFAULT 0,
            total_rows INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            payload_json TEXT,
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS reconciliation_mappings (
            id {id_type} PRIMARY KEY,
            mapping_type TEXT NOT NULL,
            source_value TEXT NOT NULL,
            target_value TEXT NOT NULL,
            metadata_json TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mapping_type, source_value)
        )""",
    ]


def _security_create_statements(id_type: str, timestamp_type: str) -> list[str]:
    """Security, authorization and immutable audit structures.

    These tables are intentionally independent from reconciliation data so an
    authentication change cannot invalidate an import run.
    """

    return [
        f"""CREATE TABLE IF NOT EXISTS app_users (
            id {id_type} PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            profile TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            failed_login_count INTEGER NOT NULL DEFAULT 0,
            locked_until {timestamp_type},
            last_login_at {timestamp_type},
            deleted_at {timestamp_type},
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS user_scopes (
            id {id_type} PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES app_users(id),
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, scope_type, scope_value)
        )""",
        f"""CREATE TABLE IF NOT EXISTS auth_sessions (
            id {id_type} PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES app_users(id),
            token_hash TEXT NOT NULL UNIQUE,
            csrf_hash TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            expires_at {timestamp_type} NOT NULL,
            revoked_at {timestamp_type},
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS login_attempts (
            id {id_type} PRIMARY KEY,
            email TEXT,
            ip_address TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS audit_log (
            id {id_type} PRIMARY KEY,
            user_id BIGINT REFERENCES app_users(id),
            user_email TEXT,
            action TEXT NOT NULL,
            module TEXT NOT NULL,
            entity_type TEXT,
            entity_id TEXT,
            old_value_json TEXT,
            new_value_json TEXT,
            justification TEXT,
            result TEXT NOT NULL,
            ip_address TEXT,
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS backup_registry (
            id {id_type} PRIMARY KEY,
            requested_by BIGINT REFERENCES app_users(id),
            provider TEXT NOT NULL,
            storage_path TEXT,
            checksum TEXT,
            status TEXT NOT NULL,
            details_json TEXT,
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            restored_at {timestamp_type},
            restore_result TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS document_decisions (
            id {id_type} PRIMARY KEY,
            run_id BIGINT NOT NULL,
            document_key TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            selected_ids_json TEXT,
            status TEXT NOT NULL DEFAULT 'SAVED',
            justification TEXT,
            user_id BIGINT REFERENCES app_users(id),
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, document_key, decision_type)
        )""",
        f"""CREATE TABLE IF NOT EXISTS work_items (
            id {id_type} PRIMARY KEY,
            run_id BIGINT NOT NULL,
            document_key TEXT NOT NULL,
            center TEXT,
            nf TEXT,
            series TEXT,
            direction TEXT,
            assigned_to BIGINT REFERENCES app_users(id),
            status TEXT NOT NULL DEFAULT 'NOVA',
            priority TEXT NOT NULL DEFAULT 'MEDIA',
            due_date TEXT,
            created_by BIGINT REFERENCES app_users(id),
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at {timestamp_type},
            UNIQUE(run_id, document_key)
        )""",
        f"""CREATE TABLE IF NOT EXISTS work_item_comments (
            id {id_type} PRIMARY KEY,
            work_item_id BIGINT NOT NULL REFERENCES work_items(id),
            author_user_id BIGINT NOT NULL REFERENCES app_users(id),
            comment TEXT NOT NULL,
            created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    ]


POSTGRES_IMPORT_ALTERS = [
    f"ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS {name} {definition}"
    for name, definition in IMPORT_JOB_COLUMNS.items()
]

POSTGRES_BATCH_ALTERS = [
    f"ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS {name} {definition}"
    for name, definition in IMPORT_BATCH_COLUMNS.items()
]

INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS expected_movements_document_idx ON expected_movements(run_id, nf, series, cnpj, direction, center)",
    "CREATE INDEX IF NOT EXISTS expected_movements_match_idx ON expected_movements(run_id, direction, material_key, doc_date)",
    "CREATE INDEX IF NOT EXISTS actual_movements_document_idx ON actual_movements(run_id, nf, series)",
    "CREATE INDEX IF NOT EXISTS reconciliations_run_status_idx ON reconciliations(run_id, status, expected_id, actual_id)",
    "CREATE INDEX IF NOT EXISTS reconciliations_expected_idx ON reconciliations(expected_id, run_id)",
    "CREATE INDEX IF NOT EXISTS reconciliations_run_expected_idx ON reconciliations(run_id, expected_id)",
    "CREATE INDEX IF NOT EXISTS import_jobs_status_idx ON import_jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS import_jobs_batch_source_idx ON import_jobs(batch_id, source, created_at)",
    "CREATE INDEX IF NOT EXISTS import_jobs_workflow_idx ON import_jobs(workflow_run_id)",
    "CREATE INDEX IF NOT EXISTS import_jobs_owner_status_idx ON import_jobs(created_by, status, created_at)",
    "CREATE INDEX IF NOT EXISTS import_batches_owner_status_idx ON import_batches(created_by, status, created_at)",
    "CREATE INDEX IF NOT EXISTS import_job_events_job_idx ON import_job_events(job_id, created_at)",
    "CREATE INDEX IF NOT EXISTS reconciliation_mappings_type_idx ON reconciliation_mappings(mapping_type, status)",
    "CREATE INDEX IF NOT EXISTS app_users_profile_idx ON app_users(profile, active)",
    "CREATE INDEX IF NOT EXISTS user_scopes_user_idx ON user_scopes(user_id, scope_type)",
    "CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS auth_sessions_validation_idx ON auth_sessions(token_hash, revoked_at, expires_at, last_seen_at)",
    "CREATE INDEX IF NOT EXISTS login_attempts_identity_idx ON login_attempts(email, ip_address, created_at)",
    "CREATE INDEX IF NOT EXISTS audit_log_created_idx ON audit_log(created_at)",
    "CREATE INDEX IF NOT EXISTS audit_log_user_idx ON audit_log(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS document_decisions_document_idx ON document_decisions(run_id, document_key, updated_at)",
    "CREATE INDEX IF NOT EXISTS work_items_queue_idx ON work_items(run_id, status, priority, due_date)",
    "CREATE INDEX IF NOT EXISTS work_items_center_idx ON work_items(center, assigned_to, updated_at)",
    "CREATE INDEX IF NOT EXISTS work_item_comments_item_idx ON work_item_comments(work_item_id, created_at)",
]

POSTGRES_SECURITY_ALTERS = [
    "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
]


class Row(dict[str, Any]):
    """Mapping row that also supports SQLite-style numeric indexes."""

    def __init__(self, value: Mapping[str, Any] | sqlite3.Row):
        data = dict(value)
        super().__init__(data)
        self._values = list(data.values())

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return getattr(self._cursor, "lastrowid", None)

    def __iter__(self) -> Iterator[Row]:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def fetchone(self) -> Row | None:
        value = self._cursor.fetchone()
        return Row(value) if value is not None else None

    def fetchall(self) -> list[Row]:
        return [Row(value) for value in self._cursor.fetchall()]


class Connection:
    def __init__(self, connection: Any, *, postgres: bool):
        self._connection = connection
        self.postgres = postgres

    def execute(self, query: str, params: Sequence[Any] | None = None) -> Cursor:
        sql = query.replace("?", "%s") if self.postgres else query
        return Cursor(self._connection.execute(sql, params or ()))

    def executemany(self, query: str, params: Iterable[Sequence[Any]]) -> Cursor:
        sql = query.replace("?", "%s") if self.postgres else query
        cursor = self._connection.cursor()
        cursor.executemany(sql, params)
        return Cursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


def _ensure_postgres_schema(connection: Any) -> None:
    with connection.cursor() as cursor:
        for statement in _base_statements("BIGSERIAL", "TIMESTAMPTZ"):
            cursor.execute(statement)
        for statement in _orchestration_create_statements("BIGSERIAL", "TIMESTAMPTZ"):
            cursor.execute(statement)
        for statement in _security_create_statements("BIGSERIAL", "TIMESTAMPTZ"):
            cursor.execute(statement)
        # ``import_jobs`` existed before batches/cursors were introduced.
        for statement in POSTGRES_IMPORT_ALTERS:
            cursor.execute(statement)
        for statement in POSTGRES_BATCH_ALTERS:
            cursor.execute(statement)
        for statement in POSTGRES_SECURITY_ALTERS:
            cursor.execute(statement)
        cursor.execute(
            """UPDATE import_jobs SET status='SUPERSEDED',
               error_message=COALESCE(error_message,'Envio legado sem ciclo de importação.'),
               updated_at=CURRENT_TIMESTAMP WHERE batch_id IS NULL"""
        )
        for statement in INDEX_STATEMENTS:
            cursor.execute(statement)
        cursor.execute(
            """INSERT INTO app_settings(key, value)
               VALUES ('preferred_rt', 'KARLA DANIELLY GARCIA DE LIMA')
               ON CONFLICT(key) DO NOTHING"""
        )
    connection.commit()


def _ensure_sqlite_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in _base_statements("INTEGER", "TEXT"):
        connection.execute(statement)
    for statement in _orchestration_create_statements("INTEGER", "TEXT"):
        connection.execute(statement)
    for statement in _security_create_statements("INTEGER", "TEXT"):
        connection.execute(statement)

    existing = {
        row[1] for row in connection.execute("PRAGMA table_info(import_jobs)").fetchall()
    }
    for name, definition in IMPORT_JOB_COLUMNS.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE import_jobs ADD COLUMN {name} {definition}")

    existing_batches = {
        row[1] for row in connection.execute("PRAGMA table_info(import_batches)").fetchall()
    }
    for name, definition in IMPORT_BATCH_COLUMNS.items():
        if name not in existing_batches:
            connection.execute(f"ALTER TABLE import_batches ADD COLUMN {name} {definition}")

    user_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(app_users)").fetchall()
    }
    if "deleted_at" not in user_columns:
        connection.execute("ALTER TABLE app_users ADD COLUMN deleted_at TEXT")

    connection.execute(
        """UPDATE import_jobs SET status='SUPERSEDED',
           error_message=COALESCE(error_message,'Envio legado sem ciclo de importação.'),
           updated_at=CURRENT_TIMESTAMP WHERE batch_id IS NULL"""
    )

    for statement in INDEX_STATEMENTS:
        connection.execute(statement)
    connection.execute(
        """INSERT INTO app_settings(key, value)
           VALUES ('preferred_rt', 'KARLA DANIELLY GARCIA DE LIMA')
           ON CONFLICT(key) DO NOTHING"""
    )
    connection.commit()


def connect() -> Connection:
    """Open a configured database connection and apply idempotent migrations."""

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(database_url, row_factory=dict_row)
        if postgres_migrations_enabled():
            schema_key = ("postgres", database_url)
            with _SCHEMA_LOCK:
                if schema_key not in _SCHEMA_READY:
                    _ensure_postgres_schema(raw)
                    _SCHEMA_READY.add(schema_key)
        return Connection(raw, postgres=True)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw_sqlite = sqlite3.connect(DB_PATH)
    raw_sqlite.row_factory = sqlite3.Row
    raw_sqlite.execute("PRAGMA foreign_keys=ON")
    schema_key = ("sqlite", str(DB_PATH.resolve()))
    with _SCHEMA_LOCK:
        if schema_key not in _SCHEMA_READY:
            _ensure_sqlite_schema(raw_sqlite)
            _SCHEMA_READY.add(schema_key)
    return Connection(raw_sqlite, postgres=False)
