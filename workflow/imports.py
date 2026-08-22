"""Durable Vercel workflows for SISDEV imports and reconciliation.

Every step processes at most one source batch. Progress is committed to Neon,
so a retry resumes from ``cursor_row`` instead of restarting the HTTP request.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from vercel.workflow import Run, Workflows, start


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))


wf = Workflows(namespace="sisdev")

FINISHED_JOB_STATUSES = {"COMPLETED", "COMPLETED_WITH_WARNINGS"}


def _connect():
    """Open Neon only from a workflow step, never during sandbox discovery."""

    from auditor.database import connect

    return connect()


def _job(job_id: int) -> dict[str, Any]:
    connection = _connect()
    try:
        row = connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Importação não encontrada.")
        return dict(row)
    finally:
        connection.close()


def _event(
    connection: Any,
    job_id: int,
    status: str,
    *,
    cursor_row: int = 0,
    processed_rows: int = 0,
    total_rows: int = 0,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """INSERT INTO import_job_events(
               job_id,status,cursor_row,processed_rows,total_rows,message,payload_json
           ) VALUES (?,?,?,?,?,?,?)""",
        (
            job_id,
            status,
            cursor_row,
            processed_rows,
            total_rows,
            message,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        ),
    )


def _friendly_error(error: BaseException) -> str:
    text = str(error).strip()
    lowered = text.lower()
    if isinstance(error, FileNotFoundError):
        return "O arquivo original não foi encontrado no Blob. Envie a fonte novamente."
    if "timed out" in lowered or "timeout" in lowered:
        return "A etapa excedeu o tempo disponível e poderá ser retomada."
    if "conteúdo do arquivo não corresponde" in lowered:
        return "O arquivo enviado não corresponde ao formato selecionado."
    if "blob" in lowered or "http error" in lowered or "urlopen" in lowered:
        return "Não foi possível ler o arquivo armazenado no Blob."
    if "database" in lowered or "postgres" in lowered or "psycopg" in lowered:
        return "Não foi possível gravar o lote no Neon."
    if text:
        return text[:500]
    return "Falha inesperada durante o processamento desta fonte."


def _blob_url_from_path(pathname: str, token: str) -> str:
    """Resolve a legacy pathname by exact equality, never by recency."""

    query = urlencode({"prefix": pathname, "limit": "100"})
    request = Request(
        "https://blob.vercel-storage.com?" + query,
        headers={"Authorization": f"Bearer {token}", "x-api-version": "7"},
    )
    with urlopen(request, timeout=30) as response:
        blobs = json.load(response).get("blobs", [])
    matches = [item for item in blobs if item.get("pathname") == pathname]
    if len(matches) != 1 or not matches[0].get("url"):
        raise FileNotFoundError(f"Blob exato não encontrado: {pathname}")
    return str(matches[0]["url"])


def _download_job_blob(job: dict[str, Any]) -> Path:
    pathname = str(job.get("blob_path") or "").strip()
    if not pathname:
        raise FileNotFoundError("O job não possui blob_path.")
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("O armazenamento Blob não está configurado.")

    blob_url = str(job.get("blob_url") or "").strip()
    if not blob_url:
        blob_url = _blob_url_from_path(pathname, token)

    suffix = Path(pathname).suffix.lower()
    target_dir = Path("/tmp/sisdev-jobs") / str(job["id"])
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ("source" + suffix)
    partial = target.with_suffix(target.suffix + ".part")
    request = Request(blob_url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=120) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    partial.replace(target)

    signature = target.read_bytes()[:8]
    valid = {
        ".pdf": signature.startswith(b"%PDF"),
        ".xlsx": signature.startswith(b"PK"),
        # Agrotis can export OOXML content while retaining the legacy .xls name.
        ".xls": signature.startswith(bytes.fromhex("D0CF11E0")) or signature.startswith(b"PK"),
    }.get(suffix, False)
    if not valid:
        raise ValueError("O conteúdo do arquivo não corresponde ao formato esperado.")
    return target


def _merged_warnings(existing_json: str | None, incoming: list[Any]) -> list[Any]:
    try:
        existing = list(json.loads(existing_json or "[]"))
    except (TypeError, ValueError):
        existing = []
    for warning in incoming:
        if warning not in existing:
            existing.append(warning)
    return existing


def _update_progress(
    job_id: int,
    progress: dict[str, Any],
    *,
    base_inserted: int,
    base_duplicates: int,
    base_warnings_json: str | None,
) -> None:
    """Persist a commit checkpoint reported by the single source parser."""

    processed = int(progress.get("processed_rows") or 0)
    total = int(progress.get("total_rows") or 0)
    inserted = base_inserted + int(progress.get("imported_rows") or 0)
    duplicates = base_duplicates + int(progress.get("duplicate_rows") or 0)
    warnings = _merged_warnings(base_warnings_json, list(progress.get("warnings") or []))
    connection = _connect()
    try:
        updated = connection.execute(
            """UPDATE import_jobs SET status='PROCESSING',cursor_row=?,processed_rows=?,
               total_rows=?,inserted_rows=?,duplicate_rows=?,warning_json=?,
               heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?
               AND status<>'SUPERSEDED'""",
            (
                processed,
                processed,
                total,
                inserted,
                duplicates,
                json.dumps(warnings, ensure_ascii=False),
                job_id,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise ValueError("Este envio foi substituído por um arquivo mais recente.")
        _event(
            connection,
            job_id,
            "PROCESSING",
            cursor_row=processed,
            processed_rows=processed,
            total_rows=total,
            message=f"Lote persistido: {processed}/{total} linhas.",
        )
        connection.commit()
    finally:
        connection.close()


def _process_source_once(job_id: int) -> dict[str, Any]:
    job = _job(job_id)
    if job["status"] in FINISHED_JOB_STATUSES:
        return {
            "job_id": job_id,
            "done": True,
            "processed_rows": int(job.get("processed_rows") or 0),
            "total_rows": int(job.get("total_rows") or 0),
        }
    if job["status"] == "SUPERSEDED":
        raise ValueError("Este envio foi substituído por um arquivo mais recente.")
    if not job.get("run_id") or not job.get("batch_id"):
        raise ValueError("A importação não está vinculada a um ciclo válido.")
    resume_cursor = int(job.get("cursor_row") or 0)
    base_inserted = int(job.get("inserted_rows") or 0)
    base_duplicates = int(job.get("duplicate_rows") or 0)
    base_warnings_json = job.get("warning_json")

    connection = _connect()
    try:
        updated = connection.execute(
            """UPDATE import_jobs
               SET status='PROCESSING', started_at=COALESCE(started_at,CURRENT_TIMESTAMP),
                   heartbeat_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP,
                   error_message=NULL
               WHERE id=? AND status<>'SUPERSEDED'""",
            (job_id,),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise ValueError("Este envio foi substituído por um arquivo mais recente.")
        connection.execute(
            "UPDATE import_batches SET status='IMPORTING',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='OPEN'",
            (job["batch_id"],),
        )
        _event(
            connection,
            job_id,
            "PROCESSING",
            cursor_row=resume_cursor,
            processed_rows=resume_cursor,
            total_rows=int(job.get("total_rows") or 0),
            message="Leitura da fonte iniciada.",
        )
        connection.commit()
    finally:
        connection.close()

    local_path = _download_job_blob(job)
    from auditor.engine import import_source

    result = import_source(
        int(job["run_id"]),
        str(job["source"]),
        local_path,
        batch_size=int(job.get("batch_size") or 1000),
        cursor=resume_cursor,
        max_rows=None,
        progress_callback=lambda progress: _update_progress(
            job_id,
            progress,
            base_inserted=base_inserted,
            base_duplicates=base_duplicates,
            base_warnings_json=base_warnings_json,
        ),
    )
    next_cursor = int(result.get("next_cursor") or result.get("processed_rows") or 0)
    total_rows = int(result.get("total_rows") or 0)
    done = bool(result.get("done"))
    warnings = _merged_warnings(base_warnings_json, list(result.get("warnings") or []))
    duplicates = base_duplicates + int(result.get("duplicate_rows") or 0)
    inserted = base_inserted + int(result.get("imported_rows") or 0)
    final_status = "PROCESSING"
    if done:
        final_status = "COMPLETED_WITH_WARNINGS" if warnings or duplicates else "COMPLETED"

    connection = _connect()
    try:
        updated = connection.execute(
            """UPDATE import_jobs
               SET status=?, cursor_row=?, processed_rows=?, total_rows=?,
                   inserted_rows=?, duplicate_rows=?,
                   warning_json=?, heartbeat_at=CURRENT_TIMESTAMP,
                   finished_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE finished_at END,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status<>'SUPERSEDED'""",
            (
                final_status,
                next_cursor,
                next_cursor,
                total_rows,
                inserted,
                duplicates,
                json.dumps(warnings, ensure_ascii=False),
                done,
                job_id,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise ValueError("Este envio foi substituído por um arquivo mais recente.")
        _event(
            connection,
            job_id,
            final_status,
            cursor_row=next_cursor,
            processed_rows=next_cursor,
            total_rows=total_rows,
            message="Fonte concluída." if done else "Lote concluído; continuação agendada.",
            payload={"duplicates": duplicates, "warnings": warnings},
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "job_id": job_id,
        "status": final_status,
        "done": done,
        "processed_rows": next_cursor,
        "total_rows": total_rows,
    }


def _mark_import_failed(job_id: int, message: str) -> None:
    connection = _connect()
    try:
        job = connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return
        updated = connection.execute(
            """UPDATE import_jobs SET status='FAILED',error_message=?,error_rows=error_rows+1,
               heartbeat_at=CURRENT_TIMESTAMP,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status<>'SUPERSEDED'""",
            (message, job_id),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return
        _event(
            connection,
            job_id,
            "FAILED",
            cursor_row=int(job.get("cursor_row") or 0),
            processed_rows=int(job.get("processed_rows") or 0),
            total_rows=int(job.get("total_rows") or 0),
            message=message,
        )
        connection.commit()
    finally:
        connection.close()


@wf.step(max_retries=2)
async def process_import_source(job_id: int) -> dict[str, Any]:
    return _process_source_once(job_id)


@wf.step(max_retries=0)
async def mark_import_failed(job_id: int, message: str) -> None:
    _mark_import_failed(job_id, message)


@wf.workflow
async def process_import_job(job_id: int) -> dict[str, Any]:
    try:
        return await process_import_source(job_id)
    except Exception as error:
        await mark_import_failed(job_id, _friendly_error(error))
        raise


def _reconcile_batch(batch_id: int) -> dict[str, Any]:
    connection = _connect()
    try:
        batch = connection.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ValueError("Ciclo de importação não encontrado.")
        required = [item for item in str(batch["required_sources"] or "").split(",") if item]
        latest: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT * FROM import_jobs WHERE batch_id=? ORDER BY id DESC", (batch_id,)
        ):
            latest.setdefault(str(row["source"]), dict(row))
        missing = [source for source in required if source not in latest]
        unfinished = [
            source
            for source in required
            if source in latest and latest[source]["status"] not in FINISHED_JOB_STATUSES
        ]
        if missing or unfinished:
            details = []
            if missing:
                details.append("fontes ausentes: " + ", ".join(missing))
            if unfinished:
                details.append("fontes não concluídas: " + ", ".join(unfinished))
            raise ValueError("Conciliação aguardando " + "; ".join(details) + ".")
        run_id = int(batch["run_id"])
    finally:
        connection.close()

    from auditor.engine import reconcile_run

    result = reconcile_run(run_id, require_complete=True)
    connection = _connect()
    try:
        connection.execute(
            """UPDATE import_batches SET status='COMPLETED',error_message=NULL,
               reconciled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (batch_id,),
        )
        connection.commit()
    finally:
        connection.close()
    return {"batch_id": batch_id, "run_id": run_id, **result}


def _mark_reconciliation_failed(batch_id: int, message: str) -> None:
    connection = _connect()
    try:
        connection.execute(
            "UPDATE import_batches SET status='FAILED',error_message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (message, batch_id),
        )
        connection.commit()
    finally:
        connection.close()


@wf.step(max_retries=1)
async def reconcile_batch_step(batch_id: int) -> dict[str, Any]:
    return _reconcile_batch(batch_id)


@wf.step(max_retries=0)
async def mark_reconciliation_failed(batch_id: int, message: str) -> None:
    _mark_reconciliation_failed(batch_id, message)


@wf.workflow
async def reconcile_import_batch(batch_id: int) -> dict[str, Any]:
    try:
        return await reconcile_batch_step(batch_id)
    except Exception as error:
        await mark_reconciliation_failed(batch_id, _friendly_error(error))
        raise


async def start_import(job_id: int) -> Run:
    return await start(process_import_job, job_id)


async def start_reconciliation(batch_id: int) -> Run:
    return await start(reconcile_import_batch, batch_id)
