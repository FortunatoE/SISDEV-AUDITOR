"""Manual full-data smoke validation; never writes to the application database."""

import os
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor import database, engine  # noqa: E402


def main():
    os.environ.pop("DATABASE_URL", None)
    with tempfile.TemporaryDirectory() as directory:
        database.DB_PATH = Path(directory) / "full.sqlite"
        run_id = engine.create_import_run()
        started = time.perf_counter()
        for source, path, _ in engine.SOURCES:
            source_started = time.perf_counter()
            result = engine.import_source(run_id, source, path, batch_size=1000)
            print(source, result["imported_rows"], result["duplicate_rows"], round(time.perf_counter() - source_started, 2), flush=True)
        print("import_seconds", round(time.perf_counter() - started, 2), flush=True)
        reconciliation_started = time.perf_counter()
        summary = engine.reconcile_run(run_id)
        print("reconcile_seconds", round(time.perf_counter() - reconciliation_started, 2), summary["expected_rows"], summary["actual_rows"], flush=True)
        for page in ("pending", "regularization", "recipes", "movements", "stocks", "reports", "rules"):
            page_started = time.perf_counter()
            data = engine.page_records_v2(page, {})
            print("page", page, len(data.get("rows", [])), round(time.perf_counter() - page_started, 2), flush=True)
        print("dashboard", engine.dashboard_v2({})["total"], flush=True)


if __name__ == "__main__":
    main()
