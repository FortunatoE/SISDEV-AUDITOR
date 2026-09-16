"""Read-only timing diagnostics for the operational work queue."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from auditor.database import connect
from auditor.engine import (
    _latest_success,
    _mapping_dict,
    _property_cnpj_map,
    _recipe_date_index,
    _recipes_for_regularization,
    _return_chain_context,
    _stock_lookup,
)


def measure(label, callback):
    started = time.perf_counter()
    result = callback()
    elapsed = time.perf_counter() - started
    size = len(result) if hasattr(result, "__len__") else "-"
    print(f"STEP name={label} seconds={elapsed:.3f} size={size}", flush=True)
    return result


def main() -> None:
    if not (os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_UNPOOLED")):
        raise SystemExit("A database connection is required")
    connection = connect()
    try:
        run = _latest_success(connection)
        if not run:
            print("QUEUE no_successful_run", flush=True)
            return
        run_id = int(run["id"])
        print(f"QUEUE run_id={run_id}", flush=True)
        if connection.postgres:
            recipe_keys = connection.execute(
                """SELECT DISTINCT jsonb_object_keys(raw_json::jsonb) key
                   FROM source_records WHERE run_id=? AND source='agrotis_recipe'
                   ORDER BY 1""",
                (run_id,),
            ).fetchall()
            print(f"RECIPE keys={[row['key'] for row in recipe_keys]}", flush=True)
            date_samples = connection.execute(
                """SELECT DISTINCT entry.value sample
                   FROM source_records record
                   CROSS JOIN LATERAL jsonb_each_text(record.raw_json::jsonb) entry
                   WHERE record.run_id=? AND record.source='agrotis_recipe'
                     AND entry.key ILIKE 'Data de Emiss%%'
                   ORDER BY 1 LIMIT 10""",
                (run_id,),
            ).fetchall()
            print(f"RECIPE date_samples={[row['sample'] for row in date_samples]}", flush=True)
        pending = measure(
            "pending_rows",
            lambda: connection.execute(
                """SELECT e.direction,e.doc_date,COUNT(*) count
                   FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id
                   WHERE r.run_id=? AND r.status='NAO_LANCADO'
                   GROUP BY e.direction,e.doc_date""",
                (run_id,),
            ).fetchall(),
        )
        accepted_dates = set()
        for row in pending:
            if row["direction"] == "1" or not row["doc_date"]:
                continue
            parsed = datetime.strptime(str(row["doc_date"])[:10], "%Y-%m-%d").date()
            accepted_dates.update((parsed.isoformat(), (parsed - timedelta(days=1)).isoformat()))
        print(
            f"PENDING groups={len(pending)} accepted_recipe_dates={len(accepted_dates)}",
            flush=True,
        )
        recipes = measure(
            "recipes_filtered",
            lambda: _recipes_for_regularization(connection, run_id, accepted_dates),
        )
        measure("recipe_index", lambda: _recipe_date_index(recipes))
        measure("material_mappings", lambda: _mapping_dict(connection, "material_product"))
        measure("property_cnpj", lambda: _property_cnpj_map(connection))
        measure("stock", lambda: _stock_lookup(connection, run_id))
        measure("return_context", lambda: _return_chain_context(connection, run_id))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
