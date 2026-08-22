import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .database import ROOT, connect
from .normalization import document, identifier, key, lot, number, text, unit


DATA = ROOT / "dados"
SOURCES = [
    ("sap_entry_current", DATA / "entrada_sisdev.xlsx", 0),
    ("sap_exit_current", DATA / "saída_sisdev.xlsx", 0),
    ("sap_entry_history", DATA / "historico_sisdev" / "entrada_sisdev.xlsx", 0),
    ("sap_exit_history", DATA / "historico_sisdev" / "saída_sisdev.xlsx", 0),
    ("sap_stock", DATA / "MB52.xlsx", 0),
    ("sisdev_stock", DATA / "Relatório Saldo de Agrotóxico.pdf", 0),
    ("sisdev_movement", DATA / "RelAnaliseMovimentacaoAgrotoxico (2).xlsx", 2),
    ("agrotis_recipe", DATA / "ReceitasEmitidas.xls", 0),
]
SOURCE_BY_NAME = {source: (path, skiprows) for source, path, skiprows in SOURCES}
REQUIRED_SOURCES = set(SOURCE_BY_NAME)
RECIPE_CACHE = {}

REQUIRED_COLUMNS = {
    "sap_entry_current": {"Número de nota fiscal eletrônica", "Séries", "Texto breve material", "Quantidade"},
    "sap_exit_current": {"Número de nota fiscal eletrônica", "Séries", "Texto breve material", "Quantidade"},
    "sap_entry_history": {"Número de nota fiscal eletrônica", "Séries", "Texto breve material", "Quantidade"},
    "sap_exit_history": {"Número de nota fiscal eletrônica", "Séries", "Texto breve material", "Quantidade"},
    "sap_stock": {"Centro", "Texto breve material", "Utilização livre", "Lote"},
    "sisdev_movement": {"Nº NF", "SÉRIE NF", "TIPO MOVIMENTO", "PRODUTO", "LOTE", "QNT", "VOLUME"},
    "agrotis_recipe": {"Número do receituário", "Data de Emissão", "Produto", "Dose", "Tipo de Dosagem", "Nome RT"},
}


def field(row, *names):
    normalized = {key(k): value for k, value in row.items()}
    for name in names:
        if key(name) in normalized:
            return normalized[key(name)]
    return None


def iso_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.date().isoformat() if hasattr(value, "date") else value.isoformat()
    raw = text(value)
    if not raw:
        return None
    day_first = bool(re.match(r"^\d{1,2}/\d{1,2}/\d{4}", raw))
    parsed = pd.to_datetime(raw, errors="coerce", dayfirst=day_first)
    return None if pd.isna(parsed) else parsed.date().isoformat()


