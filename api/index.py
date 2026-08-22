"""Flask API deployed by Vercel.

Uploads are registered as jobs in Neon. The HTTP request only starts a durable
Vercel Workflow; parsing and persistence happen later in resumable batches.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor.database import REQUIRED_IMPORT_SOURCES, connect
from auditor.engine import (
    dashboard_v2,
    page_records_v2,
    regularization_export_rows,
    rt_preference_options,
    set_preferred_rt,
    validation_rows,
)
from auditor.exporting import xlsx_bytes


STATIC = ROOT / "src" / "web"
ALLOWED_SOURCES = set(REQUIRED_IMPORT_SOURCES)
FINISHED_JOB_STATUSES = {"COMPLETED", "COMPLETED_WITH_WARNINGS"}
ACTIVE_BATCH_STATUSES = {"OPEN", "IMPORTING", "RECONCILIATION_STARTING", "RECONCILING"}
MAPPING_TYPES = {
    "material_product",
    "property_cnpj",
    "cnpj_center_ure",
    "manufacturer_lot",
    "reconciliation_rule",
}
STATUS_LABELS = {
    "STARTING": "Aguardando",
    "QUEUED": "Aguardando",
    "PROCESSING": "Processando",
    "COMPLETED": "Concluído",
    "COMPLETED_WITH_WARNINGS": "Concluído com alertas",
    "FAILED": "Falhou",
    "SUPERSEDED": "Substituído",
}

app = Flask(__name__)
# Multipart uploads cross the Vercel Function request body. The operational
# files are currently <=2.22 MB, so keep an explicit margin below the platform
# limit; larger future files must use Blob client uploads.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in dict(row).items()}


def _safe_error(error: BaseException) -> str:
    text = str(error).strip()
    lowered = text.lower()
    if "workflow" in lowered or "queue" in lowered:
        return "Não foi possível iniciar a fila agora. Tente novamente em instantes."
    if "blob" in lowered:
        return "Não foi possível acessar o armazenamento de arquivos."
    if "database" in lowered or "postgres" in lowered or "psycopg" in lowered:
        return "Não foi possível acessar o banco Neon."
    return text[:500] if text else "Falha inesperada na operação."


def _job_payload(row: Any, *, include_error: bool = True) -> dict[str, Any]:
    result = _row_dict(row)
    processed = int(result.get("processed_rows") or 0)
    total = int(result.get("total_rows") or 0)
    status = str(result.get("status") or "QUEUED")
    if total > 0:
        progress = min(100.0, round(processed * 100.0 / total, 1))
    else:
        progress = 100.0 if status in FINISHED_JOB_STATUSES else 0.0
    result["progress_percent"] = progress
    result["status_label"] = STATUS_LABELS.get(status, status.title())
    result["can_retry"] = status == "FAILED"
    if not include_error:
        result.pop("error_message", None)
    return result


def _batch_payload(row: Any) -> dict[str, Any]:
    result = _row_dict(row)
    result["required_sources"] = [
        item for item in str(result.get("required_sources") or "").split(",") if item
    ]
    return result


def _load_job(job_id: int) -> Any:
    connection = connect()
    try:
        return connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        connection.close()


def _get_or_create_batch(connection: Any) -> Any:
    batch = connection.execute(
        """SELECT * FROM import_batches
           WHERE status IN ('OPEN','IMPORTING') ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if batch is not None:
        return batch

    summary = json.dumps(
        {"sources": {}, "source_details": {}, "warnings": []}, ensure_ascii=False
    )
    run_id = connection.execute(
        "INSERT INTO import_runs(status,summary_json) VALUES ('RUNNING',?) RETURNING id",
        (summary,),
    ).fetchone()[0]
    required = ",".join(REQUIRED_IMPORT_SOURCES)
    return connection.execute(
        """INSERT INTO import_batches(run_id,status,required_sources)
           VALUES (?,'OPEN',?) RETURNING *""",
        (run_id, required),
    ).fetchone()


def _latest_jobs_for_batch(connection: Any, batch_id: int) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for row in connection.execute(
        "SELECT * FROM import_jobs WHERE batch_id=? ORDER BY id DESC", (batch_id,)
    ):
        latest.setdefault(str(row["source"]), row)
    return latest


