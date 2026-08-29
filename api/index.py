"""Flask API deployed by Vercel.

Uploads are registered as jobs in Neon. The HTTP request only starts a durable
Vercel Workflow; parsing and persistence happen later in resumable batches.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import zipfile
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor.database import REQUIRED_IMPORT_SOURCES, connect
from auditor.engine import (
    dashboard_v2,
    page_records_v2,
    regularization_document_detail,
    regularization_export_rows,
    rt_preference_options,
    set_preferred_rt,
    validation_rows,
)
from auditor.exporting import csv_bytes, xlsx_bytes
from auditor.security import (
    PROFILES,
    authenticate,
    create_session,
    create_user,
    ensure_bootstrap_admin,
    has_permission,
    hash_password,
    normalize_email,
    public_user,
    record_audit,
    revoke_session,
    scope_values,
    validate_session,
)


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
EXPORTABLE_PAGES = {
    "pending", "regularization", "analysis", "invoices", "recipes", "movements",
    "stocks", "reports", "history", "materials", "lots", "units",
    "movement_types", "units_measure", "logs", "rules",
}

OLE_WORKBOOK_SIGNATURE = bytes.fromhex("D0CF11E0")
OOXML_REQUIRED_MEMBERS = {"[content_types].xml", "xl/workbook.xml"}


def _spreadsheet_upload_error(stream: Any, extension: str) -> str | None:
    """Validate Excel content by signature, not only by the file suffix.

    Agrotis exports OOXML workbooks with the legacy ``.xls`` filename. Those
    files are valid Excel workbooks and must follow the same ZIP/macro checks
    as a regular ``.xlsx`` upload.
    """

    header = stream.read(8)
    stream.seek(0)
    is_ooxml = header.startswith(b"PK")
    is_ole = header.startswith(OLE_WORKBOOK_SIGNATURE)
    if extension == ".xlsx" and not is_ooxml:
        return "O conteúdo enviado não é uma planilha XLSX válida."
    if extension == ".xls" and not (is_ole or is_ooxml):
        return "O conteúdo enviado não é uma planilha XLS válida."
    if not is_ooxml:
        return None

    try:
        with zipfile.ZipFile(stream) as archive:
            members = {name.replace("\\", "/").lower() for name in archive.namelist()}
            if not OOXML_REQUIRED_MEMBERS.issubset(members):
                return "O conteúdo enviado não é uma planilha Excel válida."
            if any(name.endswith("vbaproject.bin") for name in members):
                return "Planilhas com macros não são permitidas."
    except zipfile.BadZipFile:
        return "A planilha Excel está corrompida."
    finally:
        stream.seek(0)
    return None

app = Flask(__name__)
_configured_secret = os.getenv("SISDEV_SECRET_KEY") or os.getenv("SECRET_KEY")
_runtime_environment = str(os.getenv("SISDEV_ENV") or "").strip().lower()
if (os.getenv("VERCEL") or _runtime_environment in {"production", "staging"}) and not _configured_secret:
    raise RuntimeError("SISDEV_SECRET_KEY não configurada no ambiente de produção.")
# Development/test sessions may use an ephemeral key, but never a public,
# predictable fallback that could be used to forge Flask cookies.
app.secret_key = _configured_secret or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_NAME="sisdev_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
)
# Multipart uploads cross the Vercel Function request body. The operational
# files are currently <=2.22 MB, so keep an explicit margin below the platform
# limit; larger future files must use Blob client uploads.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024

PUBLIC_ENDPOINTS = {"health", "auth_login"}
ENDPOINT_PERMISSIONS = {
    "post_rt_preference": "manage_mappings",
    "upload_data": "import",
    "start_import_job": "import",
    "retry_import_job": "import",
    "reconcile_completed_sources": "reconcile",
    "export": "export",
    "list_users": "manage_users",
    "create_app_user": "manage_users",
    "update_app_user": "manage_users",
    "delete_app_user": "manage_users",
    "create_backup": "manage_backup",
    "test_restore_backup": "manage_backup",
    "restore_backup": "manage_backup",
    "record_pending_action": "treat_pending",
    "record_regularization_decision": "treat_pending",
}


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    return forwarded or request.remote_addr or ""


def _current_user() -> dict[str, Any] | None:
    return getattr(g, "current_user", None)


def _audit(
    action: str,
    module: str,
    result: str = "SUCCESS",
    *,
    critical: bool = True,
    **kwargs: Any,
) -> bool:
    """Record an authenticated action and optionally tolerate log outages.

    Critical mutations remain fail-closed. Read/export operations may choose a
    best-effort audit so a secondary logging failure does not discard a valid
    response that has already been generated.
    """

    actor = _current_user()
    if app.testing and app.config.get("AUTH_DISABLED"):
        actor = None
    try:
        record_audit(
            user=actor, action=action, module=module, result=result,
            ip_address=_client_ip(), **kwargs,
        )
        return True
    except Exception:
        app.logger.exception(
            "Audit logging failed action=%s module=%s critical=%s",
            action,
            module,
            critical,
        )
        if critical:
            raise
        return False


def _configuration_snapshot() -> bytes:
    connection = connect()
    try:
        payload = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "app_settings": [dict(row) for row in connection.execute("SELECT * FROM app_settings ORDER BY key")],
            "reconciliation_mappings": [dict(row) for row in connection.execute("SELECT * FROM reconciliation_mappings ORDER BY mapping_type,source_value")],
        }
    finally:
        connection.close()
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")


def _store_private_backup(content: bytes, name: str) -> str:
    if os.getenv("BLOB_READ_WRITE_TOKEN"):
        from vercel.blob import BlobClient

        with BlobClient() as client:
            blob = client.put(
                f"sisdev/backups/{name}", content, access="private",
                add_random_suffix=True, content_type="application/json",
            )
        return blob.pathname
    target = ROOT / "banco" / "backups" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return "local:" + str(target)


def _read_private_backup(storage_path: str) -> bytes:
    if storage_path.startswith("local:"):
        target = Path(storage_path[6:]).resolve()
        allowed = (ROOT / "banco" / "backups").resolve()
        target.relative_to(allowed)
        return target.read_bytes()
    from vercel.blob import BlobClient

    with BlobClient() as client:
        return bytes(client.get(storage_path, access="private"))


def _scoped_filters(values: dict[str, Any]) -> dict[str, Any]:
    result = dict(values)
    user = _current_user()
    if not user or user.get("profile") == "ADMINISTRADOR":
        return result
    centers = sorted(set(scope_values(user, "CENTER") + scope_values(user, "UNIT")))
    properties = sorted(set(scope_values(user, "PROPERTY")))
    requested_center = str(result.get("center") or "").strip()
    if requested_center and requested_center not in centers:
        raise PermissionError("Centro fora do escopo autorizado.")
    result["allowed_centers"] = centers
    result["allowed_properties"] = properties
    result["actor_user_id"] = int(user["id"])
    return result


def _is_admin(user: dict[str, Any] | None = None) -> bool:
    actor = user or _current_user()
    return bool(actor and actor.get("profile") == "ADMINISTRADOR")


def _owner_condition(*, alias: str = "") -> tuple[str, list[Any]]:
    """Return a fail-closed owner predicate for user-created orchestration rows."""

    if _is_admin():
        return "", []
    actor = _current_user()
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}created_by=?", [int(actor["id"])]


def _scope_snapshot(user: dict[str, Any]) -> tuple[int, str | None, str | None]:
    if user.get("profile") == "ADMINISTRADOR":
        return int(user["id"]), None, None
    centers = sorted(set(scope_values(user, "CENTER") + scope_values(user, "UNIT")))
    properties = sorted(set(scope_values(user, "PROPERTY")))
    return (
        int(user["id"]),
        json.dumps(centers, ensure_ascii=False),
        json.dumps(properties, ensure_ascii=False),
    )


@app.before_request
def enforce_security():
    if not request.path.startswith("/api/"):
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if app.testing and app.config.get("AUTH_DISABLED"):
        g.current_user = {
            "id": 0, "email": "test@sisdev.local", "display_name": "Teste",
            "profile": "ADMINISTRADOR", "active": True,
            "permissions": ["*"], "modules": ["*"], "scopes": [],
        }
        return None
    token = str(session.get("auth_token") or "")
    user_agent = request.headers.get("User-Agent", "")
    mutating = request.method not in {"GET", "HEAD", "OPTIONS"}
    user = validate_session(token, user_agent=user_agent, touch=not mutating)
    if not user:
        session.clear()
        return jsonify({"error": "Autenticação necessária.", "code": "AUTH_REQUIRED"}), 401
    g.current_user = user
    if mutating:
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf:
            return jsonify({"error": "Sessão inválida ou expirada.", "code": "CSRF_INVALID"}), 403
        confirmed_user = validate_session(token, csrf, user_agent=user_agent)
        if not confirmed_user:
            return jsonify({"error": "Sessão inválida ou expirada.", "code": "CSRF_INVALID"}), 403
        g.current_user = confirmed_user
    permission = ENDPOINT_PERMISSIONS.get(request.endpoint or "", "view")
    if not has_permission(user, permission):
        return jsonify({"error": "Você não possui permissão para esta operação.", "code": "FORBIDDEN"}), 403
    return None


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
    # Storage identifiers and private download URLs never belong in the browser.
    result.pop("blob_path", None)
    result.pop("blob_url", None)
    result.pop("created_by", None)
    result.pop("scope_centers_json", None)
    result.pop("scope_properties_json", None)
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
    result.pop("created_by", None)
    result.pop("scope_centers_json", None)
    result.pop("scope_properties_json", None)
    result["required_sources"] = [
        item for item in str(result.get("required_sources") or "").split(",") if item
    ]
    return result


def _load_job(job_id: int) -> Any:
    connection = connect()
    try:
        owner_sql, owner_params = _owner_condition()
        return connection.execute(
            "SELECT * FROM import_jobs WHERE id=?" + owner_sql,
            [job_id, *owner_params],
        ).fetchone()
    finally:
        connection.close()


def _get_or_create_batch(connection: Any, user: dict[str, Any]) -> Any:
    created_by, scope_centers_json, scope_properties_json = _scope_snapshot(user)
    batch = connection.execute(
        """SELECT * FROM import_batches
           WHERE status IN ('OPEN','IMPORTING') AND created_by=?
           ORDER BY id DESC LIMIT 1""",
        (created_by,),
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
        """INSERT INTO import_batches(
               run_id,created_by,scope_centers_json,scope_properties_json,status,required_sources
           ) VALUES (?,?,?,?, 'OPEN',?) RETURNING *""",
        (run_id, created_by, scope_centers_json, scope_properties_json, required),
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
        owner_sql, owner_params = _owner_condition()
        job = connection.execute(
            "SELECT * FROM import_jobs WHERE id=?" + owner_sql,
            [job_id, *owner_params],
        ).fetchone()
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


@app.post("/api/auth/login")
def auth_login():
    ensure_bootstrap_admin()
    data = request.get_json(silent=True) or {}
    user, message = authenticate(
        str(data.get("email") or ""), str(data.get("password") or ""), _client_ip()
    )
    if not user:
        record_audit(
            user=None, action="LOGIN", module="AUTH", result="FAILED",
            new_value={"email": str(data.get("email") or "").strip().casefold()},
            ip_address=_client_ip(),
        )
        connection = connect()
        try:
            configured = int(connection.execute("SELECT COUNT(*) n FROM app_users").fetchone()["n"] or 0)
        finally:
            connection.close()
        code = "SETUP_REQUIRED" if not configured else "INVALID_CREDENTIALS"
        if configured and message.startswith("Muitas tentativas"):
            return jsonify({"error": message, "code": "RATE_LIMITED"}), 429, {"Retry-After": "900"}
        return jsonify({"error": message if configured else "Administrador inicial ainda não configurado.", "code": code}), 401 if configured else 503
    token, csrf = create_session(
        int(user["id"]), _client_ip(), request.headers.get("User-Agent", "")
    )
    session.clear()
    session.permanent = True
    session["auth_token"] = token
    session["csrf_token_public"] = csrf
    g.current_user = user
    _audit("LOGIN", "AUTH", entity_type="USER", entity_id=user["id"])
    return jsonify({"ok": True, "user": user, "csrf_token": csrf})


@app.get("/api/auth/me")
def auth_me():
    return jsonify({
        "authenticated": True,
        "user": _current_user(),
        "csrf_token": session.get("csrf_token_public") or "",
    })


@app.post("/api/auth/logout")
def auth_logout():
    token = str(session.get("auth_token") or "")
    _audit("LOGOUT", "AUTH", entity_type="USER", entity_id=_current_user().get("id"))
    revoke_session(token)
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/users")
def list_users():
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT * FROM app_users WHERE deleted_at IS NULL ORDER BY display_name,email"
        ).fetchall()
        users = [public_user(row, connection=connection) for row in rows]
    finally:
        connection.close()
    return jsonify({"rows": users, "count": len(users), "profiles": list(PROFILES)})


@app.post("/api/auth/users")
def create_app_user():
    data = request.get_json(silent=True) or {}
    scopes = [
        (str(item.get("scope_type") or ""), str(item.get("scope_value") or ""))
        for item in data.get("scopes", []) if isinstance(item, dict)
    ]
    user = create_user(
        str(data.get("email") or ""), str(data.get("display_name") or ""),
        str(data.get("password") or ""), str(data.get("profile") or ""),
        scopes=scopes, active=bool(data.get("active", True)),
    )
    _audit("CREATE", "USERS", entity_type="USER", entity_id=user["id"], new_value=user)
    return jsonify({"ok": True, "user": user}), 201


@app.patch("/api/auth/users/<int:user_id>")
def update_app_user(user_id: int):
    data = request.get_json(silent=True) or {}
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM app_users WHERE id=? AND deleted_at IS NULL", (user_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "Usuário não encontrado."}), 404
        old_value = public_user(row, connection=connection)
        profile = str(data.get("profile", row["profile"])).strip().upper()
        if profile not in PROFILES:
            return jsonify({"error": "Perfil inválido."}), 400
        active = int(bool(data.get("active", bool(row["active"]))))
        if user_id == _current_user().get("id") and not active:
            return jsonify({"error": "Não é possível desativar o próprio usuário."}), 409
        if row["profile"] == "ADMINISTRADOR" and row["active"] and (profile != "ADMINISTRADOR" or not active):
            remaining_admins = connection.execute(
                """SELECT COUNT(*) n FROM app_users
                   WHERE id<>? AND profile='ADMINISTRADOR' AND active=1 AND deleted_at IS NULL""",
                (user_id,),
            ).fetchone()["n"]
            if not remaining_admins:
                return jsonify({"error": "É necessário manter ao menos um administrador ativo."}), 409
        email = normalize_email(data.get("email", row["email"]))
        if not email or len(email) > 254 or any(character.isspace() for character in email):
            return jsonify({"error": "Informe um usuário ou e-mail válido."}), 400
        duplicate = connection.execute(
            "SELECT id FROM app_users WHERE email=? AND id<>?", (email, user_id)
        ).fetchone()
        if duplicate:
            return jsonify({"error": "Já existe um usuário com esse identificador."}), 409
        display_name = str(data.get("display_name", row["display_name"])).strip()
        if not display_name:
            return jsonify({"error": "Informe o nome do usuário."}), 400
        password = str(data.get("password") or "")
        password_hash = hash_password(password) if password else row["password_hash"]
        connection.execute(
            """UPDATE app_users SET email=?,display_name=?,profile=?,active=?,password_hash=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (email, display_name, profile, active, password_hash, user_id),
        )
        if password or not active:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND revoked_at IS NULL",
                (user_id,),
            )
        if "scopes" in data:
            connection.execute("DELETE FROM user_scopes WHERE user_id=?", (user_id,))
            for item in data.get("scopes") or []:
                if not isinstance(item, dict):
                    continue
                scope_type = str(item.get("scope_type") or "").strip().upper()
                scope_value = str(item.get("scope_value") or "").strip()
                if scope_type in {"CENTER", "UNIT", "PROPERTY"} and scope_value:
                    connection.execute(
                        "INSERT INTO user_scopes(user_id,scope_type,scope_value) VALUES (?,?,?) ON CONFLICT(user_id,scope_type,scope_value) DO NOTHING",
                        (user_id, scope_type, scope_value),
                    )
        connection.commit()
        updated = connection.execute("SELECT * FROM app_users WHERE id=?", (user_id,)).fetchone()
        new_value = public_user(updated, connection=connection)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _audit(
        "UPDATE", "USERS", entity_type="USER", entity_id=user_id,
        old_value=old_value, new_value=new_value,
        justification=str(data.get("justification") or ""),
    )
    return jsonify({"ok": True, "user": new_value})


