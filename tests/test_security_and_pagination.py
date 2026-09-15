from __future__ import annotations

import sys
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from api import index as api
from auditor import database, engine
from auditor.exporting import csv_bytes
from auditor.security import (
    AuditIdentityError,
    create_session,
    create_user,
    record_audit,
    validate_session,
)


@pytest.fixture()
def secure_client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SISDEV_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SISDEV_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "secure.sqlite")
    api.app.config.update(TESTING=True, AUTH_DISABLED=False, SECRET_KEY="test-secret")
    return api.app.test_client()


def _login(client, email: str, password: str):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    return response, (response.get_json() or {}).get("csrf_token", "")


def test_api_requires_authentication_and_valid_csrf(secure_client):
    assert secure_client.get("/api/dashboard").status_code == 401
    assert secure_client.get("/api/route-that-does-not-exist").status_code == 401
    create_user("admin@example.com", "Admin", "Senha-segura-123", "ADMINISTRADOR")
    login, csrf = _login(secure_client, "admin@example.com", "Senha-segura-123")
    assert login.status_code == 200
    assert csrf
    assert secure_client.get("/api/dashboard").status_code == 200
    assert secure_client.post("/api/mappings", json={}).status_code == 403
    response = secure_client.post(
        "/api/mappings",
        headers={"X-CSRF-Token": csrf},
        json={"mapping_type": "material_product", "source_value": "A", "target_value": "B"},
    )
    assert response.status_code == 200


def test_session_is_bound_to_the_originating_browser(secure_client):
    create_user("bound@example.com", "Bound", "Senha-segura-123", "CONSULTA")
    login = secure_client.post(
        "/api/auth/login",
        headers={"User-Agent": "SISDEV-Test-Browser-A"},
        json={"email": "bound@example.com", "password": "Senha-segura-123"},
    )
    assert login.status_code == 200
    assert secure_client.get(
        "/api/dashboard", headers={"User-Agent": "SISDEV-Test-Browser-B"}
    ).status_code == 401


def test_validated_session_keeps_the_application_user_id(secure_client):
    create_user("first@example.com", "First", "Senha-segura-123", "CONSULTA")
    user = create_user("second@example.com", "Second", "Senha-segura-123", "GESTOR")
    login, _csrf = _login(secure_client, user["email"], "Senha-segura-123")
    assert login.status_code == 200
    with secure_client.session_transaction() as browser_session:
        token = browser_session["auth_token"]
    validated = validate_session(token)
    assert validated["id"] == user["id"]