def _json_value(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _serialize_row(row):
    return {str(column): _json_value(value) for column, value in row.items()}


def _blob_time(blob):
    return text(blob.get("uploadedAt") or blob.get("uploaded_at") or blob.get("url"))


def _cloud_sources(only_source=None):
    """Download the newest object per source; callers can request exactly one source."""
    if not os.getenv("BLOB_READ_WRITE_TOKEN"):
        return {}
    token = os.environ["BLOB_READ_WRITE_TOKEN"]
    prefix = f"sisdev/{only_source}/" if only_source else "sisdev/"
    blobs = []
    cursor = None
    while True:
        query = {"prefix": prefix, "limit": "1000"}
        if cursor:
            query["cursor"] = cursor
        request = Request(
            "https://blob.vercel-storage.com?" + urlencode(query),
            headers={"Authorization": f"Bearer {token}", "x-api-version": "7"},
        )
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        blobs.extend(payload.get("blobs", []))
        cursor = payload.get("cursor") if payload.get("hasMore") else None
        if not cursor:
            break
    latest = {}
    for blob in blobs:
        parts = text(blob.get("pathname")).split("/")
        if len(parts) < 3:
            continue
        source = parts[1]
        if source not in SOURCE_BY_NAME or (only_source and source != only_source):
            continue
        if source not in latest or _blob_time(blob) > _blob_time(latest[source]):
            latest[source] = blob
    target = Path("/tmp/sisdev")
    files = {}
    for source, blob in latest.items():
        source_target = target / source
        source_target.mkdir(parents=True, exist_ok=True)
        path = source_target / Path(blob["pathname"]).name
        with urlopen(Request(blob["url"], headers={"Authorization": f"Bearer {token}"}), timeout=90) as response:
            path.write_bytes(response.read())
        files[source] = path
    return files


def _pdf_metadata(page_text):
    cnpj_match = re.search(r"REVENDA:\s*([0-9.\-/]+)", page_text or "", re.IGNORECASE)
    city_match = re.search(r"MUNIC[ÍI]PIO\s*:?\s*([A-ZÀ-Ü ]+)", page_text or "", re.IGNORECASE)
    return {
        "cnpj": identifier(cnpj_match.group(1)) if cnpj_match else "",
        "municipio": text(city_match.group(1)) if city_match else "",
    }


def _parse_sisdev_stock_pdf(path):
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("Instale pdfplumber para processar o PDF de estoque SISDEV.") from exc

    records = []
    metadata = {"cnpj": "", "municipio": ""}
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            current_metadata = _pdf_metadata(page.extract_text() or "")
            metadata = {name: value or metadata[name] for name, value in current_metadata.items()}
            grouped = defaultdict(list)
            for word in page.extract_words(keep_blank_chars=False):
                grouped[round(float(word["top"]), 1)].append(word)
            parsed_lines = []
            for top in sorted(grouped):
                words = sorted(grouped[top], key=lambda value: float(value["x0"]))
                # Only the first page has the report header.  Continuation pages
                # start with stock rows at the top of the page.
                if (page_number == 1 and top < 150) or top > page.height - 25:
                    continue
                columns = defaultdict(list)
                for word in words:
                    x0 = float(word["x0"])
                    if x0 < 190:
                        column = "product"
                    elif x0 < 450:
                        column = "package"
                    elif x0 < 530:
                        column = "lot"
                    elif x0 < 610:
                        column = "size"
                    elif x0 < 660:
                        column = "unit"
                    elif x0 < 750:
                        column = "quantity"
                    else:
                        column = "volume"
                    columns[column].append(word["text"])
                parsed_lines.append((top, columns))

            anchors = [(top, columns) for top, columns in parsed_lines
                       if all(columns[name] for name in ("lot", "unit", "quantity", "volume"))]
            fragments = {top: {"product": [], "package": []} for top, _ in anchors}
            for top, columns in parsed_lines:
                if not anchors:
                    break
                anchor_top, _ = min(anchors, key=lambda anchor: abs(anchor[0] - top))
                # Wrapped product/package text sits about 4.7 pt above/below
                # the numeric anchor; a narrow window avoids footer/header text.
                if abs(anchor_top - top) <= 8.5:
                    fragments[anchor_top]["product"].extend(columns["product"])
                    fragments[anchor_top]["package"].extend(columns["package"])

            for top, columns in anchors:
                product = " ".join(fragments[top]["product"]).strip()
                package = " ".join(fragments[top]["package"]).strip()
                if not product:
                    continue
                records.append({
                    "PRODUTO": product,
                    "EMBALAGEM": package,
                    "LOTE": " ".join(columns["lot"]),
                    "TAMANHO": number("".join(columns["size"])),
                    "U.M.": " ".join(columns["unit"]),
                    "QUANTIDADE": number("".join(columns["quantity"])),
                    "VOLUME": number("".join(columns["volume"])),
                    "CNPJ": metadata["cnpj"],
                    "MUNICIPIO": metadata["municipio"],
                    "CENTRO": "",
                    "_row_number": page_number * 10000 + len(records) + 1,
                })
    if not records:
        raise ValueError("O PDF SISDEV não contém linhas de estoque reconhecíveis.")
    return records


def _validate_columns(source, columns):
    expected = REQUIRED_COLUMNS.get(source)
    if not expected:
        return
    found = {key(column) for column in columns}
    missing = sorted(column for column in expected if key(column) not in found)
    if missing:
        raise ValueError(f"Fonte {source}: colunas obrigatórias ausentes: {', '.join(missing)}")


def _read_source(source, path):
    path = Path(path)
    if source == "sisdev_stock":
        parsed = _parse_sisdev_stock_pdf(path)
        return [(int(row.pop("_row_number")), row) for row in parsed]
    skiprows = SOURCE_BY_NAME[source][1]
    frame = pd.read_excel(path, skiprows=skiprows, dtype=object).dropna(axis=1, how="all")
    _validate_columns(source, frame.columns)
    rows = []
    for index, series in frame.iterrows():
        data = _serialize_row(series.to_dict())
        if any(value not in (None, "") for value in data.values()):
            rows.append((int(index) + skiprows + 2, data))
    return rows


def _product_name(value):
    value = text(value)
    value = re.sub(r"^\s*\d+\s*-\s*", "", value)
    value = re.sub(r"\s*-\s*Reg\.?\s*MAPA\s*:.*$", "", value, flags=re.IGNORECASE)
    return value.strip()


def _movement_direction(value):
    normalized = key(value)
    if "SAIDA" in normalized:
        return "2"
    if "ENTRADA" in normalized or "DEVOLUCAO" in normalized:
        return "1"
    return ""


def _expected_direction(source, value=None):
    """Return the SISDEV direction code, deriving it from the SAP source.

    The SAP extracts do not consistently expose a direction column.  A valid
    value in the file is allowed to override the source convention, otherwise
    entry sources are direction 1 and exit sources are direction 2.
    """
    raw = text(value)
    if raw in {"1", "2"}:
        return raw
    override = _movement_direction(raw)
    if override:
        return override
    if source.startswith("sap_entry_"):
        return "1"
    if source.startswith("sap_exit_"):
        return "2"
    return ""


def _series(value):
    raw = text(value)
    if re.fullmatch(r"\d+(?:[.,]0+)?", raw):
        return str(int(float(raw.replace(",", "."))))
    return identifier(raw)


def _summary_for_run(conn, run_id, lock=False):
    suffix = " FOR UPDATE" if lock and getattr(conn, "postgres", False) else ""
    row = conn.execute("SELECT summary_json FROM import_runs WHERE id=?" + suffix, (run_id,)).fetchone()
    if not row:
        raise ValueError(f"Execução {run_id} não encontrada.")
    try:
        summary = json.loads(row["summary_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        summary = {}
    summary.setdefault("sources", {})
    summary.setdefault("source_details", {})
    summary.setdefault("warnings", [])
    return summary


def _save_summary(conn, run_id, summary, status=None):
    if status:
        conn.execute("UPDATE import_runs SET status=?, summary_json=? WHERE id=?", (status, json.dumps(summary, ensure_ascii=False), run_id))
    else:
        conn.execute("UPDATE import_runs SET summary_json=? WHERE id=?", (json.dumps(summary, ensure_ascii=False), run_id))


def create_import_run():
    conn = connect()
    summary = {"sources": {}, "source_details": {}, "warnings": []}
    run_id = conn.execute(
        "INSERT INTO import_runs(status,summary_json) VALUES ('RUNNING',?) RETURNING id",
        (json.dumps(summary, ensure_ascii=False),),
    ).fetchone()[0]
    conn.commit()
    conn.close()
    return run_id


def latest_open_run():
    conn = connect()
    row = conn.execute("SELECT id FROM import_runs WHERE status='RUNNING' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def _clear_source(conn, run_id, source):
    source_ids = "SELECT id FROM source_records WHERE run_id=? AND source=?"
    conn.execute(
        f"DELETE FROM reconciliations WHERE expected_id IN (SELECT id FROM expected_movements WHERE source_record_id IN ({source_ids})) "
        f"OR actual_id IN (SELECT id FROM actual_movements WHERE source_record_id IN ({source_ids}))",
        (run_id, source, run_id, source),
    )
    conn.execute(f"DELETE FROM expected_movements WHERE source_record_id IN ({source_ids})", (run_id, source))
    conn.execute(f"DELETE FROM actual_movements WHERE source_record_id IN ({source_ids})", (run_id, source))
    conn.execute("DELETE FROM source_records WHERE run_id=? AND source=?", (run_id, source))
    conn.execute("DELETE FROM audit_issues WHERE run_id=? AND reference=?", (run_id, source))
    RECIPE_CACHE.pop(run_id, None)


def _expected_values(run_id, record_id, source, data, manufacturer_lots):
    sap_lot = lot(field(data, "Lote"))
    manufacturer_lot = lot(field(data, "Lote Fabricante")) or manufacturer_lots.get(sap_lot, "")
    return (
        run_id, record_id, document(field(data, "Número de nota fiscal eletrônica")), _series(field(data, "Séries")),
        _expected_direction(source, field(data, "Direção do movimento")), iso_date(field(data, "Data documento")),
        text(field(data, "Texto breve material")), key(field(data, "Texto breve material")), sap_lot,
        manufacturer_lot, number(field(data, "Quantidade")), unit(field(data, "UMB")),
        text(field(data, "Centro")), identifier(field(data, "CNPJ")),
    )


def _actual_values(run_id, record_id, data):
    movement_quantity = number(field(data, "QNT"))
    product = _product_name(field(data, "PRODUTO"))
    return (
        run_id, record_id, document(field(data, "Nº NF")), _series(field(data, "SÉRIE NF")),
        text(field(data, "TIPO MOVIMENTO")), iso_date(field(data, "DATA MOVIMENTO")), product, key(product),
        lot(field(data, "LOTE")), abs(movement_quantity) if movement_quantity is not None else None,
        number(field(data, "VOLUME")), unit(field(data, "U.M.")), identifier(field(data, "CPF/CNPJ REVENDA")),
        text(field(data, "SITUAÇÃO")),
    )


def import_source(run_id, source, path, batch_size=1000, cursor=0, max_rows=None, progress_callback=None):
    """Parse one source once and persist it in deterministic database batches.

    ``progress_callback`` receives a progress dictionary after every committed
    batch.  A workflow retry from cursor zero is idempotent because it replaces
    only this source inside the shared run.
    """
    if source not in SOURCE_BY_NAME:
        raise ValueError(f"Fonte não suportada: {source}")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    rows = _read_source(source, path)
    total_rows = len(rows)
    cursor = max(0, int(cursor or 0))
    end = total_rows if max_rows is None else min(total_rows, cursor + max(0, int(max_rows)))
    selected = rows[cursor:end]

    conn = connect()
    manufacturer_lots = _mapping_dict(conn, "manufacturer_lot", lot)
    if cursor == 0:
        _clear_source(conn, run_id, source)
        conn.commit()

    existing_rows = {row[0] for row in conn.execute("SELECT row_number FROM source_records WHERE run_id=? AND source=?", (run_id, source))}
    fingerprints = {row[0] for row in conn.execute("SELECT fingerprint FROM source_records WHERE run_id=? AND source=?", (run_id, source))} if source == "sisdev_movement" else set()
    batch_size = max(1, int(batch_size or 1000))
    imported = 0
    duplicate_rows = 0

    try:
        for start in range(0, len(selected), batch_size):
            source_chunk = selected[start:start + batch_size]
            new_records = []
            for row_number, data in source_chunk:
                if row_number in existing_rows:
                    continue
                raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
                fingerprint = hashlib.sha256((source + raw).encode("utf-8")).hexdigest()
                if source == "sisdev_movement" and fingerprint in fingerprints:
                    duplicate_rows += 1
                    continue
                new_records.append((row_number, data, fingerprint, raw))
                existing_rows.add(row_number)
                fingerprints.add(fingerprint)

            if new_records:
                conn.executemany(
                    "INSERT INTO source_records(run_id,source,source_file,row_number,fingerprint,raw_json) VALUES (?,?,?,?,?,?)",
                    [(run_id, source, path.name, row_number, fingerprint, raw)
                     for row_number, _, fingerprint, raw in new_records],
                )
                placeholders = ",".join("?" for _ in new_records)
                identifiers = conn.execute(
                    f"SELECT id,row_number FROM source_records WHERE run_id=? AND source=? AND row_number IN ({placeholders})",
                    [run_id, source, *[record[0] for record in new_records]],
                ).fetchall()
                record_ids = {record["row_number"]: record["id"] for record in identifiers}
                if source.startswith("sap_") and source != "sap_stock":
                    conn.executemany(
                        """INSERT INTO expected_movements(run_id,source_record_id,nf,series,direction,doc_date,
                        sap_material,material_key,lot,manufacturer_lot,quantity,unit,center,cnpj)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        [_expected_values(run_id, record_ids[row_number], source, data, manufacturer_lots)
                         for row_number, data, _, _ in new_records],
                    )
                elif source == "sisdev_movement":
                    conn.executemany(
                        """INSERT INTO actual_movements(run_id,source_record_id,nf,series,movement_type,movement_date,
                        product,product_key,lot,quantity,volume,unit,cnpj,status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        [_actual_values(run_id, record_ids[row_number], data)
                         for row_number, data, _, _ in new_records],
                    )
                imported += len(new_records)
            conn.commit()
            processed = min(end, cursor + start + len(source_chunk))
            if progress_callback:
                progress_callback({
                    "processed_rows": processed, "total_rows": total_rows,
                    "imported_rows": imported, "duplicate_rows": duplicate_rows, "warnings": [],
                })
        # Serialize the read/merge/write so parallel source workflows cannot
        # overwrite one another's summary entries.
        summary = _summary_for_run(conn, run_id, lock=True)
        imported_total = conn.execute("SELECT COUNT(*) FROM source_records WHERE run_id=? AND source=?", (run_id, source)).fetchone()[0]
        if cursor == 0:
            summary["source_details"][source] = {}
        details = summary["source_details"].setdefault(source, {})
        details.update({
            "file": path.name, "total_rows": total_rows, "processed_rows": end,
            "duplicate_rows": (0 if cursor == 0 else int(details.get("duplicate_rows", 0))) + duplicate_rows,
            "done": end >= total_rows,
        })
        summary["sources"][source] = imported_total
        if details["duplicate_rows"]:
            conn.execute(
                "DELETE FROM audit_issues WHERE run_id=? AND category='DUPLICIDADE_EXPORTACAO' AND reference=?",
                (run_id, source),
            )
            conn.execute(
                "INSERT INTO audit_issues(run_id,severity,category,reference,message,details_json) VALUES (?,?,?,?,?,?)",
                (run_id, "ALTA", "DUPLICIDADE_EXPORTACAO", source,
                 f"{details['duplicate_rows']} cópias idênticas do relatório SISDEV foram removidas.",
                 json.dumps({"duplicate_rows": details["duplicate_rows"]})),
            )
        _save_summary(conn, run_id, summary, "RUNNING")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "run_id": run_id, "source": source, "file": path.name, "processed_rows": end, "total_rows": total_rows,
        "next_cursor": end, "done": end >= total_rows, "imported_rows": imported,
        "duplicate_rows": duplicate_rows, "warnings": [],
    }


def import_source_batch(run_id, source, source_path, cursor=0, batch_size=1000):
    return import_source(run_id, source, source_path, batch_size=batch_size, cursor=cursor, max_rows=batch_size)


def import_and_reconcile(only_source=None, source_path=None, run_id=None, reconcile=None):
    """Compatibility wrapper; source jobs accumulate in one RUNNING run."""
    if run_id is None:
        run_id = latest_open_run() if only_source else None
        run_id = run_id or create_import_run()
    reconcile = (only_source is None) if reconcile is None else bool(reconcile)
    if only_source:
        cloud = {} if source_path else _cloud_sources(only_source)
        path = Path(source_path) if source_path else cloud.get(only_source, SOURCE_BY_NAME[only_source][0])
        result = import_source(run_id, only_source, path)
        if reconcile:
            result["reconciliation"] = reconcile_run(run_id)
        return {"run_id": run_id, "sources": {only_source: result["imported_rows"]}, **result}

    cloud = _cloud_sources()
    results = {}
    for source, default_path, _ in SOURCES:
        path = cloud.get(source, default_path)
        if not path.exists():
            raise FileNotFoundError(f"Arquivo obrigatório ausente para {source}: {path.name}")
        results[source] = import_source(run_id, source, path)
    final = reconcile_run(run_id) if reconcile else {}
    return {"run_id": run_id, "sources": {name: value["imported_rows"] for name, value in results.items()}, **final}


def _product_matches(left, right, mappings=None):
    left, right = key(_product_name(left)), key(_product_name(right))
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    mapped = (mappings or {}).get(left)
    if mapped and (mapped == right or mapped in right or right in mapped):
        return True
    ignored = {"REG", "MAPA", "SC", "EC", "WG", "FS", "SL", "BR"}
    left_terms = {term for term in left.split() if len(term) > 2 and term not in ignored}
    right_terms = {term for term in right.split() if len(term) > 2 and term not in ignored}
    return bool(left_terms and right_terms and left_terms == right_terms)


def _actual_metadata(conn, actual_rows):
    metadata = {}
    if not actual_rows:
        return metadata
    run_id = actual_rows[0]["run_id"]
    center_by_cnpj = _center_by_cnpj(conn, run_id)
    for row in conn.execute(
        "SELECT a.id,s.raw_json FROM actual_movements a JOIN source_records s ON s.id=a.source_record_id WHERE a.run_id=?",
        (run_id,),
    ):
        raw = json.loads(row["raw_json"])
        raw_cnpj = identifier(field(raw, "CPF/CNPJ REVENDA", "CNPJ"))
        metadata[row["id"]] = {
            "doc_date": iso_date(field(raw, "DATA NF")) or iso_date(field(raw, "DATA MOVIMENTO")),
            "center": text(field(raw, "CENTRO", "UNIDADE", "URE")) or center_by_cnpj.get(raw_cnpj, center_by_cnpj.get("", "")),
            "direction": _movement_direction(field(raw, "TIPO MOVIMENTO")),
        }
    return metadata


def _reconcile(conn, run_id):
    expected = conn.execute("SELECT * FROM expected_movements WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    actual = conn.execute("SELECT * FROM actual_movements WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    metadata = _actual_metadata(conn, actual)
    product_mappings = _mapping_dict(conn, "material_product")
    actual_by_document = defaultdict(list)
    for actual_row in actual:
        actual_by_document[(actual_row["nf"], actual_row["series"])].append(actual_row)
    used = set()
    reconciliation_rows = []

    for expected_row in expected:
        doc_candidates = [
            row for row in actual_by_document.get((expected_row["nf"], expected_row["series"]), [])
            if row["id"] not in used
        ]
        chosen = None
        status = "NAO_LANCADO"
        diagnosis = "NF e série não encontrados no SISDEV."
        confidence = "ALTA"

        if not expected_row["nf"]:
            status, diagnosis, confidence = "PENDENTE_DADOS", "Linha SAP sem número de NF para conciliação.", "BAIXA"
        elif doc_candidates:
            product_candidates = [row for row in doc_candidates if _product_matches(expected_row["sap_material"], row["product"], product_mappings)]
            candidates = product_candidates or doc_candidates
            direction_candidates = [
                row for row in candidates
                if not metadata.get(row["id"], {}).get("direction")
                or metadata[row["id"]]["direction"] == expected_row["direction"]
            ]
            if direction_candidates:
                candidates = direction_candidates
            dated = [
                row for row in candidates
                if not metadata.get(row["id"], {}).get("doc_date")
                or not expected_row["doc_date"]
                or metadata[row["id"]]["doc_date"] == expected_row["doc_date"]
            ]
            if dated:
                candidates = dated
            centered = [
                row for row in candidates
                if not metadata.get(row["id"], {}).get("center")
                or not expected_row["center"]
                or key(metadata[row["id"]]["center"]) == key(expected_row["center"])
            ]
            if centered:
                candidates = centered

            preferred_lot = expected_row["manufacturer_lot"] or expected_row["lot"]
            candidates.sort(key=lambda row: (
                row["lot"] != preferred_lot,
                row["lot"] != expected_row["lot"],
                abs((expected_row["quantity"] or 0) - abs((row["quantity"] or 0) * (row["volume"] or 0))),
                row["id"],
            ))
            chosen = candidates[0]
            used.add(chosen["id"])
            actual_meta = metadata.get(chosen["id"], {})

            if not product_candidates:
                status, diagnosis, confidence = "DIVERGENCIA_MATERIAL", "NF encontrada, mas o produto SAP não corresponde ao produto SISDEV.", "MEDIA"
            elif actual_meta.get("direction") and actual_meta["direction"] != expected_row["direction"]:
                status, diagnosis, confidence = "DIVERGENCIA_DIRECAO", "Documento e produto encontrados em direção de movimento diferente.", "MEDIA"
            elif actual_meta.get("doc_date") and expected_row["doc_date"] and actual_meta["doc_date"] != expected_row["doc_date"]:
                status, diagnosis, confidence = "DIVERGENCIA_DATA", "Documento e produto encontrados em data fiscal diferente.", "MEDIA"
            elif actual_meta.get("center") and expected_row["center"] and key(actual_meta["center"]) != key(expected_row["center"]):
                status, diagnosis, confidence = "DIVERGENCIA_CENTRO", "Documento encontrado em centro/URE diferente.", "MEDIA"
            elif expected_row["manufacturer_lot"] and chosen["lot"] != expected_row["manufacturer_lot"]:
                if chosen["lot"] == expected_row["lot"]:
                    status, diagnosis, confidence = "DIVERGENCIA_LOTE_FABRICANTE", "Lote SAP corresponde, mas o lote fabricante prioritário diverge.", "MEDIA"
                else:
                    status, diagnosis, confidence = "DIVERGENCIA_LOTE", "Documento encontrado, porém lote fabricante não corresponde.", "MEDIA"
            elif not expected_row["manufacturer_lot"] and expected_row["lot"] and chosen["lot"] != expected_row["lot"]:
                status, diagnosis, confidence = "DIVERGENCIA_LOTE", "Documento encontrado, porém lote SAP não corresponde.", "MEDIA"
            elif expected_row["quantity"] is None or chosen["quantity"] is None or chosen["volume"] is None:
                status, diagnosis, confidence = "PENDENTE_QUANTIDADE", "Quantidade ou volume ausente para validar o documento.", "BAIXA"
            else:
                actual_total = abs(chosen["quantity"] * chosen["volume"])
                tolerance = max(0.001, abs(expected_row["quantity"]) * 0.000001)
                if abs(expected_row["quantity"] - actual_total) > tolerance:
                    status, diagnosis, confidence = "DIVERGENCIA_QUANTIDADE", "Lote corresponde, mas a quantidade SAP difere do total SISDEV (embalagens × volume).", "MEDIA"
                elif expected_row["unit"] and chosen["unit"] and expected_row["unit"] != chosen["unit"]:
                    status, diagnosis, confidence = "DIVERGENCIA_UNIDADE", "Quantidade confere, mas a unidade SAP difere da unidade SISDEV.", "MEDIA"
                else:
                    status, diagnosis, confidence = "CORRETO", "Documento, produto, direção, data, lote fabricante e quantidade compatíveis.", "ALTA"

        details = {
            "nf": expected_row["nf"], "series": expected_row["series"], "sap_material": expected_row["sap_material"],
            "sap_lot": expected_row["lot"], "manufacturer_lot": expected_row["manufacturer_lot"],
        }
        reconciliation_rows.append(
            (run_id, expected_row["id"], chosen["id"] if chosen else None, status, diagnosis, json.dumps(details), confidence)
        )

    unmatched = 0
    for actual_row in actual:
        if actual_row["id"] not in used:
            unmatched += 1
            reconciliation_rows.append(
                (run_id, None, actual_row["id"], "SEM_ORIGEM_SAP", "Movimentação SISDEV sem origem SAP encontrada.",
                 json.dumps({"nf": actual_row["nf"]}), "BAIXA")
            )
    if reconciliation_rows:
        conn.executemany(
            """INSERT INTO reconciliations(run_id,expected_id,actual_id,status,diagnosis,details_json,confidence)
            VALUES (?,?,?,?,?,?,?)""",
            reconciliation_rows,
        )
    if unmatched:
        conn.execute(
            "INSERT INTO audit_issues(run_id,severity,category,reference,message,details_json) VALUES (?,?,?,?,?,?)",
            (run_id, "MEDIA", "SEM_ORIGEM_SAP", "sisdev_movement", f"{unmatched} movimentos SISDEV não encontraram origem SAP.", json.dumps({"rows": unmatched})),
        )


def _source_completion(conn, run_id, summary):
    persisted = {
        row["source"]: row["rows"]
        for row in conn.execute(
            "SELECT source,COUNT(*) rows FROM source_records WHERE run_id=? GROUP BY source", (run_id,)
        )
    }
    latest_jobs = {}
    for row in conn.execute(
        "SELECT id,source,status FROM import_jobs WHERE run_id=? ORDER BY id", (run_id,)
    ):
        latest_jobs[row["source"]] = row["status"]
    details = summary.get("source_details", {})
    known = set(persisted) | set(latest_jobs) | set(details)
    successful_job_statuses = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "SUCCESS", "SUCCEEDED"}
    completed = set()
    for source in known:
        if source in latest_jobs:
            if latest_jobs[source] in successful_job_statuses:
                completed.add(source)
        elif details.get(source, {}).get("done") or source in persisted:
            completed.add(source)
    return known, completed


def reconcile_run(run_id, require_complete=True):
    conn = connect()
    summary = _summary_for_run(conn, run_id)
    known, completed = _source_completion(conn, run_id, summary)
    missing = sorted(REQUIRED_SOURCES - known)
    unfinished = sorted((REQUIRED_SOURCES & known) - completed)
    if require_complete and (missing or unfinished):
        conn.close()
        parts = []
        if missing:
            parts.append("fontes ausentes: " + ", ".join(missing))
        if unfinished:
            parts.append("fontes incompletas: " + ", ".join(unfinished))
        raise ValueError("Não é possível conciliar; " + "; ".join(parts) + ".")
    try:
        conn.execute("DELETE FROM reconciliations WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM audit_issues WHERE run_id=? AND category<>'DUPLICIDADE_EXPORTACAO'", (run_id,))
        _reconcile(conn, run_id)
        summary["reconciled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        summary["expected_rows"] = conn.execute("SELECT COUNT(*) FROM expected_movements WHERE run_id=?", (run_id,)).fetchone()[0]
        summary["actual_rows"] = conn.execute("SELECT COUNT(*) FROM actual_movements WHERE run_id=?", (run_id,)).fetchone()[0]
        conn.execute(
            "UPDATE import_runs SET status='SUCCESS',finished_at=CURRENT_TIMESTAMP,summary_json=? WHERE id=?",
            (json.dumps(summary, ensure_ascii=False), run_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"run_id": run_id, **summary}


def _latest_success(conn):
    return conn.execute(
        "SELECT id,started_at,finished_at,summary_json FROM import_runs WHERE status='SUCCESS' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _filter_sql(filters, alias="e"):
    filters = filters or {}
    clauses = []
    params = []
    for request_key, column in (
        ("from", f"{alias}.doc_date >= ?"), ("to", f"{alias}.doc_date <= ?"),
        ("center", f"{alias}.center = ?"), ("direction", f"{alias}.direction = ?"),
    ):
        if filters.get(request_key):
            clauses.append(column)
            params.append(filters[request_key])
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def dashboard_v2(filters=None):
    filters = filters or {}
    conn = connect()
    run = _latest_success(conn)
    if not run:
        conn.close()
        return {"ready": False}
    where, params = _filter_sql(filters)
    doc_query = """WITH linhas AS (
      SELECT e.nf,e.series,MAX(CASE WHEN r.status='NAO_LANCADO' THEN 4
        WHEN r.status IN ('PENDENTE_DADOS','PENDENTE_QUANTIDADE') THEN 2
        WHEN r.status<>'CORRETO' THEN 3 ELSE 1 END) prioridade
      FROM expected_movements e JOIN reconciliations r ON r.expected_id=e.id WHERE e.run_id=?""" + where + """
      GROUP BY e.nf,e.series), docs AS (SELECT CASE prioridade WHEN 4 THEN 'NAO_LANCADO'
        WHEN 3 THEN 'DIVERGENTE' WHEN 2 THEN 'PENDENTE' ELSE 'CORRETO' END status FROM linhas)
      SELECT status,COUNT(*) n FROM docs GROUP BY status"""
    statuses = {row["status"]: row["n"] for row in conn.execute(doc_query, [run["id"], *params])}
    for status_name in ("CORRETO", "PENDENTE", "DIVERGENTE", "NAO_LANCADO"):
        statuses.setdefault(status_name, 0)
    rows_query = """SELECT r.status,r.diagnosis,e.nf,e.series,e.doc_date,e.center,e.direction,e.sap_material,
      e.lot sap_lot,e.manufacturer_lot,e.quantity,a.product sisdev_product,a.lot sisdev_lot,
      ABS(a.quantity*a.volume) actual_quantity FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id
      LEFT JOIN actual_movements a ON a.id=r.actual_id WHERE e.run_id=?""" + where + """
      AND r.status!='CORRETO' ORDER BY e.doc_date DESC,r.id DESC LIMIT 100"""
    rows = [dict(row) for row in conn.execute(rows_query, [run["id"], *params])]
    options = {
        "centers": [row[0] for row in conn.execute("SELECT DISTINCT center FROM expected_movements WHERE run_id=? AND center<>'' ORDER BY 1", (run["id"],))],
        "directions": [{"value": row[0], "label": "Entrada" if row[0] == "1" else "Saída" if row[0] == "2" else row[0]}
                       for row in conn.execute("SELECT DISTINCT direction FROM expected_movements WHERE run_id=? ORDER BY 1", (run["id"],))],
        "range": dict(conn.execute("SELECT MIN(doc_date) min_date,MAX(doc_date) max_date FROM expected_movements WHERE run_id=?", (run["id"],)).fetchone()),
    }
    issues = [dict(row) for row in conn.execute("SELECT severity,category,message FROM audit_issues WHERE run_id=? ORDER BY id DESC LIMIT 8", (run["id"],))]
    total = sum(statuses.values())
    correct = statuses["CORRETO"]
    launched = total - statuses["NAO_LANCADO"]
    conn.close()
    return {
        "ready": True, "run": dict(run), "total": total, "correct": correct,
        "efficacy": round(correct / total * 100, 1) if total else 0, "launched": launched,
        "adherence": round(launched / total * 100, 1) if total else 0, "statuses": statuses,
        "issues": issues, "pending": rows, "options": options,
    }


def dashboard():
    return dashboard_v2()


def page_records(page, filters=None):
    return page_records_v2(page, filters)


def _recipe_number(value):
    return number(value)


def _recipes_for_regularization(conn, run_id):
    if run_id in RECIPE_CACHE:
        return RECIPE_CACHE[run_id]
    recipes = []
    for record in conn.execute("SELECT raw_json FROM source_records WHERE run_id=? AND source='agrotis_recipe' ORDER BY row_number", (run_id,)):
        raw = json.loads(record["raw_json"])
        normalized = {key(column): value for column, value in raw.items()}

        def recipe_field(*names):
            for name in names:
                if key(name) in normalized:
                    return normalized[key(name)]
            return None

        recipes.append({
            "data_emissao": iso_date(recipe_field("Data de Emissão")),
            "produto": text(recipe_field("Produto")),
            "numero_receita": text(recipe_field("Número do receituário")),
            "nota_fiscal": document(recipe_field("Nota Fiscal")),
            "art": text(recipe_field("ART")),
            "nome_rt": text(recipe_field("Nome RT")),
            "cultura": text(recipe_field("Cultura")),
            "diagnostico": text(recipe_field("Diagnóstico")),
            "dose_recomendada": _recipe_number(recipe_field("Dose")),
            "tipo_dosagem": text(recipe_field("Tipo de Dosagem")),
            "area_receita": _recipe_number(recipe_field("Área")),
            "quantidade_receita": _recipe_number(recipe_field("Quantidade")),
            "unidade_receita": unit(recipe_field("Unidade Quantidade")),
            "nome_propriedade": text(recipe_field("Nome da Propriedade")),
            "nome_produtor": text(recipe_field("Nome Produtor")),
        })
    RECIPE_CACHE[run_id] = recipes
    return recipes


def preferred_rt(conn):
    row = conn.execute("SELECT value FROM app_settings WHERE key='preferred_rt'").fetchone()
    return row["value"] if row else "KARLA DANIELLY GARCIA DE LIMA"


def set_preferred_rt(value):
    conn = connect()
    conn.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES ('preferred_rt',?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", (text(value),),
    )
    conn.commit()
    conn.close()


def rt_preference_options():
    conn = connect()
    run = _latest_success(conn)
    options = sorted({recipe["nome_rt"] for recipe in _recipes_for_regularization(conn, run["id"]) if recipe["nome_rt"]}) if run else []
    value = preferred_rt(conn)
    conn.close()
    return {"preferred_rt": value, "options": options}


def _normalized_dose(value, dosage_type):
    dose = value if isinstance(value, (int, float)) else _recipe_number(value)
    normalized_type = "".join(char for char in key(dosage_type).lower() if char.isalpha())
    if dose is None:
        return None, text(dosage_type)
    if normalized_type == "gha":
        return dose / 1000, "kg/ha"
    if normalized_type == "mlha":
        return dose / 1000, "L/ha"
    return dose, text(dosage_type)


def _format_dose(dose, dosage_type):
    if dose is None:
        return None
    shown = f"{dose:.6f}".rstrip("0").rstrip(".")
    return f"{shown} {dosage_type}" if dosage_type else shown


def _recipe_date_index(recipes):
    by_date = defaultdict(list)
    by_date_product = defaultdict(list)
    for recipe in recipes:
        by_date[recipe["data_emissao"]].append(recipe)
        by_date_product[(recipe["data_emissao"], key(_product_name(recipe["produto"])))].append(recipe)
    return {"by_date": by_date, "by_date_product": by_date_product}


def _select_recipe(recipes, expected_row, preferred_name=None, product_mappings=None, property_cnpj=None):
    document_date = expected_row.get("doc_date")
    if not document_date:
        return None, []
    try:
        parsed_date = datetime.strptime(document_date, "%Y-%m-%d").date()
    except ValueError:
        return None, []
    accepted_dates = (parsed_date.isoformat(), (parsed_date - timedelta(days=1)).isoformat())
    pool = recipes
    if isinstance(recipes, dict) and "by_date" in recipes:
        expected_product = key(_product_name(expected_row.get("sap_material")))
        expected_product = (product_mappings or {}).get(expected_product, expected_product)
        exact_pool = [recipe for accepted_date in accepted_dates
                      for recipe in recipes["by_date_product"].get((accepted_date, expected_product), [])]
        pool = exact_pool or [recipe for accepted_date in accepted_dates
                              for recipe in recipes["by_date"].get(accepted_date, [])]
    candidates = [recipe for recipe in pool
                  if recipe["data_emissao"] in accepted_dates
                  and _product_matches(expected_row.get("sap_material"), recipe["produto"], product_mappings)]
    exact_document = [recipe for recipe in candidates if recipe["nota_fiscal"] and recipe["nota_fiscal"] == expected_row.get("nf")]
    if exact_document:
        candidates = exact_document
    expected_cnpj = identifier(expected_row.get("cnpj"))
    same_property = [recipe for recipe in candidates
                     if expected_cnpj and (property_cnpj or {}).get(key(recipe["nome_propriedade"])) == expected_cnpj]
    if same_property:
        candidates = same_property
    exact_quantity = [
        recipe for recipe in candidates
        if recipe["quantidade_receita"] is not None and expected_row.get("quantity") is not None
        and abs(recipe["quantidade_receita"] - expected_row["quantity"]) <= max(0.001, abs(expected_row["quantity"]) * 0.000001)
    ]
    if exact_quantity:
        candidates = exact_quantity
    preferred = [recipe for recipe in candidates if preferred_name and key(recipe["nome_rt"]) == key(preferred_name)]
    if len(preferred) == 1:
        return preferred[0], candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def _regularization_rows(conn, run_id, preferred_name=None, filters=None):
    recipes = _recipes_for_regularization(conn, run_id)
    recipe_index = _recipe_date_index(recipes)
    product_mappings = _mapping_dict(conn, "material_product")
    property_cnpj = _property_cnpj_map(conn)
    where, params = _filter_sql(filters)
    expected = conn.execute(
        """SELECT e.doc_date,e.cnpj,e.nf,e.series,e.direction,e.sap_material,e.lot,e.manufacturer_lot,
        e.quantity,e.unit,e.center,s.raw_json FROM reconciliations r
        JOIN expected_movements e ON e.id=r.expected_id JOIN source_records s ON s.id=e.source_record_id
        WHERE r.run_id=? AND r.status='NAO_LANCADO'""" + where +
        " ORDER BY e.doc_date DESC,e.nf,e.id", [run_id, *params],
    )
    rows = []
    for item in expected:
        row = dict(item)
        raw = json.loads(row.pop("raw_json"))
        direction = "Entrada" if row["direction"] == "1" else "Saída" if row["direction"] == "2" else row["direction"]
        base = {
            "direcao": direction, "data_documento": row["doc_date"], "cnpj": row["cnpj"],
            "numero_nf": row["nf"], "numero_nfe": row["nf"], "serie": row["series"], "produto": row["sap_material"],
            "lote": row["manufacturer_lot"] or row["lot"], "lote_sap": row["lot"],
            "quantidade_sap": row["quantity"], "quantidade": row["quantity"],
            "unidade_sap": row["unit"], "ure": row["center"],
            "volume_embalagem": number(field(raw, "Volume da Embalagens", "Volume da Embalagem")),
            "quantidade_embalagens": number(field(raw, "Quantidade de Embalagens", "Quantidade de Embalagem")),
        }
        base["quantidade_embalagem"] = base["quantidade_embalagens"]
        if direction == "Entrada":
            base.update({"situacao": "PRONTO_PARA_REGULARIZAR", "pendencia": "Conferir embalagem e efetuar o lançamento de entrada."})
            rows.append(base)
            continue
        recipe, candidates = _select_recipe(recipe_index, row, preferred_name, product_mappings, property_cnpj)
        if recipe:
            emission_window = "D" if recipe["data_emissao"] == row["doc_date"] else "D-1"
            dose, dose_type = _normalized_dose(recipe["dose_recomendada"], recipe["tipo_dosagem"])
            calculated_area = round((row["quantity"] or 0) / dose, 6) if dose else None
            base.update({
                "numero_receita": recipe["numero_receita"], "numero_receituario": recipe["numero_receita"],
                "art": recipe["art"], "nome_rt": recipe["nome_rt"],
                "cultura": recipe["cultura"], "diagnostico": recipe["diagnostico"],
                "nome_propriedade": recipe["nome_propriedade"], "dose_recomendada": _format_dose(dose, dose_type),
                "tipo_dosagem": dose_type, "area_receita": recipe["area_receita"],
                "area_calculada": calculated_area, "area": calculated_area,
                "janela_receita": emission_window, "situacao": "RECEITA_SUGERIDA",
                "pendencia": "Conferir dados e efetuar o lançamento de saída.",
            })
        elif candidates:
            base.update({"situacao": "RECEITAS_MULTIPLAS", "janela_receita": "D/D-1", "pendencia": f"Selecionar uma entre {len(candidates)} receitas candidatas."})
        else:
            base.update({"situacao": "SEM_RECEITA", "janela_receita": "D/D-1", "pendencia": "Localizar receita compatível emitida em D ou D-1."})
        rows.append(base)
    return rows


def regularization_export_rows(filters=None):
    filters = filters or {}
    conn = connect()
    run = _latest_success(conn)
    if not run:
        conn.close()
        return []
    preference = filters.get("preferred_rt") or preferred_rt(conn)
    rows = _regularization_rows(conn, run["id"], preference, filters)
    conn.close()
    columns = {
        "Data documento": "data_documento", "CNPJ": "cnpj", "Número de nota fiscal eletrônica": "numero_nf",
        "Séries": "serie", "Produto": "produto", "Lote": "lote", "Quantidade": "quantidade_sap",
        "Volume da embalagem": "volume_embalagem", "Quantidade de embalagem": "quantidade_embalagens",
        "Número de receituário": "numero_receita", "ART": "art", "Nome RT": "nome_rt", "Cultura": "cultura",
        "Diagnóstico": "diagnostico", "Unidade recebimento de embalagem (URE)": "ure",
        "Dose recomendada": "dose_recomendada", "Área (quantidade do lote/dose)": "area_calculada",
    }
    return [{name: row.get(source_name) for name, source_name in columns.items()} for row in rows if row["direcao"] == "Saída"]


def _setting_map(conn, setting_key):
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (setting_key,)).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row["value"])
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _mapping_dict(conn, mapping_type, normalizer=key):
    return {
        normalizer(row["source_value"]): normalizer(row["target_value"])
        for row in conn.execute(
            "SELECT source_value,target_value FROM reconciliation_mappings WHERE mapping_type=? AND status='ACTIVE'",
            (mapping_type,),
        )
        if normalizer(row["source_value"])
    }


def _property_cnpj_map(conn):
    configured = {key(name): identifier(cnpj) for name, cnpj in _setting_map(conn, "property_cnpj_map").items()}
    # property names are textual keys while the targets are CNPJ identifiers.
    configured.update({
        key(row["source_value"]): identifier(row["target_value"])
        for row in conn.execute(
            "SELECT source_value,target_value FROM reconciliation_mappings WHERE mapping_type='property_cnpj' AND status='ACTIVE'"
        )
    })
    return configured


def _center_by_cnpj(conn, run_id):
    configured = {identifier(cnpj): text(center) for cnpj, center in _setting_map(conn, "stock_center_map").items()}
    configured.update({
        identifier(row["source_value"]): text(row["target_value"])
        for row in conn.execute(
            "SELECT source_value,target_value FROM reconciliation_mappings WHERE mapping_type='cnpj_center_ure' AND status='ACTIVE'"
        )
    })
    grouped = defaultdict(set)
    for row in conn.execute("SELECT cnpj,center FROM expected_movements WHERE run_id=? AND cnpj<>'' AND center<>''", (run_id,)):
        grouped[identifier(row["cnpj"])].add(text(row["center"]))
    for cnpj, centers in grouped.items():
        if len(centers) == 1:
            configured.setdefault(cnpj, next(iter(centers)))
    all_centers = {center for centers in grouped.values() for center in centers if center}
    if len(all_centers) == 1:
        configured.setdefault("", next(iter(all_centers)))
    return configured


def _stock_rows(conn, run_id):
    center_map = _center_by_cnpj(conn, run_id)
    product_mappings = _mapping_dict(conn, "material_product")
    property_cnpj = _property_cnpj_map(conn)
    manufacturer_lots = _mapping_dict(conn, "manufacturer_lot", lot)
    sap = {}
    for record in conn.execute("SELECT raw_json FROM source_records WHERE run_id=? AND source='sap_stock' ORDER BY row_number", (run_id,)):
        raw = json.loads(record["raw_json"])
        center, product = text(field(raw, "Centro")), text(field(raw, "Texto breve material"))
        sap_lot, manufacturer_lot = lot(field(raw, "Lote")), lot(field(raw, "Lote Fabricante"))
        preferred_lot = manufacturer_lot or manufacturer_lots.get(sap_lot, "") or sap_lot
        measure = unit(field(raw, "UMB"))
        stock_key = (key(product), preferred_lot, measure, center)
        current = sap.setdefault(stock_key, {
            "centro": center, "depositos": set(), "material": product, "lote": sap_lot,
            "lote_fabricante": manufacturer_lot, "quantidade_sap": 0.0, "unidade": measure,
        })
        current["depositos"].add(text(field(raw, "Depósito")))
        current["quantidade_sap"] += number(field(raw, "Utilização livre")) or 0

    sisdev = {}
    for record in conn.execute("SELECT raw_json FROM source_records WHERE run_id=? AND source='sisdev_stock' ORDER BY row_number", (run_id,)):
        raw = json.loads(record["raw_json"])
        cnpj = identifier(field(raw, "CNPJ"))
        center = text(field(raw, "CENTRO")) or center_map.get(cnpj, center_map.get("", cnpj))
        product, product_lot, measure = text(field(raw, "PRODUTO")), lot(field(raw, "LOTE")), unit(field(raw, "U.M."))
        stock_key = (key(product), product_lot, measure, center)
        current = sisdev.setdefault(stock_key, {
            "centro": center, "cnpj_sisdev": cnpj, "material": product, "lote": product_lot,
            "quantidade_sisdev": 0.0, "unidade": measure,
        })
        current["quantidade_sisdev"] += number(field(raw, "VOLUME")) or 0

    rows, used_sisdev = [], set()
    for stock_key, sap_row in sap.items():
        _, preferred_lot, measure, center = stock_key
        candidates = [
            (candidate_key, candidate) for candidate_key, candidate in sisdev.items()
            if candidate_key not in used_sisdev and candidate_key[1:] == (preferred_lot, measure, center)
            and _product_matches(sap_row["material"], candidate["material"], product_mappings)
        ]
        candidate_key, sisdev_row = candidates[0] if candidates else (None, None)
        if candidate_key:
            used_sisdev.add(candidate_key)
        sap_quantity = round(sap_row["quantidade_sap"], 6)
        sisdev_quantity = round(sisdev_row["quantidade_sisdev"], 6) if sisdev_row else 0.0
        difference = round(sisdev_quantity - sap_quantity, 6)
        rows.append({
            "centro": center, "deposito": ", ".join(sorted(value for value in sap_row["depositos"] if value)),
            "material": sap_row["material"], "lote": sap_row["lote"], "lote_fabricante": sap_row["lote_fabricante"],
            "quantidade_sap": sap_quantity, "quantidade_sisdev": sisdev_quantity, "diferenca": difference,
            "diferenca_sisdev_menos_sap": difference,
            "unidade": measure, "necessidade": "Saída" if difference > 0 else "Entrada" if difference < 0 else "Equilibrado",
            "status_mapeamento": "Conciliado" if sisdev_row else "Sem saldo SISDEV correspondente",
        })
    for candidate_key, sisdev_row in sisdev.items():
        if candidate_key in used_sisdev:
            continue
        quantity = round(sisdev_row["quantidade_sisdev"], 6)
        rows.append({
            "centro": sisdev_row["centro"], "deposito": "", "material": sisdev_row["material"],
            "lote": sisdev_row["lote"], "lote_fabricante": sisdev_row["lote"], "quantidade_sap": 0.0,
            "quantidade_sisdev": quantity, "diferenca": quantity,
            "diferenca_sisdev_menos_sap": quantity, "unidade": sisdev_row["unidade"],
            "necessidade": "Saída" if quantity > 0 else "Equilibrado", "status_mapeamento": "Sem saldo SAP correspondente",
        })
    return sorted(rows, key=lambda row: (row["centro"], key(row["material"]), row["lote_fabricante"]))


def validation_rows(limit=None, filters=None):
    conn = connect()
    run = _latest_success(conn)
    if not run:
        conn.close()
        return []
    where, params = _filter_sql(filters)
    query = """SELECT e.*,r.status reconciliation_status,a.product sisdev_product,a.movement_date,
      a.quantity actual_packages,a.volume actual_volume,s.raw_json
      FROM expected_movements e JOIN reconciliations r ON r.expected_id=e.id
      JOIN source_records s ON s.id=e.source_record_id LEFT JOIN actual_movements a ON a.id=r.actual_id
      WHERE e.run_id=?""" + where + " ORDER BY e.doc_date,e.nf,e.id"
    expected_rows = conn.execute(query, [run["id"], *params]).fetchall()
    recipes = _recipes_for_regularization(conn, run["id"])
    recipe_index = _recipe_date_index(recipes)
    product_mappings = _mapping_dict(conn, "material_product")
    property_cnpj = _property_cnpj_map(conn)
    preference = preferred_rt(conn)
    rows = []
    for item in expected_rows:
        data = dict(item)
        raw = json.loads(data.pop("raw_json"))
        recipe, _ = _select_recipe(recipe_index, data, preference, product_mappings, property_cnpj) if data["direction"] == "2" else (None, [])
        dose, dose_type = _normalized_dose(recipe["dose_recomendada"], recipe["tipo_dosagem"]) if recipe else (None, None)
        rows.append({
            "Data": data["movement_date"],
            "Tipo movimento": "Entrada" if data["direction"] == "1" else "Saída" if data["direction"] == "2" else data["direction"],
            "Centro": data["center"], "Data documento": data["doc_date"], "CNPJ": data["cnpj"],
            "Carta de Correção": field(raw, "Carta de Correção"), "Nº documento": field(raw, "Nº documento"),
            "Número de nota fiscal eletrônica": data["nf"], "Séries": data["series"],
            "Texto breve material": data["sap_material"], "Lote": data["lot"], "Lote Fabricante": data["manufacturer_lot"],
            "Quantidade": data["quantity"], "Volume da Embalagens": number(field(raw, "Volume da Embalagens")),
            "Quantidade de Embalagens": number(field(raw, "Quantidade de Embalagens")), "PROD. SISDEV": data["sisdev_product"],
            "Nome da Propriedade": recipe["nome_propriedade"] if recipe else None,
            "Número do receituário": recipe["numero_receita"] if recipe else None,
            "ART": recipe["art"] if recipe else None, "Cultura": recipe["cultura"] if recipe else None,
            "Diagnóstico": recipe["diagnostico"] if recipe else None, "Área": recipe["area_receita"] if recipe else None,
            "Dose": _format_dose(dose, dose_type) if recipe else None, "Tipo de Dosagem": dose_type,
            "Quantidade.1": recipe["quantidade_receita"] if recipe else None, "Nome RT": recipe["nome_rt"] if recipe else None,
            "URE": data["center"], "Essa nota tem lançamento?": "Sim" if data["reconciliation_status"] == "CORRETO" else "Não",
            "Status auditoria": data["reconciliation_status"],
        })
        if limit and len(rows) >= limit:
            break
    conn.close()
    return rows


def _recipe_page_rows(conn, run_id):
    cnpj_map = _property_cnpj_map(conn)
    return [{
        "data_emissao": recipe["data_emissao"], "produto": recipe["produto"],
        "volume_receita": recipe["quantidade_receita"], "unidade": recipe["unidade_receita"],
        "emitido_por": recipe["nome_rt"], "art": recipe["art"], "numero_receita": recipe["numero_receita"],
        "diagnostico": recipe["diagnostico"], "nome_propriedade": recipe["nome_propriedade"],
        "cnpj": cnpj_map.get(key(recipe["nome_propriedade"]), "Não mapeado"),
    } for recipe in _recipes_for_regularization(conn, run_id)]


def _date_in_filters(value, filters):
    value = iso_date(value)
    lower = iso_date((filters or {}).get("from"))
    upper = iso_date((filters or {}).get("to"))
    return not ((lower and (not value or value < lower)) or (upper and (not value or value > upper)))


def _direction_in_filter(value, filters):
    requested = text((filters or {}).get("direction"))
    if not requested:
        return True
    actual = _movement_direction(value)
    requested_code = requested if requested in {"1", "2"} else _movement_direction(requested)
    return actual == requested_code


def page_records_v2(page, filters=None):
    filters = filters or {}
    data = dashboard_v2(filters)
    if not data.get("ready"):
        return data
    conn = connect()
    run_id = data["run"]["id"]
    expected_where, expected_params = _filter_sql(filters)
    summary = None

    if page == "pending":
        rows = [dict(row) for row in conn.execute(
            """SELECT r.status,r.diagnosis,r.confidence,e.nf,e.series,e.doc_date,e.center,
            CASE e.direction WHEN '1' THEN 'Entrada' WHEN '2' THEN 'Saída' ELSE e.direction END direcao,
            e.sap_material,e.lot lote_sap,e.manufacturer_lot lote_fabricante,e.quantity quantidade_sap,e.unit unidade_sap,
            a.product produto_sisdev,a.lot lote_sisdev,ABS(a.quantity*a.volume) quantidade_sisdev
            FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id LEFT JOIN actual_movements a ON a.id=r.actual_id
            WHERE r.run_id=?""" + expected_where + " AND r.status!='CORRETO' ORDER BY e.doc_date DESC,r.id DESC",
            [run_id, *expected_params],
        )]
    elif page == "regularization":
        rows = _regularization_rows(conn, run_id, filters.get("preferred_rt") or preferred_rt(conn), filters)
        summary = {
            "total": len(rows), "entries": sum(row["direcao"] == "Entrada" for row in rows),
            "exits": sum(row["direcao"] == "Saída" for row in rows),
            "recipes_suggested": sum(row["situacao"] == "RECEITA_SUGERIDA" for row in rows),
        }
    elif page == "recipes":
        rows = _recipe_page_rows(conn, run_id)
        rows = [row for row in rows if _date_in_filters(row["data_emissao"], filters)]
        if filters.get("preferred_rt"):
            rows = [row for row in rows if key(row["emitido_por"]) == key(filters["preferred_rt"])]
    elif page == "stocks":
        rows = _stock_rows(conn, run_id)
        if filters.get("center"):
            rows = [row for row in rows if key(row["centro"]) == key(filters["center"])]
    elif page == "reports":
        conn.close()
        return {"page": page, "rows": validation_rows(500, filters), "summary": None}
    elif page == "invoices":
        rows = [dict(row) for row in conn.execute(
            """SELECT e.nf,e.series,e.doc_date,e.center,CASE e.direction WHEN '1' THEN 'Entrada' WHEN '2' THEN 'Saída' ELSE e.direction END direcao,
            COUNT(*) linhas,SUM(e.quantity) quantidade_sap FROM expected_movements e WHERE e.run_id=?""" + expected_where +
            " GROUP BY e.nf,e.series,e.doc_date,e.center,e.direction ORDER BY e.doc_date DESC", [run_id, *expected_params],
        )]
        for row in rows:
            row["quantidade_sap"] = round(row["quantidade_sap"] or 0, 3)
    elif page == "movements":
        rows = [dict(row) for row in conn.execute(
            """SELECT nf,series,CASE lower(movement_type) WHEN 'entrada' THEN 'Entrada' WHEN 'saida' THEN 'Saída' ELSE movement_type END direcao,
            movement_date,product,lot,quantity embalagens,volume volume_embalagem,ABS(quantity*volume) quantidade_total,unit,status
            FROM actual_movements WHERE run_id=? ORDER BY movement_date DESC""", (run_id,),
        )]
        for row in rows:
            direction_code = _movement_direction(row["direcao"])
            row["direcao"] = "Entrada" if direction_code == "1" else "Saída" if direction_code == "2" else row["direcao"]
        rows = [row for row in rows if _date_in_filters(row["movement_date"], filters)
                and _direction_in_filter(row["direcao"], filters)]
    elif page == "analysis":
        rows = [dict(row) for row in conn.execute(
            """SELECT r.status,r.diagnosis,r.confidence,COUNT(*) ocorrencias FROM reconciliations r
            JOIN expected_movements e ON e.id=r.expected_id WHERE r.run_id=?""" + expected_where +
            " GROUP BY r.status,r.diagnosis,r.confidence ORDER BY ocorrencias DESC",
            [run_id, *expected_params],
        )]
    elif page == "materials":
        rows = [dict(row) for row in conn.execute(
            "SELECT e.sap_material material_sap,e.material_key normalizado,COUNT(*) ocorrencias FROM expected_movements e WHERE e.run_id=?" +
            expected_where + " GROUP BY e.sap_material,e.material_key ORDER BY ocorrencias DESC", [run_id, *expected_params],
        )]
    elif page == "lots":
        rows = [dict(row) for row in conn.execute(
            "SELECT e.sap_material material,e.lot lote_sap,e.manufacturer_lot lote_fabricante,COUNT(*) ocorrencias FROM expected_movements e WHERE e.run_id=?" +
            expected_where + " GROUP BY e.sap_material,e.lot,e.manufacturer_lot ORDER BY ocorrencias DESC", [run_id, *expected_params],
        )]
    elif page == "units":
        rows = [dict(row) for row in conn.execute(
            "SELECT e.center centro,e.cnpj,COUNT(*) ocorrencias FROM expected_movements e WHERE e.run_id=?" + expected_where +
            " GROUP BY e.center,e.cnpj ORDER BY ocorrencias DESC", [run_id, *expected_params],
        )]
    elif page == "movement_types":
        rows = [dict(row) for row in conn.execute(
            "SELECT e.direction codigo,CASE e.direction WHEN '1' THEN 'Entrada' WHEN '2' THEN 'Saída' ELSE e.direction END tipo,COUNT(*) ocorrencias FROM expected_movements e WHERE e.run_id=?" +
            expected_where + " GROUP BY e.direction", [run_id, *expected_params],
        )]
    elif page == "units_measure":
        rows = [dict(row) for row in conn.execute(
            "SELECT unit unidade,COUNT(*) ocorrencias FROM actual_movements WHERE run_id=? GROUP BY unit ORDER BY ocorrencias DESC", (run_id,),
        )]
    elif page == "history":
        rows = [dict(row) for row in conn.execute(
            "SELECT source,source_file,COUNT(*) linhas FROM source_records WHERE run_id=? GROUP BY source,source_file ORDER BY source", (run_id,),
        )]
    elif page == "logs":
        rows = [dict(row) for row in conn.execute("SELECT id,started_at,finished_at,status,summary_json FROM import_runs ORDER BY id DESC")]
    elif page == "rules":
        rows = [
            {"indicador": "Documento", "valor": "NF + série + produto + direção + data; centro quando disponível"},
            {"indicador": "Lote", "valor": "Lote fabricante prioritário"},
            {"indicador": "Quantidade", "valor": "SAP comparado ao valor absoluto de embalagens × volume SISDEV"},
            {"indicador": "Receita", "valor": "Produto e quantidade, emissão em D ou D-1, preferência de RT"},
            {"indicador": "Estoque", "valor": "Centro + produto + lote fabricante + unidade"},
        ]
        configured = [
            {"indicador": row["source_value"], "valor": row["target_value"]}
            for row in conn.execute(
                "SELECT source_value,target_value FROM reconciliation_mappings "
                "WHERE mapping_type='reconciliation_rule' AND status='ACTIVE' ORDER BY source_value"
            )
        ]
        if configured:
            rows.extend(configured)
    else:
        rows = [
            {"indicador": "Documentos SAP distintos", "valor": data["total"]},
            {"indicador": "Eficácia", "valor": f"{data['efficacy']}%"},
            {"indicador": "Divergências", "valor": data["statuses"].get("DIVERGENTE", 0)},
        ]
    conn.close()
    return {"page": page, "rows": rows, "summary": summary}