@app.delete("/api/auth/users/<int:user_id>")
def delete_app_user(user_id: int):
    if user_id == _current_user().get("id"):
        return jsonify({"error": "Não é possível excluir o próprio usuário."}), 409
    data = request.get_json(silent=True) or {}
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM app_users WHERE id=? AND deleted_at IS NULL", (user_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "Usuário não encontrado."}), 404
        old_value = public_user(row, connection=connection)
        if row["profile"] == "ADMINISTRADOR" and row["active"]:
            remaining_admins = connection.execute(
                """SELECT COUNT(*) n FROM app_users
                   WHERE id<>? AND profile='ADMINISTRADOR' AND active=1 AND deleted_at IS NULL""",
                (user_id,),
            ).fetchone()["n"]
            if not remaining_admins:
                return jsonify({"error": "É necessário manter ao menos um administrador ativo."}), 409
        deleted_identity = (
            f"deleted-{user_id}-{hashlib.sha256(str(row['email']).encode('utf-8')).hexdigest()[:12]}"
        )
        connection.execute(
            """UPDATE app_users SET email=?,display_name=?,active=0,password_hash=?,
               deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (
                deleted_identity,
                f"Usuário excluído #{user_id}",
                hash_password(os.urandom(32).hex()),
                user_id,
            ),
        )
        connection.execute("DELETE FROM user_scopes WHERE user_id=?", (user_id,))
        connection.execute(
            "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND revoked_at IS NULL",
            (user_id,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _audit(
        "DELETE", "USERS", entity_type="USER", entity_id=user_id,
        old_value=old_value, new_value={"deleted": True},
        justification=str(data.get("justification") or "Exclusão solicitada pelo administrador."),
    )
    return jsonify({"ok": True, "deleted_user_id": user_id})


@app.post("/api/admin/backups")
def create_backup():
    content = _configuration_snapshot()
    checksum = hashlib.sha256(content).hexdigest()
    name = f"config-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    storage_path = _store_private_backup(content, name)
    connection = connect()
    try:
        row = connection.execute(
            """INSERT INTO backup_registry(requested_by,provider,storage_path,checksum,status,details_json)
               VALUES (?,?,?,?, 'CREATED', ?) RETURNING *""",
            (
                _current_user().get("id"), "VERCEL_BLOB" if not storage_path.startswith("local:") else "LOCAL_PRIVATE",
                storage_path, checksum,
                json.dumps({"kind": "configuration", "restore_tested": False}, ensure_ascii=False),
            ),
        ).fetchone()
        connection.commit()
    finally:
        connection.close()
    _audit("CREATE_BACKUP", "BACKUP", entity_type="BACKUP", entity_id=row["id"], new_value={"checksum": checksum})
    return jsonify({"ok": True, "backup": {"id": row["id"], "status": row["status"], "checksum": checksum}}), 201


@app.post("/api/admin/backups/<int:backup_id>/restore-test")
def test_restore_backup(backup_id: int):
    connection = connect()
    try:
        row = connection.execute("SELECT * FROM backup_registry WHERE id=?", (backup_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return jsonify({"error": "Backup não encontrado."}), 404
    content = _read_private_backup(row["storage_path"])
    valid_checksum = hmac.compare_digest(hashlib.sha256(content).hexdigest(), row["checksum"])
    try:
        payload = json.loads(content)
        valid_structure = payload.get("version") == 1 and isinstance(payload.get("app_settings"), list) and isinstance(payload.get("reconciliation_mappings"), list)
    except (UnicodeDecodeError, json.JSONDecodeError):
        valid_structure = False
    result = "VERIFIED" if valid_checksum and valid_structure else "FAILED"
    connection = connect()
    try:
        connection.execute(
            "UPDATE backup_registry SET restore_result=? WHERE id=?", (result, backup_id)
        )
        connection.commit()
    finally:
        connection.close()
    _audit("TEST_RESTORE", "BACKUP", result=result, entity_type="BACKUP", entity_id=backup_id)
    return jsonify({"ok": result == "VERIFIED", "result": result}), 200 if result == "VERIFIED" else 422


@app.post("/api/admin/backups/<int:backup_id>/restore")
def restore_backup(backup_id: int):
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True or not str(data.get("justification") or "").strip():
        return jsonify({"error": "Confirmação e justificativa são obrigatórias para restaurar."}), 400
    connection = connect()
    try:
        row = connection.execute("SELECT * FROM backup_registry WHERE id=?", (backup_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return jsonify({"error": "Backup não encontrado."}), 404
    content = _read_private_backup(row["storage_path"])
    if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), row["checksum"]):
        return jsonify({"error": "O backup não passou na verificação de integridade."}), 422
    payload = json.loads(content)
    connection = connect()
    try:
        for setting in payload.get("app_settings", []):
            connection.execute(
                """INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""",
                (setting["key"], setting["value"]),
            )
        for mapping in payload.get("reconciliation_mappings", []):
            connection.execute(
                """INSERT INTO reconciliation_mappings(mapping_type,source_value,target_value,metadata_json,status)
                   VALUES (?,?,?,?,?) ON CONFLICT(mapping_type,source_value) DO UPDATE SET
                   target_value=excluded.target_value,metadata_json=excluded.metadata_json,
                   status=excluded.status,updated_at=CURRENT_TIMESTAMP""",
                (mapping["mapping_type"], mapping["source_value"], mapping["target_value"], mapping.get("metadata_json"), mapping.get("status", "ACTIVE")),
            )
        connection.execute(
            "UPDATE backup_registry SET restored_at=CURRENT_TIMESTAMP,restore_result='RESTORED' WHERE id=?",
            (backup_id,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _audit(
        "RESTORE", "BACKUP", entity_type="BACKUP", entity_id=backup_id,
        justification=str(data.get("justification") or ""),
    )
    return jsonify({"ok": True, "result": "RESTORED"})


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "queue": "vercel-workflow", "database": "neon-or-sqlite"})


@app.get("/api/dashboard")
def dashboard():
    try:
        filters = _scoped_filters(request.args.to_dict())
    except PermissionError as error:
        return jsonify({"error": str(error), "code": "SCOPE_FORBIDDEN"}), 403
    return jsonify(dashboard_v2(filters))


@app.get("/api/page/<page>")
def page(page: str):
    if page not in set(_current_user().get("modules") or []) and "*" not in set(_current_user().get("modules") or []):
        return jsonify({"error": "Módulo não autorizado para este perfil."}), 403
    if page == "logs" and not has_permission(_current_user(), "view_audit"):
        return jsonify({"error": "Trilha de auditoria não autorizada."}), 403
    if page == "users":
        if not has_permission(_current_user(), "manage_users"):
            return jsonify({"error": "Administração de usuários não autorizada."}), 403
        try:
            page_number = max(1, int(request.args.get("page", 1)))
        except (TypeError, ValueError):
            page_number = 1
        try:
            per_page = int(request.args.get("per_page", 50))
        except (TypeError, ValueError):
            per_page = 50
        if per_page not in {25, 50, 100, 250}:
            per_page = 50
        sort_column = {
            "display_name": "display_name", "email": "email", "profile": "profile",
            "active": "active", "created_at": "created_at",
        }.get(str(request.args.get("sort") or ""), "display_name")
        sort_direction = "DESC" if str(request.args.get("order") or "").lower() == "desc" else "ASC"
        connection = connect()
        try:
            total = int(connection.execute(
                "SELECT COUNT(*) n FROM app_users WHERE deleted_at IS NULL"
            ).fetchone()["n"] or 0)
            total_pages = (total + per_page - 1) // per_page if total else 0
            if total_pages and page_number > total_pages:
                page_number = total_pages
            offset = (page_number - 1) * per_page
            users = [public_user(row, connection=connection) for row in connection.execute(
                f"""SELECT * FROM app_users WHERE deleted_at IS NULL
                    ORDER BY {sort_column} {sort_direction},id LIMIT ? OFFSET ?""",
                (per_page, offset),
            )]
        finally:
            connection.close()
        return jsonify({
            "page": "users", "rows": users, "summary": None,
            "pagination": {
                "page": page_number, "per_page": per_page, "total": total,
                "total_pages": total_pages,
                "from": offset + 1 if users else 0, "to": offset + len(users),
            },
        })
    try:
        filters = _scoped_filters(request.args.to_dict())
    except PermissionError as error:
        return jsonify({"error": str(error), "code": "SCOPE_FORBIDDEN"}), 403
    return jsonify(page_records_v2(page, filters))


@app.get("/api/settings/rt-preference")
def get_rt_preference():
    return jsonify(rt_preference_options())


@app.get("/api/regularization/<document_id>")
def regularization_detail(document_id: str):
    try:
        filters = _scoped_filters(request.args.to_dict())
    except PermissionError as error:
        return jsonify({"error": str(error), "code": "SCOPE_FORBIDDEN"}), 403
    detail = regularization_document_detail(document_id, filters)
    if detail is None:
        return jsonify({"error": "Nota fiscal não encontrada na fila atual."}), 404
    document = detail.get("document") or {}
    _audit(
        "VIEW_DETAIL", "REGULARIZATION", entity_type="DOCUMENT", entity_id=document_id,
        new_value={"nf": document.get("numero_nfe"), "series": document.get("serie")},
        critical=False,
    )
    return jsonify(detail)


@app.post("/api/regularization/<document_id>/decisions")
def record_regularization_decision(document_id: str):
    data = request.get_json(silent=True) or {}
    decision_type = str(data.get("decision_type") or "").strip().upper()
    allowed = {
        "SELECT_RECIPE", "CONFIRM_RECIPE", "REJECT_RECIPE",
        "CONFIRM_MOVEMENT", "REJECT_MOVEMENT", "TREAT_PENDING",
    }
    if decision_type not in allowed:
        return jsonify({"error": "Tipo de decisão não suportado."}), 400
    selected_ids = data.get("selected_ids") or []
    if not isinstance(selected_ids, list):
        return jsonify({"error": "A seleção deve ser uma lista."}), 400
    selected_ids = list(dict.fromkeys(
        str(value).strip() for value in selected_ids if str(value).strip()
    ))[:200]
    movement_id = str(data.get("movement_id") or "").strip()
    if movement_id and movement_id not in selected_ids:
        selected_ids.append(movement_id)
    if decision_type == "CONFIRM_RECIPE" and not selected_ids:
        return jsonify({"error": "Selecione ao menos uma receita antes de confirmar."}), 400
    if decision_type in {"CONFIRM_MOVEMENT", "REJECT_MOVEMENT"} and not selected_ids:
        return jsonify({"error": "Selecione ao menos uma movimentação da saída original."}), 400
    try:
        detail_filters = _scoped_filters({})
    except PermissionError as error:
        return jsonify({"error": str(error), "code": "SCOPE_FORBIDDEN"}), 403
    detail = regularization_document_detail(document_id, detail_filters)
    if detail is None:
        return jsonify({"error": "Nota fiscal não encontrada na fila atual."}), 404
    recipe_ids = {str(item.get("recipe_id")) for item in detail.get("recipes", []) if item.get("recipe_id")}
    if decision_type in {"SELECT_RECIPE", "CONFIRM_RECIPE", "REJECT_RECIPE"}:
        unknown_ids = [value for value in selected_ids if value not in recipe_ids]
        if unknown_ids:
            return jsonify({"error": "Uma ou mais receitas não pertencem a esta nota fiscal."}), 400
    if decision_type == "CONFIRM_RECIPE":
        recipes = {str(item.get("recipe_id")): item for item in detail.get("recipes", [])}
        selected_total = sum(float(recipes[value].get("quantidade_receita") or 0) for value in selected_ids)
        required_total = float((detail.get("recipe_selection") or {}).get("required") or 0)
        tolerance = max(0.001, abs(required_total) * 0.000001)
        if abs(selected_total - required_total) > tolerance:
            return jsonify({
                "error": "O volume das receitas selecionadas não corresponde ao volume necessário.",
                "required": required_total,
                "selected": selected_total,
                "difference": round(required_total - selected_total, 6),
            }), 400
    if decision_type in {"CONFIRM_MOVEMENT", "REJECT_MOVEMENT"}:
        suggested_movements = {
            str(item.get("id")) for item in (detail.get("return_chain") or {}).get("movements", [])
            if item.get("id")
        }
        unknown_ids = [value for value in selected_ids if value not in suggested_movements]
        if not suggested_movements or unknown_ids:
            return jsonify({"error": "Uma ou mais movimentações não pertencem ao retorno desta nota fiscal."}), 400
    justification = str(data.get("justification") or "").strip()[:2000]
    status = "CONFIRMED" if decision_type.startswith("CONFIRM_") else "REJECTED" if decision_type.startswith("REJECT_") else "SAVED"
    connection = connect()
    try:
        row = connection.execute(
            """INSERT INTO document_decisions(
                   run_id,document_key,decision_type,selected_ids_json,status,justification,user_id
               ) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(run_id,document_key,decision_type) DO UPDATE SET
                   selected_ids_json=excluded.selected_ids_json,status=excluded.status,
                   justification=excluded.justification,user_id=excluded.user_id,
                   updated_at=CURRENT_TIMESTAMP
               RETURNING *""",
            (
                detail["run_id"], document_id, decision_type,
                json.dumps(selected_ids, ensure_ascii=False), status, justification,
                int(_current_user()["id"]),
            ),
        ).fetchone()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    document = detail.get("document") or {}
    _audit(
        decision_type, "REGULARIZATION", entity_type="DOCUMENT", entity_id=document_id,
        new_value={
            "nf": document.get("numero_nfe"), "selected_ids": selected_ids,
            "decision_status": status,
        },
        justification=justification,
    )
    payload = _row_dict(row)
    payload["selected_ids"] = selected_ids
    payload.pop("selected_ids_json", None)
    return jsonify({"ok": True, "decision": payload})


@app.post("/api/settings/rt-preference")
def post_rt_preference():
    value = str((request.get_json(silent=True) or {}).get("preferred_rt", "")).strip()
    old = rt_preference_options().get("preferred_rt")
    set_preferred_rt(value)
    _audit("UPDATE", "SETTINGS", entity_type="SETTING", entity_id="preferred_rt", old_value=old, new_value=value)
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


@app.post("/api/pending/<int:reconciliation_id>/actions")
def record_pending_action(reconciliation_id: int):
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip().upper()
    reason = str(data.get("reason") or "").strip()
    if action not in {"REGISTER_NOTE", "REQUEST_REVIEW", "CONFIRM_REGULARIZED"}:
        return jsonify({"error": "Ação de tratamento inválida."}), 400
    if not reason:
        return jsonify({"error": "Informe a justificativa do tratamento."}), 400
    connection = connect()
    try:
        scoped = _scoped_filters({})
        allowed_centers = scoped.get("allowed_centers")
        reconciliation = connection.execute(
            """SELECT r.id,r.status,r.diagnosis,e.center
               FROM reconciliations r
               JOIN expected_movements e ON e.id=r.expected_id
               WHERE r.id=?""",
            (reconciliation_id,),
        ).fetchone()
        if reconciliation is None:
            return jsonify({"error": "Pendência não encontrada."}), 404
        if allowed_centers is not None and str(reconciliation.get("center") or "") not in {
            str(value) for value in allowed_centers
        }:
            # Do not reveal whether an identifier exists outside the caller's scope.
            return jsonify({"error": "Pendência não encontrada."}), 404
        entry = connection.execute(
            """INSERT INTO action_history(reconciliation_id,action,user_name,reason)
               VALUES (?,?,?,?) RETURNING *""",
            (reconciliation_id, action, _current_user().get("email"), reason),
        ).fetchone()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _audit(
        "TREAT_PENDING", "PENDING", entity_type="RECONCILIATION", entity_id=reconciliation_id,
        old_value=_row_dict(reconciliation), new_value={"action": action, "reason": reason},
        justification=reason,
    )
    return jsonify({"ok": True, "action": _row_dict(entry)}), 201


@app.post("/api/mappings")
def save_mapping():
    data = request.get_json(silent=True) or {}
    mapping_type = str(data.get("mapping_type") or "").strip()
    source_value = str(data.get("source_value") or "").strip()
    target_value = str(data.get("target_value") or "").strip()
    can_manage = has_permission(_current_user(), "manage_mappings")
    can_suggest = has_permission(_current_user(), "suggest_mappings")
    if not can_manage and not can_suggest:
        return jsonify({"error": "Você não possui permissão para sugerir correspondências."}), 403
    status = str(data.get("status") or ("ACTIVE" if can_manage else "SUGGESTED")).strip().upper()
    if not can_manage:
        status = "SUGGESTED"
    metadata = data.get("metadata")
    if mapping_type not in MAPPING_TYPES:
        return jsonify({"error": "Tipo de mapeamento inválido."}), 400
    if not source_value or not target_value:
        return jsonify({"error": "Origem e destino são obrigatórios."}), 400
    if status not in {"ACTIVE", "INACTIVE", "SUGGESTED", "REJECTED"}:
        return jsonify({"error": "Status de mapeamento inválido."}), 400
    if metadata is not None and not isinstance(metadata, (dict, list)):
        return jsonify({"error": "metadata deve ser um objeto ou uma lista."}), 400
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
    connection = connect()
    try:
        previous = connection.execute(
            "SELECT * FROM reconciliation_mappings WHERE mapping_type=? AND source_value=?",
            (mapping_type, source_value),
        ).fetchone()
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
    audit_action = "APPROVE_MAPPING" if status == "ACTIVE" and previous and previous.get("status") == "SUGGESTED" else "SUGGEST_MAPPING" if status == "SUGGESTED" else "UPDATE" if previous else "CREATE"
    _audit(
        audit_action, "MAPPINGS", entity_type=mapping_type,
        entity_id=source_value, old_value=_row_dict(previous) if previous else None,
        new_value=_row_dict(row), justification=str(data.get("justification") or ""),
    )
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

    # Validate the real file signature and reject active workbook content. The
    # parser never executes macros, but rejecting them here narrows the attack
    # surface and gives the user an actionable message.
    header = upload.stream.read(8)
    upload.stream.seek(0)
    if extension == ".pdf" and not header.startswith(b"%PDF-"):
        return jsonify({"error": "O conteúdo enviado não é um PDF válido."}), 400
    if extension in {".xlsx", ".xls"}:
        spreadsheet_error = _spreadsheet_upload_error(upload.stream, extension)
        if spreadsheet_error:
            return jsonify({"error": spreadsheet_error}), 400
        if extension == ".xls" and header.startswith(b"PK"):
            app.logger.info(
                "Accepted OOXML workbook with legacy .xls filename",
                extra={"source": source, "upload_filename": secure_filename(upload.filename)},
            )

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
        actor = _current_user()
        batch = _get_or_create_batch(connection, actor)
        created_by, scope_centers_json, scope_properties_json = _scope_snapshot(actor)
        connection.execute(
            """UPDATE import_jobs SET status='SUPERSEDED',finished_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP
               WHERE batch_id=? AND source=? AND status<>'SUPERSEDED'""",
            (batch["id"], source),
        )
        job = connection.execute(
            """INSERT INTO import_jobs(
                   batch_id,run_id,created_by,scope_centers_json,scope_properties_json,
                   source,source_file,blob_path,blob_url,status
               ) VALUES (?,?,?,?,?,?,?,?,?,'QUEUED') RETURNING *""",
            (
                batch["id"],
                batch["run_id"],
                created_by,
                scope_centers_json,
                scope_properties_json,
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
    })
    _audit(
        "IMPORT_UPLOAD", "IMPORTS", entity_type="IMPORT_JOB", entity_id=job["id"],
        new_value={
            "source": source, "file": secure_filename(upload.filename),
            "size": request.content_length, "batch_id": batch["id"],
        },
    )
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
    response = _queue_job(job_id, restart=bool(data.get("restart")))
    _audit("IMPORT_PROCESS", "IMPORTS", entity_type="IMPORT_JOB", entity_id=job_id)
    return response


@app.get("/api/import/<int:job_id>")
def import_job_status(job_id: int):
    connection = connect()
    try:
        owner_sql, owner_params = _owner_condition()
        job = connection.execute(
            "SELECT * FROM import_jobs WHERE id=?" + owner_sql,
            [job_id, *owner_params],
        ).fetchone()
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
    response = _queue_job(job_id, manual_retry=True, restart=bool(data.get("restart")))
    _audit("IMPORT_RETRY", "IMPORTS", entity_type="IMPORT_JOB", entity_id=job_id)
    return response


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
    if not _is_admin():
        clauses.append("created_by=?")
        params.append(int(_current_user()["id"]))
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
        owner_sql, owner_params = _owner_condition()
        batch = connection.execute(
            "SELECT * FROM import_batches WHERE 1=1" + owner_sql + " ORDER BY id DESC LIMIT 1",
            owner_params,
        ).fetchone()
        if batch is None:
            return jsonify({"batch": None, "jobs": {}, "ready": False})
        readiness = _batch_readiness(connection, batch)
    finally:
        connection.close()
    return jsonify({"batch": _batch_payload(batch), **readiness})


def _requested_batch(connection: Any, data: dict[str, Any]) -> Any:
    owner_sql, owner_params = _owner_condition()
    candidates: set[int] = set()
    for value in data.get("batch_ids") or []:
        candidates.add(int(value))
    job_ids = [int(value) for value in (data.get("job_ids") or [])]
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        for row in connection.execute(
            f"SELECT DISTINCT batch_id FROM import_jobs WHERE id IN ({placeholders})" + owner_sql,
            [*job_ids, *owner_params],
        ):
            if row["batch_id"] is not None:
                candidates.add(int(row["batch_id"]))
    if len(candidates) > 1:
        raise ValueError("Todos os jobs devem pertencer ao mesmo ciclo.")
    if candidates:
        return connection.execute(
            "SELECT * FROM import_batches WHERE id=?" + owner_sql,
            [next(iter(candidates)), *owner_params],
        ).fetchone()
    return connection.execute(
        """SELECT * FROM import_batches
           WHERE status IN ('OPEN','IMPORTING','FAILED','RECONCILIATION_STARTING','RECONCILING')
        """ + owner_sql + " ORDER BY id DESC LIMIT 1",
        owner_params,
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
    _audit(
        "RECONCILE", "RECONCILIATION", entity_type="IMPORT_BATCH", entity_id=batch["id"],
        new_value={"workflow_run_id": workflow_run_id, "status": batch["status"]},
    )
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
        owner_sql, owner_params = _owner_condition()
        batch = connection.execute(
            "SELECT * FROM import_batches WHERE id=?" + owner_sql,
            [batch_id, *owner_params],
        ).fetchone()
        if batch is None:
            return jsonify({"error": "Ciclo não encontrado."}), 404
        readiness = _batch_readiness(connection, batch)
    finally:
        connection.close()
    return jsonify({"batch": _batch_payload(batch), **readiness})


@app.get("/api/export/<fmt>/<page_name>")
def export(fmt: str, page_name: str):
    if fmt not in {"csv", "xlsx"} or page_name not in EXPORTABLE_PAGES:
        return jsonify({"error": "Exportação não suportada."}), 404
    user = _current_user()
    modules = set(user.get("modules") or [])
    if page_name not in modules and "*" not in modules:
        return jsonify({"error": "Módulo não autorizado para este perfil."}), 403
    if page_name == "logs" and not has_permission(user, "view_audit"):
        return jsonify({"error": "Trilha de auditoria não autorizada."}), 403
    try:
        filters = _scoped_filters(request.args.to_dict())
    except PermissionError as error:
        return jsonify({"error": str(error), "code": "SCOPE_FORBIDDEN"}), 403
    filters["export"] = "1"
    if page_name == "reports":
        rows = validation_rows(filters=filters)
    elif page_name == "regularization":
        rows = regularization_export_rows(filters)
    else:
        rows = page_records_v2(page_name, filters).get("rows", [])
    columns = list(rows[0]) if rows else []
    if fmt == "csv":
        content = csv_bytes(columns, rows)
        response = content, 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="sisdev_{page_name}.csv"',
        }
    elif fmt == "xlsx":
        response = xlsx_bytes(columns, rows), 200, {
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Content-Disposition": f'attachment; filename="sisdev_{page_name}.xlsx"',
        }
    _audit(
        "EXPORT", "EXPORTS", entity_type="PAGE", entity_id=page_name,
        new_value={"format": fmt, "records": len(rows), "filters": filters},
        critical=False,
    )
    return response


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