def test_session_expires_after_thirty_minutes_of_inactivity(secure_client):
    user = create_user("idle@example.com", "Idle", "Senha-segura-123", "CONSULTA")
    token, _csrf = create_session(user["id"], user_agent="SISDEV-Test")
    connection = database.connect()
    connection.execute(
        "UPDATE auth_sessions SET last_seen_at=? WHERE user_id=?",
        ((datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(), user["id"]),
    )
    connection.commit()
    connection.close()

    assert validate_session(token, user_agent="SISDEV-Test") is None
    connection = database.connect()
    revoked = connection.execute(
        "SELECT revoked_at FROM auth_sessions WHERE user_id=?", (user["id"],)
    ).fetchone()
    connection.close()
    assert revoked["revoked_at"] is not None


def test_session_never_exceeds_absolute_expiration(secure_client):
    user = create_user("absolute@example.com", "Absolute", "Senha-segura-123", "CONSULTA")
    token, _csrf = create_session(user["id"], user_agent="SISDEV-Test")
    connection = database.connect()
    connection.execute(
        "UPDATE auth_sessions SET expires_at=?,last_seen_at=CURRENT_TIMESTAMP WHERE user_id=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), user["id"]),
    )
    connection.commit()
    connection.close()
    assert validate_session(token, user_agent="SISDEV-Test") is None


def test_valid_activity_renews_session_last_seen(secure_client):
    user = create_user("active@example.com", "Active", "Senha-segura-123", "CONSULTA")
    token, _csrf = create_session(user["id"], user_agent="SISDEV-Test")
    previous = (datetime.now(timezone.utc) - timedelta(minutes=29)).isoformat()
    connection = database.connect()
    connection.execute(
        "UPDATE auth_sessions SET last_seen_at=? WHERE user_id=?", (previous, user["id"])
    )
    connection.commit()
    connection.close()

    assert validate_session(token, user_agent="SISDEV-Test") is not None
    connection = database.connect()
    renewed = connection.execute(
        "SELECT last_seen_at FROM auth_sessions WHERE user_id=?", (user["id"],)
    ).fetchone()
    connection.close()
    assert str(renewed["last_seen_at"]) != previous


def test_recent_session_does_not_write_on_every_read(secure_client, monkeypatch):
    user = create_user("recent@example.com", "Recent", "Senha-segura-123", "CONSULTA")
    token, _csrf = create_session(user["id"], user_agent="SISDEV-Test")
    original_execute = database.Connection.execute

    def reject_touch(connection, query, params=None):
        if "UPDATE auth_sessions SET last_seen_at" in query:
            raise AssertionError("sessão recente não deve gerar escrita")
        return original_execute(connection, query, params)

    monkeypatch.setattr(database.Connection, "execute", reject_touch)
    assert validate_session(token, user_agent="SISDEV-Test") is not None


def test_session_touch_failure_does_not_invalidate_valid_session(secure_client, monkeypatch):
    user = create_user("full-db@example.com", "Full DB", "Senha-segura-123", "CONSULTA")
    token, _csrf = create_session(user["id"], user_agent="SISDEV-Test")
    previous = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    connection = database.connect()
    connection.execute(
        "UPDATE auth_sessions SET last_seen_at=? WHERE user_id=?", (previous, user["id"])
    )
    connection.commit()
    connection.close()
    original_execute = database.Connection.execute

    def fail_touch(connection, query, params=None):
        if "UPDATE auth_sessions SET last_seen_at" in query:
            raise RuntimeError("database full")
        return original_execute(connection, query, params)

    monkeypatch.setattr(database.Connection, "execute", fail_touch)
    assert validate_session(token, user_agent="SISDEV-Test") is not None


def test_users_page_uses_database_pagination(secure_client):
    create_user("admin@example.com", "Admin", "Senha-segura-123", "ADMINISTRADOR")
    login, _csrf = _login(secure_client, "admin@example.com", "Senha-segura-123")
    assert login.status_code == 200
    response = secure_client.get("/api/page/users?page=1&per_page=25")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["pagination"] == {
        "page": 1, "per_page": 25, "total": 1, "total_pages": 1, "from": 1, "to": 1,
    }


def test_admin_can_edit_and_soft_delete_existing_user(secure_client):
    admin = create_user("admin@example.com", "Admin", "Senha-segura-123", "ADMINISTRADOR")
    target = create_user(
        "operator@example.com", "Operador", "Senha-segura-123", "OPERADOR",
        scopes=[("CENTER", "1001")],
    )
    login, csrf = _login(secure_client, admin["email"], "Senha-segura-123")
    assert login.status_code == 200

    updated = secure_client.patch(
        f"/api/auth/users/{target['id']}",
        headers={"X-CSRF-Token": csrf},
        json={
            "display_name": "Auditor editado", "email": "auditor@example.com",
            "profile": "AUDITOR", "active": True, "password": "Nova-senha-123",
            "scopes": [{"scope_type": "CENTER", "scope_value": "2001"}],
        },
    )
    assert updated.status_code == 200
    assert updated.get_json()["user"]["profile"] == "AUDITOR"
    assert updated.get_json()["user"]["email"] == "auditor@example.com"
    assert updated.get_json()["user"]["scopes"] == [
        {"scope_type": "CENTER", "scope_value": "2001"},
    ]

    deleted = secure_client.delete(
        f"/api/auth/users/{target['id']}",
        headers={"X-CSRF-Token": csrf},
        json={"justification": "Acesso encerrado"},
    )
    assert deleted.status_code == 200
    listing = secure_client.get("/api/page/users?page=1&per_page=25").get_json()
    assert listing["pagination"]["total"] == 1
    assert listing["rows"][0]["id"] == admin["id"]

    connection = database.connect()
    removed = connection.execute("SELECT * FROM app_users WHERE id=?", (target["id"],)).fetchone()
    scopes = connection.execute(
        "SELECT COUNT(*) n FROM user_scopes WHERE user_id=?", (target["id"],)
    ).fetchone()["n"]
    audit = connection.execute(
        "SELECT action,justification FROM audit_log WHERE module='USERS' AND entity_id=? ORDER BY id DESC",
        (str(target["id"]),),
    ).fetchone()
    connection.close()
    assert removed["active"] == 0 and removed["deleted_at"] is not None
    assert scopes == 0
    assert audit["action"] == "DELETE" and audit["justification"] == "Acesso encerrado"

    secure_client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    rejected, _ = _login(secure_client, "auditor@example.com", "Nova-senha-123")
    assert rejected.status_code == 401


def test_admin_cannot_delete_self_or_remove_last_active_admin(secure_client):
    admin = create_user("admin@example.com", "Admin", "Senha-segura-123", "ADMINISTRADOR")
    login, csrf = _login(secure_client, admin["email"], "Senha-segura-123")
    assert login.status_code == 200
    own_delete = secure_client.delete(
        f"/api/auth/users/{admin['id']}", headers={"X-CSRF-Token": csrf}, json={},
    )
    assert own_delete.status_code == 409
    downgrade = secure_client.patch(
        f"/api/auth/users/{admin['id']}",
        headers={"X-CSRF-Token": csrf},
        json={"profile": "CONSULTA", "active": True},
    )
    assert downgrade.status_code == 409


def test_profile_and_center_scope_are_enforced_in_backend(secure_client):
    create_user(
        "consulta@example.com", "Consulta", "Senha-segura-123", "CONSULTA",
        scopes=[("CENTER", "1001")],
    )
    login, csrf = _login(secure_client, "consulta@example.com", "Senha-segura-123")
    assert login.status_code == 200
    assert secure_client.get("/api/dashboard?center=2000").status_code == 403
    assert secure_client.post(
        "/api/mappings", headers={"X-CSRF-Token": csrf},
        json={"mapping_type": "material_product", "source_value": "A", "target_value": "B"},
    ).status_code == 403


def test_center_scope_covers_stock_movements_history_and_dashboard(secure_client):
    connection = database.connect()
    run_id = connection.execute(
        "INSERT INTO import_runs(status,summary_json) VALUES ('SUCCESS','{}') RETURNING id"
    ).fetchone()[0]
    source_ids = []
    for row_number, center in enumerate(("1001", "2000"), start=1):
        raw = {
            "Centro": center, "Texto breve material": f"PRODUTO {center}",
            "Lote": f"LOTE-{center}", "Lote Fabricante": f"FAB-{center}",
            "UMB": "L", "Utilização livre": 10, "Depósito": "D1",
        }
        source = connection.execute(
            """INSERT INTO source_records(run_id,source,source_file,row_number,fingerprint,raw_json)
               VALUES (?,'sap_stock','MB52.xlsx',?,?,?) RETURNING id""",
            (run_id, row_number, f"fp-{center}", json.dumps(raw)),
        ).fetchone()
        source_ids.append(source[0])
        connection.execute(
            """INSERT INTO expected_movements(
                   run_id,source_record_id,nf,series,direction,doc_date,sap_material,
                   material_key,lot,manufacturer_lot,quantity,unit,center,cnpj,status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, source[0], str(row_number), "1", "2", "2026-08-01",
                f"PRODUTO {center}", f"PRODUTO {center}", f"LOTE-{center}",
                f"FAB-{center}", 10, "L", center, f"CNPJ-{center}", "PENDENTE",
            ),
        )
        connection.execute(
            """INSERT INTO actual_movements(
                   run_id,nf,series,movement_type,movement_date,product,product_key,
                   lot,quantity,volume,unit,cnpj,status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, str(row_number), "1", "saida", "2026-08-01",
                f"PRODUTO {center}", f"PRODUTO {center}", f"FAB-{center}",
                1, 10, "L", f"CNPJ-{center}", "OK",
            ),
        )
    connection.execute(
        "INSERT INTO audit_issues(run_id,severity,category,message) VALUES (?,'ALTA','GLOBAL','Alerta global')",
        (run_id,),
    )
    connection.commit()
    connection.close()

    filters = {"allowed_centers": ["1001"]}
    stocks = engine.page_records_v2("stocks", filters)["rows"]
    movements = engine.page_records_v2("movements", filters)["rows"]
    measures = engine.page_records_v2("units_measure", filters)["rows"]
    history = engine.page_records_v2("history", filters)["rows"]
    dashboard = engine.dashboard_v2(filters)

    assert {row["centro"] for row in stocks} == {"1001"}
    assert {row["centro"] for row in movements} == {"1001"}
    assert measures == [{"unidade": "L", "ocorrencias": 1}]
    assert history == [{"source": "sap_stock", "source_file": "MB52.xlsx", "linhas": 1}]
    assert dashboard["options"]["centers"] == ["1001"]
    assert dashboard["issues"] == []


def test_export_requires_access_to_requested_module(secure_client):
    create_user("consulta@example.com", "Consulta", "Senha-segura-123", "CONSULTA")
    login, _csrf = _login(secure_client, "consulta@example.com", "Senha-segura-123")
    assert login.status_code == 200
    assert secure_client.get("/api/export/csv/regularization").status_code == 403
    assert secure_client.get("/api/export/csv/reports").status_code == 200


def test_pending_action_cannot_cross_center_scope(secure_client):
    create_user(
        "operator@example.com", "Operador", "Senha-segura-123", "OPERADOR",
        scopes=[("CENTER", "1001")],
    )
    connection = database.connect()
    run_id = connection.execute(
        "INSERT INTO import_runs(status,summary_json) VALUES ('SUCCESS','{}') RETURNING id"
    ).fetchone()[0]
    expected_id = connection.execute(
        """INSERT INTO expected_movements(run_id,nf,series,direction,center,status)
           VALUES (?,'200','1','2','2000','PENDENTE') RETURNING id""",
        (run_id,),
    ).fetchone()[0]
    reconciliation_id = connection.execute(
        """INSERT INTO reconciliations(run_id,expected_id,status,diagnosis,confidence)
           VALUES (?,?,'NAO_LANCADO','Teste','BAIXA') RETURNING id""",
        (run_id, expected_id),
    ).fetchone()[0]
    connection.commit()
    connection.close()

    login, csrf = _login(secure_client, "operator@example.com", "Senha-segura-123")
    assert login.status_code == 200
    response = secure_client.post(
        f"/api/pending/{reconciliation_id}/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "REQUEST_REVIEW", "reason": "Teste de escopo"},
    )
    assert response.status_code == 404
    connection = database.connect()
    assert connection.execute("SELECT COUNT(*) n FROM action_history").fetchone()["n"] == 0
    connection.close()


def test_import_jobs_and_batches_are_owner_scoped(secure_client):
    owner = create_user("owner@example.com", "Owner", "Senha-segura-123", "GESTOR")
    viewer = create_user("viewer@example.com", "Viewer", "Senha-segura-123", "GESTOR")
    connection = database.connect()
    run_id = connection.execute(
        "INSERT INTO import_runs(status,summary_json) VALUES ('RUNNING','{}') RETURNING id"
    ).fetchone()[0]
    batch_id = connection.execute(
        """INSERT INTO import_batches(run_id,created_by,status,required_sources)
           VALUES (?,?,'OPEN','sap_stock') RETURNING id""",
        (run_id, owner["id"]),
    ).fetchone()[0]
    job_id = connection.execute(
        """INSERT INTO import_jobs(batch_id,run_id,created_by,source,blob_path,status)
           VALUES (?,?,?,'sap_stock','private/path','QUEUED') RETURNING id""",
        (batch_id, run_id, owner["id"]),
    ).fetchone()[0]
    assert connection.execute(
        "SELECT created_by FROM import_jobs WHERE id=?", (job_id,)
    ).fetchone()["created_by"] == owner["id"]
    connection.commit()
    connection.close()

    login, csrf = _login(secure_client, viewer["email"], "Senha-segura-123")
    assert login.status_code == 200
    assert login.get_json()["user"]["profile"] == "GESTOR"
    status_response = secure_client.get(f"/api/import/{job_id}")
    assert status_response.status_code == 404
    assert secure_client.post(
        f"/api/import/{job_id}", headers={"X-CSRF-Token": csrf}, json={}
    ).status_code == 404
    assert secure_client.get(f"/api/reconcile/{batch_id}").status_code == 404
    assert secure_client.get("/api/import-jobs").get_json()["jobs"] == []


def test_session_secret_has_no_predictable_public_fallback():
    assert api.app.secret_key != "sisdev-local-development-only"


def test_inactive_user_cannot_login(secure_client):
    create_user("inactive@example.com", "Inativo", "Senha-segura-123", "CONSULTA", active=False)
    response, _csrf = _login(secure_client, "inactive@example.com", "Senha-segura-123")
    assert response.status_code == 401
    assert response.get_json()["code"] == "INVALID_CREDENTIALS"


def test_username_without_email_domain_can_authenticate(secure_client):
    create_user("emmanuel.fortunato", "Emmanuel Fortunato", "Senha-segura-123", "ADMINISTRADOR")
    response, csrf = _login(secure_client, "emmanuel.fortunato", "Senha-segura-123")
    assert response.status_code == 200
    assert csrf


def test_login_rate_limit_is_generic_and_temporary(secure_client):
    create_user("limited@example.com", "Limitado", "Senha-segura-123", "CONSULTA")
    for _ in range(5):
        response, _csrf = _login(secure_client, "limited@example.com", "senha-incorreta")
        assert response.status_code == 401
    blocked, _csrf = _login(secure_client, "limited@example.com", "Senha-segura-123")
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "RATE_LIMITED"


def test_invalid_upload_is_rejected_before_blob(secure_client):
    create_user("admin@example.com", "Admin", "Senha-segura-123", "ADMINISTRADOR")
    _login_response, csrf = _login(secure_client, "admin@example.com", "Senha-segura-123")
    response = secure_client.post(
        "/api/upload",
        headers={"X-CSRF-Token": csrf},
        data={"source": "sap_stock", "file": (io.BytesIO(b"not-an-xlsx"), "estoque.xlsx")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "XLSX" in response.get_json()["error"]


def test_agrotis_ooxml_with_legacy_xls_name_is_accepted_by_validation():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("xl/workbook.xml", "<workbook />")
    content.seek(0)

    assert api._spreadsheet_upload_error(content, ".xls") is None


def test_legacy_xls_diagnostic_uses_non_reserved_log_metadata():
    api.app.logger.info(
        "Accepted OOXML workbook with legacy .xls filename",
        extra={"source": "agrotis_recipe", "upload_filename": "ReceitasEmitidas.xls"},
    )


def test_arbitrary_zip_with_xls_name_is_rejected_by_validation():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("document.txt", "not a workbook")
    content.seek(0)

    assert "Excel válida" in api._spreadsheet_upload_error(content, ".xls")


def test_auditor_suggests_and_manager_approves_mapping(secure_client):
    create_user("auditor@example.com", "Auditor", "Senha-segura-123", "AUDITOR", scopes=[("CENTER", "1001")])
    _login_response, csrf = _login(secure_client, "auditor@example.com", "Senha-segura-123")
    suggested = secure_client.post(
        "/api/mappings", headers={"X-CSRF-Token": csrf},
        json={"mapping_type": "material_product", "source_value": "MAT-01", "target_value": "PRODUTO A"},
    )
    assert suggested.status_code == 200
    assert suggested.get_json()["mapping"]["status"] == "SUGGESTED"
    secure_client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    create_user("gestor@example.com", "Gestor", "Senha-segura-123", "GESTOR", scopes=[("CENTER", "1001")])
    _login_response, manager_csrf = _login(secure_client, "gestor@example.com", "Senha-segura-123")
    approved = secure_client.post(
        "/api/mappings", headers={"X-CSRF-Token": manager_csrf},
        json={"mapping_type": "material_product", "source_value": "MAT-01", "target_value": "PRODUTO A", "status": "ACTIVE", "justification": "Conferido"},
    )
    assert approved.status_code == 200
    assert approved.get_json()["mapping"]["status"] == "ACTIVE"


def test_private_configuration_backup_can_be_restore_tested(secure_client, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    create_user("admin@example.com", "Admin", "Senha-segura-123", "ADMINISTRADOR")
    _login_response, csrf = _login(secure_client, "admin@example.com", "Senha-segura-123")
    created = secure_client.post("/api/admin/backups", headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201
    backup_id = created.get_json()["backup"]["id"]
    tested = secure_client.post(
        f"/api/admin/backups/{backup_id}/restore-test", headers={"X-CSRF-Token": csrf}
    )
    assert tested.status_code == 200
    assert tested.get_json()["result"] == "VERIFIED"


def test_admin_archives_old_run_but_preserves_original_job(secure_client, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    monkeypatch.setattr(api, "_start_retention_workflow", lambda _run_id: "wfr-retention-test")
    admin = create_user("storage-admin@example.com", "Storage Admin", "Senha-segura-123", "ADMINISTRADOR")
    _login_response, csrf = _login(secure_client, admin["email"], "Senha-segura-123")
    connection = database.connect()
    run_ids = [
        connection.execute(
            "INSERT INTO import_runs(status,summary_json) VALUES ('SUCCESS','{}') RETURNING id"
        ).fetchone()[0]
        for _ in range(4)
    ]
    old_run = run_ids[0]
    connection.execute(
        """INSERT INTO import_jobs(
               run_id,source,source_file,blob_path,status,file_sha256,file_size,created_by
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (old_run, "sap_stock", "MB52.xlsx", "sisdev/sap_stock/MB52-private.xlsx", "COMPLETED", "abc", 123, admin["id"]),
    )
    connection.execute(
        """INSERT INTO source_records(run_id,source,source_file,row_number,fingerprint,raw_json)
           VALUES (?,?,?,?,?,?)""",
        (old_run, "sap_stock", "MB52.xlsx", 1, "fingerprint", "{}"),
    )
    connection.commit()
    connection.close()

    response = secure_client.post(
        f"/api/admin/storage/archive/{old_run}",
        headers={"X-CSRF-Token": csrf},
        json={"confirmation": f"ARQUIVAR {old_run}", "justification": "Teste de retenção"},
    )

    assert response.status_code == 202
    assert response.get_json()["archive"]["released_rows"] == 0
    assert response.get_json()["archive"]["workflow_run_id"] == "wfr-retention-test"
    connection = database.connect()
    assert connection.execute("SELECT status FROM import_runs WHERE id=?", (old_run,)).fetchone()["status"] == "SUCCESS"
    assert connection.execute("SELECT COUNT(*) AS n FROM source_records WHERE run_id=?", (old_run,)).fetchone()["n"] == 1
    assert connection.execute("SELECT COUNT(*) AS n FROM import_jobs WHERE run_id=?", (old_run,)).fetchone()["n"] == 1
    archive = connection.execute("SELECT * FROM run_archives WHERE run_id=?", (old_run,)).fetchone()
    connection.close()
    assert archive["status"] == "QUEUED"
    assert archive["retained_originals"] == 1
    assert str(archive["manifest_path"]).startswith("local:")


def test_audit_resolves_stale_session_id_by_authenticated_email(secure_client):
    user = create_user("audit@example.com", "Audit", "Senha-segura-123", "AUDITOR")
    record_audit(
        user={"id": user["id"] + 999, "email": user["email"]},
        action="EXPORT",
        module="EXPORTS",
        result="SUCCESS",
    )
    connection = database.connect()
    audit = connection.execute("SELECT user_id,user_email FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    connection.close()
    assert audit["user_id"] == user["id"]
    assert audit["user_email"] == user["email"]


def test_audit_rejects_unknown_authenticated_actor(secure_client):
    with pytest.raises(AuditIdentityError):
        record_audit(
            user={"id": 999_999, "email": "missing@example.com"},
            action="EXPORT",
            module="EXPORTS",
            result="SUCCESS",
        )


def test_noncritical_audit_failure_does_not_break_export(secure_client, monkeypatch):
    create_user("admin-export@example.com", "Admin", "Senha-segura-123", "ADMINISTRADOR")
    login, _csrf = _login(secure_client, "admin-export@example.com", "Senha-segura-123")
    assert login.status_code == 200

    def broken_audit(**_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(api, "record_audit", broken_audit)
    response = secure_client.get("/api/export/csv/pending")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv")


def test_local_security_schema_enforces_audit_user_foreign_key(secure_client):
    connection = database.connect()
    foreign_keys = connection.execute("PRAGMA foreign_key_list(audit_log)").fetchall()
    connection.close()
    assert any(row["table"] == "app_users" and row["from"] == "user_id" for row in foreign_keys)


def test_pagination_contract_and_allowed_sizes():
    rows = [{"id": value, "name": f"Registro {value}"} for value in range(1, 121)]
    page, metadata = engine._paginate_rows(rows, {"page": "2", "per_page": "50"})
    assert [row["id"] for row in page[:2]] == [51, 52]
    assert metadata == {"page": 2, "per_page": 50, "total": 120, "total_pages": 3, "from": 51, "to": 100}
    _page, fallback = engine._paginate_rows(rows, {"per_page": "9999"})
    assert fallback["per_page"] == 50


def test_balance_diagnostic_covers_full_partial_and_zero_stock():
    full = engine._balance_diagnostic({"quantidade_sisdev": 120}, 100, "L")
    partial = engine._balance_diagnostic({"quantidade_sisdev": 80}, 100, "L")
    empty = engine._balance_diagnostic(None, 40, "L")
    assert full["status_saldo_codigo"] == "OK" and full["saldo_apos_lancamento"] == 20
    assert partial["status_saldo_codigo"] == "SALDO_PARCIAL" and partial["falta_saldo"] == 20
    assert empty["status_saldo_codigo"] == "SEM_SALDO" and empty["falta_saldo"] == 40


def test_csv_export_neutralizes_formula_cells():
    content = csv_bytes(["produto"], [{"produto": "=HYPERLINK(\"https://invalid\")"}]).decode("utf-8-sig")
    assert "'=HYPERLINK" in content


def test_regularization_decision_validates_document_recipes_and_audits(secure_client, monkeypatch):
    user = create_user("operator@example.com", "Operador", "Senha-segura-123", "OPERADOR")
    login, csrf = _login(secure_client, user["email"], "Senha-segura-123")
    assert login.status_code == 200

    detail = {
        "run_id": 77,
        "document_id": "doc-123",
        "document": {"numero_nfe": "123", "serie": "1", "centro": "1001"},
        "items": [],
        "recipes": [
            {"recipe_id": "10", "quantidade_receita": 30},
            {"recipe_id": "11", "quantidade_receita": 10},
        ],
        "recipe_selection": {"required": 40, "mode": "MULTIPLE"},
        "return_chain": None,
    }
    monkeypatch.setattr(api, "regularization_document_detail", lambda *_args, **_kwargs: detail)

    unknown = secure_client.post(
        "/api/regularization/doc-123/decisions",
        headers={"X-CSRF-Token": csrf},
        json={"decision_type": "CONFIRM_RECIPE", "selected_ids": ["999"]},
    )
    assert unknown.status_code == 400

    mismatch = secure_client.post(
        "/api/regularization/doc-123/decisions",
        headers={"X-CSRF-Token": csrf},
        json={"decision_type": "CONFIRM_RECIPE", "selected_ids": ["10"]},
    )
    assert mismatch.status_code == 400
    assert mismatch.get_json()["difference"] == 10

    confirmed = secure_client.post(
        "/api/regularization/doc-123/decisions",
        headers={"X-CSRF-Token": csrf},
        json={
            "decision_type": "CONFIRM_RECIPE", "selected_ids": ["10", "11"],
            "justification": "Volumes conferidos",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["decision"]["status"] == "CONFIRMED"

    connection = database.connect()
    decision = connection.execute(
        "SELECT * FROM document_decisions WHERE run_id=? AND document_key=?",
        (77, "doc-123"),
    ).fetchone()
    audit = connection.execute(
        "SELECT action,result FROM audit_log WHERE module='REGULARIZATION' AND entity_id='doc-123'"
    ).fetchone()
    connection.close()
    assert decision["selected_ids_json"] == '["10", "11"]'
    assert audit["action"] == "CONFIRM_RECIPE" and audit["result"] == "SUCCESS"


def test_return_decision_accepts_multiple_related_movements(secure_client, monkeypatch):
    user = create_user("return-operator@example.com", "Operador", "Senha-segura-123", "OPERADOR")
    login, csrf = _login(secure_client, user["email"], "Senha-segura-123")
    assert login.status_code == 200
    detail = {
        "run_id": 88,
        "document_id": "return-doc",
        "document": {"numero_nfe": "300", "serie": "1", "centro": "1001"},
        "items": [], "recipes": [], "recipe_selection": {"required": 40},
        "return_chain": {"movements": [{"id": 20}, {"id": 21}]},
    }
    monkeypatch.setattr(api, "regularization_document_detail", lambda *_args, **_kwargs: detail)

    unknown = secure_client.post(
        "/api/regularization/return-doc/decisions",
        headers={"X-CSRF-Token": csrf},
        json={"decision_type": "CONFIRM_MOVEMENT", "selected_ids": ["999"]},
    )
    assert unknown.status_code == 400

    confirmed = secure_client.post(
        "/api/regularization/return-doc/decisions",
        headers={"X-CSRF-Token": csrf},
        json={
            "decision_type": "CONFIRM_MOVEMENT", "selected_ids": ["20", "21"],
            "justification": "Saídas do lote conferidas",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["decision"]["selected_ids"] == ["20", "21"]


def test_work_queue_updates_are_persistent_and_center_scoped(secure_client, monkeypatch):
    manager = create_user(
        "queue-manager@example.com", "Gestora da fila", "Senha-segura-123", "GESTOR",
        scopes=[("CENTER", "0714")],
    )
    create_user(
        "queue-outsider@example.com", "Operador externo", "Senha-segura-123", "OPERADOR",
        scopes=[("CENTER", "9999")],
    )
    source_path = Path(database.DB_PATH).parent / "queue-source.xlsx"
    source_path.touch()
    source_row = {
        "Número de nota fiscal eletrônica": 456,
        "Séries": 1,
        "Data documento": "12/09/2026",
        "Texto breve material": "PRODUTO FILA",
        "Lote": "LOTE-FILA",
        "Lote Fabricante": "FAB-FILA",
        "Quantidade": 20,
        "UMB": "L",
        "Centro": "0714",
        "CNPJ": "01.722.958/0014-73",
    }
    run_id = engine.create_import_run()
    monkeypatch.setattr(engine, "_read_source", lambda *_args: [(2, source_row)])
    engine.import_source(run_id, "sap_exit_current", source_path)
    engine.reconcile_run(run_id, require_complete=False)

    login, csrf = _login(secure_client, manager["email"], "Senha-segura-123")
    assert login.status_code == 200
    queue = secure_client.get("/api/work-items").get_json()
    document_id = queue["rows"][0]["document_id"]
    updated = secure_client.patch(
        f"/api/work-items/{document_id}", headers={"X-CSRF-Token": csrf},
        json={
            "status": "EM_ANALISE", "priority": "ALTA",
            "due_date": "2026-09-15", "assigned_to": manager["id"],
        },
    )
    assert updated.status_code == 200
    commented = secure_client.post(
        f"/api/work-items/{document_id}/comments", headers={"X-CSRF-Token": csrf},
        json={"comment": "Tratamento iniciado."},
    )
    assert commented.status_code == 201
    secure_client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    _outsider_login, outsider_csrf = _login(
        secure_client, "queue-outsider@example.com", "Senha-segura-123",
    )
    forbidden = secure_client.patch(
        f"/api/work-items/{document_id}", headers={"X-CSRF-Token": outsider_csrf},
        json={"status": "REGULARIZADA"},
    )
    assert forbidden.status_code == 404