def _batch_readiness(connection: Any, batch: Any) -> dict[str, Any]:
    required = [item for item in str(batch["required_sources"] or "").split(",") if item]
    latest = _latest_jobs_for_batch(connection, int(batch["id"]))
    missing = [source for source in required if source not in latest]
    unfinished = [
        source
        for source in required
        if source in latest and latest[source]["status"] not in FINISHED_JOB_STATUSES
    ]
    return {
        "ready": not missing and not unfinished,
        "missing_sources": missing,
        "unfinished_sources": unfinished,
        "jobs": {source: _job_payload(row) for source, row in latest.items()},
    }


def _run_coroutine(coroutine: Any) -> Any:
    """Run a short SDK call from Flask's synchronous WSGI handler."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # This path mainly supports async test harnesses without nesting event loops.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _start_import_workflow(job_id: int) -> str:
    from workflow.imports import start_import

    run = _run_coroutine(start_import(job_id))
    return str(run.run_id)


def _start_reconciliation_workflow(batch_id: int) -> str:
    from workflow.imports import start_reconciliation

    run = _run_coroutine(start_reconciliation(batch_id))
    return str(run.run_id)


def _queue_job(job_id: int, *, manual_retry: bool = False, restart: bool = False):
    connection = connect()
    try:
        job = connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return jsonify({"error": "Importação não encontrada."}), 404
        if job["status"] == "SUPERSEDED":
            return jsonify({"error": "Este arquivo foi substituído por um envio mais recente."}), 409
        if job["status"] in FINISHED_JOB_STATUSES and not restart:
            return jsonify(_job_payload(job)), 200
        if job["status"] == "STARTING":
            return jsonify(_job_payload(job)), 202
        if (
            job["status"] in {"QUEUED", "PROCESSING"}
            and job.get("workflow_run_id")
            and not manual_retry
            and not restart
        ):
            return jsonify(_job_payload(job)), 202

        attempts = int(job.get("attempt_count") or 0)
        maximum = int(job.get("max_attempts") or 3)
        if attempts >= maximum and not manual_retry:
            return jsonify({
                "error": "O limite automático de tentativas foi atingido.",
                "retry_url": f"/api/import/{job_id}/retry",
            }), 409
        if manual_retry and attempts >= maximum:
            maximum = attempts + 1

        reset_sql = ""
        if restart:
            reset_sql = ",cursor_row=0,processed_rows=0,inserted_rows=0,duplicate_rows=0,error_rows=0,total_rows=0"
        claimed = connection.execute(
            f"""UPDATE import_jobs SET status='STARTING',workflow_run_id=NULL,
                attempt_count=attempt_count+1,max_attempts=?,error_message=NULL,
                finished_at=NULL,updated_at=CURRENT_TIMESTAMP{reset_sql}
                WHERE id=? AND status=?""",
            (maximum, job_id, job["status"]),
        )
        # Compare-and-set prevents two clicks from starting two workflow runs.
        if claimed.rowcount != 1:
            connection.rollback()
            current = connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
            return jsonify(_job_payload(current)), 202
        connection.execute(
            """INSERT INTO import_job_events(job_id,status,cursor_row,processed_rows,total_rows,message)
               SELECT id,'STARTING',cursor_row,processed_rows,total_rows,? FROM import_jobs WHERE id=?""",
            ("Reprocessamento solicitado." if manual_retry else "Processamento solicitado.", job_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    try:
        workflow_run_id = _start_import_workflow(job_id)
    except Exception as error:
        message = _safe_error(error)
        connection = connect()
        try:
            connection.execute(
                """UPDATE import_jobs SET status='FAILED',error_message=?,finished_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='STARTING'""",
                (message, job_id),
            )
            connection.commit()
        finally:
            connection.close()
        app.logger.exception("Could not start import workflow for job %s", job_id)
        return jsonify({"error": message, "job_id": job_id}), 503

    connection = connect()
    try:
        connection.execute(
            """UPDATE import_jobs SET workflow_run_id=?,
               status=CASE WHEN status='STARTING' THEN 'QUEUED' ELSE status END,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (workflow_run_id, job_id),
        )
        connection.commit()
        job = connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        connection.close()
    payload = _job_payload(job)
    payload.update({"ok": True, "workflow_run_id": workflow_run_id})
    return jsonify(payload), 202


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_error: RequestEntityTooLarge):
    return jsonify({"error": "Arquivo maior que o limite atual de 4 MB."}), 413


