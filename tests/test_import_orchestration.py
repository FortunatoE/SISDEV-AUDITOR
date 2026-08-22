from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from api import index as api
from auditor import database
from auditor import engine
from workflow import imports as workflow_imports


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.sqlite")
    api.app.config.update(TESTING=True)
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
    assert result["done"] is True

    connection = database.connect()
    job = connection.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    connection.close()
    assert job["inserted_rows"] == 195
    assert job["duplicate_rows"] == 8
    assert job["cursor_row"] == 200
    assert job["status"] == "COMPLETED_WITH_WARNINGS"


def test_unknown_api_route_is_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.is_json
