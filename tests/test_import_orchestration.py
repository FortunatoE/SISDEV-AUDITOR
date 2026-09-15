from __future__ import annotations

import asyncio
import io
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from api import index as api
from auditor import database
from auditor import engine
from auditor.security import create_user
from workflow import imports as workflow_imports


def test_postgres_migrations_are_disabled_by_default_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("SISDEV_AUTO_MIGRATE", raising=False)
    assert database.postgres_migrations_enabled() is False
    monkeypatch.setenv("SISDEV_AUTO_MIGRATE", "1")
    assert database.postgres_migrations_enabled() is True


def test_storage_quota_error_is_actionable():
    error = RuntimeError("could not extend file because project size limit (512 MB) has been exceeded")
    assert api._safe_error(error) == (
        "O banco Neon atingiu o limite de armazenamento. "
        "Libere espaço ou amplie o plano para continuar."
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    api.app.config.update(TESTING=True, AUTH_DISABLED=True)
    return api.app.test_client()


def _create_job(*, status="QUEUED", source="sap_entry_current"):
    connection = database.connect()
    run_id = connection.execute(
        "INSERT INTO import_runs(status,summary_json) VALUES ('RUNNING',?) RETURNING id",
        (json.dumps({"sources": {}, "source_details": {}, "warnings": []}),),
    ).fetchone()[0]
    batch_id = connection.execute(
        "INSERT INTO import_batches(run_id,status) VALUES (?,'OPEN') RETURNING id",
        (run_id,),
    ).fetchone()[0]
    job_id = connection.execute(
        """INSERT INTO import_jobs(batch_id,run_id,source,source_file,blob_path,blob_url,status)
           VALUES (?,?,?,?,?,?,?) RETURNING id""",
        (batch_id, run_id, source, "source.xlsx", f"sisdev/{source}/source.xlsx", "https://blob.invalid/source.xlsx", status),
    ).fetchone()[0]
    connection.commit()
    connection.close()
    return job_id, batch_id, run_id


def _valid_xlsx_bytes() -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("xl/workbook.xml", "<workbook />")
    return content.getvalue()


def test_duplicate_upload_reuses_existing_job_without_second_blob(client, monkeypatch):
    puts = []

    class FakeBlobClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def put(self, pathname, stream, **_kwargs):
            puts.append(pathname)
            return types.SimpleNamespace(
                pathname=f"{pathname}-private", download_url="https://blob.invalid/private", url=None
            )

    monkeypatch.setitem(sys.modules, "vercel.blob", types.SimpleNamespace(BlobClient=FakeBlobClient))
    payload = _valid_xlsx_bytes()
    first = client.post(
        "/api/upload", data={"source": "sap_stock", "file": (io.BytesIO(payload), "MB52.xlsx")},
        content_type="multipart/form-data",
    )
    second = client.post(
        "/api/upload", data={"source": "sap_stock", "file": (io.BytesIO(payload), "MB52.xlsx")},
        content_type="multipart/form-data",
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json()["duplicate_upload"] is True
    assert second.get_json()["job_id"] == first.get_json()["job_id"]
    assert len(puts) == 1
    connection = database.connect()
    assert connection.execute("SELECT COUNT(*) AS n FROM import_jobs").fetchone()["n"] == 1
    connection.close()


def test_storage_overview_protects_configured_recent_cycles(client):
    connection = database.connect()
    for _ in range(4):
        connection.execute("INSERT INTO import_runs(status,summary_json) VALUES ('SUCCESS','{}')")
    connection.commit()
    connection.close()

    response = client.get("/api/admin/storage")

    assert response.status_code == 200
    body = response.get_json()
    assert body["keep_recent_runs"] == 3
    assert len(body["cycles"]) == 4
    assert [row["protected"] for row in body["cycles"]] == [True, True, True, False]


def test_retention_worker_deletes_in_chunks_and_finishes_archive(client):
    user = create_user("retention@example.com", "Retention", "Senha-segura-123", "ADMINISTRADOR")
    connection = database.connect()
    run_id = connection.execute(
        "INSERT INTO import_runs(status,summary_json) VALUES ('SUCCESS','{}') RETURNING id"
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO run_archives(
               run_id,requested_by,manifest_path,manifest_checksum,status,retained_originals
           ) VALUES (?,?,?,?,?,?)""",
        (run_id, user["id"], "local:manifest", "checksum", "QUEUED", 1),
    )
    connection.executemany(
        """INSERT INTO source_records(run_id,source,source_file,row_number,fingerprint,raw_json)
           VALUES (?,?,?,?,?,?)""",
        [(run_id, "sap_stock", "MB52.xlsx", index, f"fp-{index}", "{}") for index in range(1, 4)],
    )
    connection.commit()
    connection.close()

    assert workflow_imports._archive_chunk(run_id, "source_records") == 3
    assert workflow_imports._archive_chunk(run_id, "source_records") == 0
    result = workflow_imports._finish_archive(run_id)

    assert result["released_rows"] == 3
    connection = database.connect()
    assert connection.execute("SELECT status FROM import_runs WHERE id=?", (run_id,)).fetchone()["status"] == "ARCHIVED"
    assert connection.execute("SELECT status FROM run_archives WHERE run_id=?", (run_id,)).fetchone()["status"] == "COMPLETED"
    connection.close()


def test_start_and_get_import_job_are_async_and_idempotent(client, monkeypatch):
    job_id, batch_id, run_id = _create_job()
    calls = []

    def starter(value):
        calls.append(value)
        return "wfr_test"

    monkeypatch.setattr(api, "_start_import_workflow", starter)
    response = client.post(f"/api/import/{job_id}")
    assert response.status_code == 202
    assert response.get_json()["workflow_run_id"] == "wfr_test"
    assert response.get_json()["batch_id"] == batch_id
    assert response.get_json()["run_id"] == run_id

    repeated = client.post(f"/api/import/{job_id}")
    assert repeated.status_code == 202
    assert calls == [job_id]

    status = client.get(f"/api/import/{job_id}")
    assert status.status_code == 200
    assert status.get_json()["status"] == "QUEUED"
    assert status.get_json()["progress_percent"] == 0


def test_reconciliation_rejects_incomplete_batch(client):
    _job_id, batch_id, _run_id = _create_job(status="COMPLETED")
    response = client.post("/api/reconcile", json={"batch_ids": [batch_id]})
    assert response.status_code == 409
    body = response.get_json()
    assert body["ready"] is False
    assert "sisdev_stock" in body["missing_sources"]


def test_import_health_summarizes_current_cycle(client):
    job_id, batch_id, _run_id = _create_job(status="FAILED", source="agrotis_recipe")
    connection = database.connect()
    connection.execute(
        """UPDATE import_jobs SET processed_rows=6000,total_rows=36807,inserted_rows=5998,
           duplicate_rows=2,error_rows=1,error_message='Falha de teste' WHERE id=?""",
        (job_id,),
    )
    connection.execute(
        "INSERT INTO import_job_events(job_id,status,processed_rows,total_rows,message) VALUES (?,?,?,?,?)",
        (job_id, "FAILED", 6000, 36807, "Último lote confirmado."),
    )
    connection.commit()
    connection.close()

    response = client.get("/api/import-health")

    assert response.status_code == 200
    body = response.get_json()
    assert body["batch"]["id"] == batch_id
    assert body["summary"] == {
        "required": 8, "completed": 0, "processing": 0, "waiting": 0,
        "failed": 1, "missing": 7, "progress_percent": 0.0,
    }
    recipe = next(row for row in body["sources"] if row["source"] == "agrotis_recipe")
    assert recipe["processed_rows"] == 6000
    assert recipe["inserted_rows"] == 5998
    assert recipe["last_message"] == "Último lote confirmado."
    assert "retomar" in recipe["action_recommended"]


def test_import_health_without_batch_is_actionable(client):
    response = client.get("/api/import-health")

    assert response.status_code == 200
    body = response.get_json()
    assert body["batch"] is None
    assert body["summary"]["missing"] == 8
    assert body["alerts"] == ["Nenhum ciclo de importação foi iniciado."]


def test_lot_trace_separates_sap_and_sisdev_running_balances(client):
    engine.RECIPE_CACHE.clear()
    connection = database.connect()
    run_id = connection.execute(
        "INSERT INTO import_runs(status,summary_json) VALUES ('SUCCESS','{}') RETURNING id"
    ).fetchone()[0]
    connection.executemany(
        """INSERT INTO expected_movements(
               run_id,nf,series,direction,doc_date,sap_material,material_key,lot,
               manufacturer_lot,quantity,unit,center,cnpj,status
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (run_id, "000000123", "1", "1", "2026-08-01", "PRODUTO A", "PRODUTO A", "LOTE1", "LOTE1", 100, "L", "1001", "01722958000100", "PENDENTE"),
            (run_id, "000000124", "1", "2", "2026-08-02", "PRODUTO A", "PRODUTO A", "LOTE1", "LOTE1", 40, "L", "1001", "01722958000100", "PENDENTE"),
        ],
    )
    connection.executemany(
        """INSERT INTO actual_movements(
               run_id,nf,series,movement_type,movement_date,product,product_key,lot,
               quantity,volume,unit,cnpj,status
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (run_id, "000000123", "1", "entrada", "2026-08-01", "PRODUTO A", "PRODUTO A", "LOTE1", 10, 10, "L", "01722958000100", "LANÇADO"),
            (run_id, "000000124", "1", "saida", "2026-08-02", "PRODUTO A", "PRODUTO A", "LOTE1", 4, 10, "L", "01722958000100", "LANÇADO"),
        ],
    )
    connection.execute(
        """INSERT INTO source_records(run_id,source,source_file,row_number,fingerprint,raw_json)
           VALUES (?,?,?,?,?,?)""",
        (run_id, "agrotis_recipe", "receitas.xls", 1, "recipe-1", json.dumps({
            "Data de Emissão": "01/08/2026", "Produto": "PRODUTO A",
            "Número do receituário": "REC-1", "Nota Fiscal": "123",
            "Quantidade": 100, "Unidade Quantidade": "L", "Nome RT": "RT TESTE",
        })),
    )
    connection.commit()
    connection.close()

    options = client.get("/api/lot-trace/options?search=LOTE1").get_json()["options"]
    assert options == [{"label": "PRODUTO A & LOTE1", "product": "PRODUTO A", "lot": "LOTE1"}]

    response = client.get("/api/lot-trace?product=PRODUTO%20A&lot=LOTE1")
    assert response.status_code == 200
    body = response.get_json()
    assert body["summary"] == {
        "events": 5, "sap_movements": 2, "sisdev_movements": 2,
        "recipes": 1, "audit_actions": 0,
    }
    assert body["events"][-1]["sap_running_balance"] == 60
    assert body["events"][-1]["sisdev_running_balance"] == 60
    assert "saldo oficial" in body["balance_note"].lower()


def test_mapping_upsert_and_list(client):
    first = client.post(
        "/api/mappings",
        json={
            "mapping_type": "material_product",
            "source_value": "MAT-001",
            "target_value": "PRODUTO A",
            "metadata": {"note": "manual"},
        },
    )
    assert first.status_code == 200

    second = client.post(
        "/api/mappings",
        json={
            "mapping_type": "material_product",
            "source_value": "MAT-001",
            "target_value": "PRODUTO A REVISTO",
        },
    )
    assert second.status_code == 200

    listing = client.get("/api/mappings?type=material_product")
    assert listing.status_code == 200
    assert listing.get_json()["count"] == 1
    assert listing.get_json()["rows"][0]["target_value"] == "PRODUTO A REVISTO"


def test_workflow_retry_resumes_cursor_and_keeps_cumulative_progress(client, monkeypatch, tmp_path):
    job_id, _batch_id, _run_id = _create_job()
    connection = database.connect()
    connection.execute(
        """UPDATE import_jobs SET cursor_row=100,processed_rows=100,total_rows=200,
           inserted_rows=95,duplicate_rows=5,warning_json='[\"anterior\"]' WHERE id=?""",
        (job_id,),
    )
    connection.commit()
    connection.close()

    local_file = tmp_path / "source.xlsx"
    local_file.write_bytes(b"PK")
    monkeypatch.setattr(workflow_imports, "_download_job_blob", lambda _job: local_file)
    seen = {}

    def fake_import_source(run_id, source, path, **kwargs):
        seen.update(kwargs)
        kwargs["progress_callback"]({
            "processed_rows": 150,
            "total_rows": 200,
            "imported_rows": 45,
            "duplicate_rows": 2,
            "warnings": ["novo"],
        })
        return {
            "processed_rows": 200,
            "next_cursor": 200,
            "total_rows": 200,
            "imported_rows": 100,
            "duplicate_rows": 3,
            "warnings": ["novo"],
            "done": True,
        }

    monkeypatch.setattr(engine, "import_source", fake_import_source)
    result = workflow_imports._process_source_once(job_id)
    assert seen["cursor"] == 100
    assert seen["max_rows"] == 5000
    assert result["done"] is True

    connection = database.connect()
    job = connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    connection.close()
    assert job["inserted_rows"] == 195
    assert job["duplicate_rows"] == 8
    assert job["cursor_row"] == 200
    assert job["status"] == "COMPLETED_WITH_WARNINGS"


def test_workflow_processes_source_in_durable_steps_until_done(monkeypatch):
    cursors = iter((5000, 10000, 12000))
    calls = []

    async def fake_step(job_id):
        cursor = next(cursors)
        calls.append((job_id, cursor))
        return {
            "job_id": job_id,
            "processed_rows": cursor,
            "total_rows": 12000,
            "done": cursor == 12000,
        }

    monkeypatch.setattr(workflow_imports, "process_import_source", fake_step)
    result = asyncio.run(workflow_imports.process_import_job.func(99))

    assert result["done"] is True
    assert calls == [(99, 5000), (99, 10000), (99, 12000)]


def test_unknown_api_route_is_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.is_json