@app.errorhandler(HTTPException)
def http_error(error: HTTPException):
    if request.path.startswith("/api/"):
        return jsonify({"error": error.description}), error.code
    return error


@app.errorhandler(Exception)
def unexpected_error(error: Exception):
    if request.path.startswith("/api/"):
        app.logger.exception("Unhandled API error")
        return jsonify({"error": _safe_error(error)}), 500
    raise error


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "queue": "vercel-workflow", "database": "neon-or-sqlite"})


@app.get("/api/dashboard")
def dashboard():
    return jsonify(dashboard_v2(request.args.to_dict()))


@app.get("/api/page/<page>")
def page(page: str):
    return jsonify(page_records_v2(page, request.args.to_dict()))


@app.get("/api/settings/rt-preference")
def get_rt_preference():
    return jsonify(rt_preference_options())


@app.post("/api/settings/rt-preference")
def post_rt_preference():
    value = str((request.get_json(silent=True) or {}).get("preferred_rt", "")).strip()
    set_preferred_rt(value)
    return jsonify({"ok": True, "preferred_rt": value})


@app.get("/api/mappings")
def list_mappings():
    mapping_type = request.args.get("type", "").strip()
    status = request.args.get("status", "ACTIVE").strip().upper()
    if mapping_type and mapping_type not in MAPPING_TYPES:
        return jsonify({"error": "Tipo de mapeamento inválido."}), 400
    clauses, params = [], []
    if mapping_type:
        clauses.append("mapping_type=?")
        params.append(mapping_type)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT * FROM reconciliation_mappings" + where + " ORDER BY mapping_type,source_value",
            params,
        ).fetchall()
    finally:
        connection.close()
    return jsonify({"rows": [_row_dict(row) for row in rows], "count": len(rows)})


@app.post("/api/mappings")
def save_mapping():
    data = request.get_json(silent=True) or {}
    mapping_type = str(data.get("mapping_type") or "").strip()
    source_value = str(data.get("source_value") or "").strip()
    target_value = str(data.get("target_value") or "").strip()
    status = str(data.get("status") or "ACTIVE").strip().upper()
    metadata = data.get("metadata")
    if mapping_type not in MAPPING_TYPES:
        return jsonify({"error": "Tipo de mapeamento inválido."}), 400
    if not source_value or not target_value:
        return jsonify({"error": "Origem e destino são obrigatórios."}), 400
    if status not in {"ACTIVE", "INACTIVE"}:
        return jsonify({"error": "Status de mapeamento inválido."}), 400
    if metadata is not None and not isinstance(metadata, (dict, list)):
        return jsonify({"error": "metadata deve ser um objeto ou uma lista."}), 400
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
    connection = connect()
    try:
        row = connection.execute(
            """INSERT INTO reconciliation_mappings(
                   mapping_type,source_value,target_value,metadata_json,status
               ) VALUES (?,?,?,?,?)
               ON CONFLICT(mapping_type,source_value) DO UPDATE SET
                 target_value=excluded.target_value,metadata_json=excluded.metadata_json,
                 status=excluded.status,updated_at=CURRENT_TIMESTAMP
               RETURNING *""",
            (mapping_type, source_value, target_value, metadata_json, status),
        ).fetchone()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return jsonify({"ok": True, "mapping": _row_dict(row)})


