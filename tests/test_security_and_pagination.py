from __future__ import annotations

import sys
import io
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from api import index as api
from auditor import database, engine
from auditor.exporting import csv_bytes
from auditor.security import create_user


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