@app.post("/api/upload")
def upload_data():
    source = request.form.get("source", "").strip()
    upload = request.files.get("file")
    if source not in ALLOWED_SOURCES or upload is None or not upload.filename:
        return jsonify({"error": "Fonte ou arquivo inválido."}), 400
    extension = Path(upload.filename).suffix.lower()
    accepted = {".pdf"} if source == "sisdev_stock" else {".xlsx", ".xls"}
    if extension not in accepted:
        expected = "PDF" if source == "sisdev_stock" else "XLSX ou XLS"
        return jsonify({"error": f"Formato inválido; envie {expected}."}), 400

    from vercel.blob import BlobClient

    pathname = f"sisdev/{source}/{secure_filename(upload.filename)}"
    with BlobClient() as client:
        blob = client.put(
            pathname,
            upload.stream,
            access="private",
            add_random_suffix=True,
            content_type=upload.mimetype or None,
        )

    connection = connect()
    try:
        batch = _get_or_create_batch(connection)
        connection.execute(
            """UPDATE import_jobs SET status='SUPERSEDED',finished_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP
               WHERE batch_id=? AND source=? AND status<>'SUPERSEDED'""",
            (batch["id"], source),
        )
        job = connection.execute(
            """INSERT INTO import_jobs(
                   batch_id,run_id,source,source_file,blob_path,blob_url,status
               ) VALUES (?,?,?,?,?,?,'QUEUED') RETURNING *""",
            (
                batch["id"],
                batch["run_id"],
                source,
                secure_filename(upload.filename),
                blob.pathname,
                blob.download_url or blob.url,
            ),
        ).fetchone()
        connection.execute(
            """INSERT INTO import_job_events(job_id,status,message)
               VALUES (?,'QUEUED','Arquivo recebido e aguardando processamento.')""",
            (job["id"],),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    payload = _job_payload(job)
    payload.update({
        "ok": True,
        "job_id": job["id"],
        "batch_id": batch["id"],
        "run_id": batch["run_id"],
        "file": upload.filename,
        "pathname": blob.pathname,
    })
    return jsonify(payload), 201


@app.post("/api/import")
def legacy_import():
    return jsonify({
        "error": "O processamento global foi desativado para evitar timeout.",
        "next": "Envie cada fonte e use POST /api/import/<job_id>.",
    }), 410


@app.post("/api/import/<int:job_id>")
def start_import_job(job_id: int):
    data = request.get_json(silent=True) or {}
    return _queue_job(job_id, restart=bool(data.get("restart")))


@app.get("/api/import/<int:job_id>")
def import_job_status(job_id: int):
    connection = connect()
    try:
        job = connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return jsonify({"error": "Importação não encontrada."}), 404
        events = connection.execute(
            """SELECT status,cursor_row,processed_rows,total_rows,message,created_at
               FROM import_job_events WHERE job_id=? ORDER BY id DESC LIMIT 20""",
            (job_id,),
        ).fetchall()
    finally:
        connection.close()
    payload = _job_payload(job)
    payload["events"] = [_row_dict(event) for event in events]
    return jsonify(payload)


@app.post("/api/import/<int:job_id>/retry")
def retry_import_job(job_id: int):
    data = request.get_json(silent=True) or {}
    return _queue_job(job_id, manual_retry=True, restart=bool(data.get("restart")))


@app.get("/api/import-jobs")
def list_import_jobs():
    clauses, params = [], []
    for argument, column in (("batch_id", "batch_id"), ("source", "source"), ("status", "status")):
        value = request.args.get(argument, "").strip()
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    try:
        limit = max(1, min(500, int(request.args.get("limit", "100"))))
    except ValueError:
        return jsonify({"error": "limit deve ser numérico."}), 400
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT * FROM import_jobs" + where + " ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        connection.close()
    return jsonify({"jobs": [_job_payload(row) for row in rows], "count": len(rows)})


@app.get("/api/import-jobs/latest")
def latest_import_jobs():
    connection = connect()
    try:
        batch = connection.execute("SELECT * FROM import_batches ORDER BY id DESC LIMIT 1").fetchone()
        if batch is None:
            return jsonify({"batch": None, "jobs": {}, "ready": False})
        readiness = _batch_readiness(connection, batch)
    finally:
        connection.close()
    return jsonify({"batch": _batch_payload(batch), **readiness})


def _requested_batch(connection: Any, data: dict[str, Any]) -> Any:
    candidates: set[int] = set()
    for value in data.get("batch_ids") or []:
        candidates.add(int(value))
    job_ids = [int(value) for value in (data.get("job_ids") or [])]
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        for row in connection.execute(
            f"SELECT DISTINCT batch_id FROM import_jobs WHERE id IN ({placeholders})", job_ids
        ):
            if row["batch_id"] is not None:
                candidates.add(int(row["batch_id"]))
    if len(candidates) > 1:
        raise ValueError("Todos os jobs devem pertencer ao mesmo ciclo.")
    if candidates:
        return connection.execute(
            "SELECT * FROM import_batches WHERE id=?", (next(iter(candidates)),)
        ).fetchone()
    return connection.execute(
        """SELECT * FROM import_batches
           WHERE status IN ('OPEN','IMPORTING','FAILED','RECONCILIATION_STARTING','RECONCILING')
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()


@app.post("/api/reconcile")
def reconcile_completed_sources():
    data = request.get_json(silent=True) or {}
    connection = connect()
    try:
        batch = _requested_batch(connection, data)
        if batch is None:
            return jsonify({"error": "Nenhum ciclo de importação foi encontrado."}), 404
        if batch["status"] == "COMPLETED":
            return jsonify({"ok": True, "batch": _batch_payload(batch)}), 200
        readiness = _batch_readiness(connection, batch)
        if not readiness["ready"]:
            return jsonify({
                "error": "A conciliação só pode iniciar após todas as fontes obrigatórias.",
                **readiness,
            }), 409
        if batch["status"] in {"RECONCILIATION_STARTING", "RECONCILING"}:
            return jsonify({"ok": True, "batch": _batch_payload(batch), **readiness}), 202
        claimed = connection.execute(
            """UPDATE import_batches SET status='RECONCILIATION_STARTING',error_message=NULL,
               reconciliation_workflow_run_id=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status=?""",
            (batch["id"], batch["status"]),
        )
        if claimed.rowcount != 1:
            connection.rollback()
            current = connection.execute(
                "SELECT * FROM import_batches WHERE id=?", (batch["id"],)
            ).fetchone()
            return jsonify({"ok": True, "batch": _batch_payload(current), **readiness}), 202
        connection.commit()
    finally:
        connection.close()

    try:
        workflow_run_id = _start_reconciliation_workflow(int(batch["id"]))
    except Exception as error:
        message = _safe_error(error)
        connection = connect()
        try:
            connection.execute(
                """UPDATE import_batches SET status='FAILED',error_message=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='RECONCILIATION_STARTING'""",
                (message, batch["id"]),
            )
            connection.commit()
        finally:
            connection.close()
        app.logger.exception("Could not start reconciliation workflow for batch %s", batch["id"])
        return jsonify({"error": message, "batch_id": batch["id"]}), 503

    connection = connect()
    try:
        connection.execute(
            """UPDATE import_batches SET reconciliation_workflow_run_id=?,
               status=CASE WHEN status='RECONCILIATION_STARTING' THEN 'RECONCILING' ELSE status END,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (workflow_run_id, batch["id"]),
        )
        connection.commit()
        batch = connection.execute(
            "SELECT * FROM import_batches WHERE id=?", (batch["id"],)
        ).fetchone()
    finally:
        connection.close()
    return jsonify({
        "ok": True,
        "workflow_run_id": workflow_run_id,
        "batch": _batch_payload(batch),
        **readiness,
    }), 202


@app.get("/api/reconcile/<int:batch_id>")
def reconciliation_status(batch_id: int):
    connection = connect()
    try:
        batch = connection.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            return jsonify({"error": "Ciclo não encontrado."}), 404
        readiness = _batch_readiness(connection, batch)
    finally:
        connection.close()
    return jsonify({"batch": _batch_payload(batch), **readiness})


@app.get("/api/export/<fmt>/<page_name>")
def export(fmt: str, page_name: str):
    if page_name == "reports":
        rows = validation_rows()
    elif page_name == "regularization":
        rows = regularization_export_rows(request.args.to_dict())
    else:
        rows = page_records_v2(page_name, request.args.to_dict()).get("rows", [])
    columns = list(rows[0]) if rows else []
    if fmt == "csv":
        import csv
        import io

        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8-sig"), 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="sisdev_{page_name}.csv"',
        }
    if fmt == "xlsx":
        return xlsx_bytes(columns, rows), 200, {
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Content-Disposition": f'attachment; filename="sisdev_{page_name}.xlsx"',
        }
    return jsonify({"error": "Formato não suportado."}), 404


@app.get("/")
@app.get("/<path:path>")
def static_site(path: str = "index.html"):
    if path == "api" or path.startswith("api/"):
        return jsonify({"error": "Endpoint não encontrado."}), 404
    target = (STATIC / (path or "index.html")).resolve()
    try:
        target.relative_to(STATIC.resolve())
    except ValueError:
        return jsonify({"error": "Caminho inválido."}), 404
    if not target.is_file():
        target = STATIC / "index.html"
    return send_from_directory(STATIC, target.relative_to(STATIC))
