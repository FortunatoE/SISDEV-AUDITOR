import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import pandas as pd
from .database import ROOT, connect
from .normalization import document, key, lot, number, text, unit

DATA = ROOT / "dados"
SOURCES = [
    ("sap_entry_current", DATA / "entrada_sisdev.xlsx", 0),
    ("sap_exit_current", DATA / "saída_sisdev.xlsx", 0),
    ("sap_entry_history", DATA / "historico_sisdev" / "entrada_sisdev.xlsx", 0),
    ("sap_exit_history", DATA / "historico_sisdev" / "saída_sisdev.xlsx", 0),
    ("sap_stock", DATA / "MB52.xlsx", 0),
    ("sisdev_movement", DATA / "RelAnaliseMovimentacaoAgrotoxico (2).xlsx", 2),
    ("agrotis_recipe", DATA / "ReceitasEmitidas.xls", 0),
]
RECIPE_CACHE = {}


def field(row, *names):
    normalized = {key(k): v for k, v in row.items()}
    for name in names:
        if key(name) in normalized:
            return normalized[key(name)]
    return None


def iso_date(value):
    value = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(value) else value.date().isoformat()


def import_and_reconcile():
    conn = connect()
    cur = conn.execute("INSERT INTO import_runs(status) VALUES ('RUNNING') RETURNING id")
    run_id = cur.fetchone()[0]
    summary = {"sources": {}, "warnings": []}
    try:
        for source, path, skiprows in SOURCES:
            if not path.exists():
                summary["warnings"].append(f"Arquivo ausente: {path.name}")
                continue
            frame = pd.read_excel(path, skiprows=skiprows, dtype=object).dropna(axis=1, how="all")
            rows = 0
            for index, series in frame.iterrows():
                data = {str(k): (None if pd.isna(v) else str(v)) for k, v in series.items()}
                raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
                fp = hashlib.sha256((source + raw).encode()).hexdigest()
                record_id = conn.execute("INSERT INTO source_records(run_id,source,source_file,row_number,fingerprint,raw_json) VALUES (?,?,?,?,?,?) RETURNING id", (run_id, source, str(path.relative_to(ROOT)), int(index) + skiprows + 2, fp, raw)).fetchone()[0]
                if source.startswith("sap_") and source != "sap_stock":
                    conn.execute("INSERT INTO expected_movements(run_id,source_record_id,nf,series,direction,doc_date,sap_material,material_key,lot,manufacturer_lot,quantity,unit,center,cnpj) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        run_id, record_id, document(field(data, "Número de nota fiscal eletrônica")), text(field(data, "Séries")), text(field(data, "Direção do movimento")), iso_date(field(data, "Data documento")), text(field(data, "Texto breve material")), key(field(data, "Texto breve material")), lot(field(data, "Lote")), lot(field(data, "Lote Fabricante")), number(field(data, "Quantidade")), unit(field(data, "UMB")), text(field(data, "Centro")), text(field(data, "CNPJ"))))
                elif source == "sisdev_movement":
                    conn.execute("INSERT INTO actual_movements(run_id,source_record_id,nf,series,movement_type,movement_date,product,product_key,lot,quantity,volume,unit,cnpj,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        run_id, record_id, document(field(data, "Nº NF")), text(field(data, "SÉRIE NF")), text(field(data, "TIPO MOVIMENTO")), iso_date(field(data, "DATA MOVIMENTO")), text(field(data, "PRODUTO")), key(text(field(data, "PRODUTO")).split("-", 1)[-1]), lot(field(data, "LOTE")), number(field(data, "QNT")), number(field(data, "VOLUME")), unit(field(data, "U.M.")), text(field(data, "CPF/CNPJ REVENDA")), text(field(data, "SITUAÇÃO"))))
                rows += 1
            summary["sources"][source] = rows
        _flag_duplicate_sisdev(conn, run_id)
        _reconcile(conn, run_id)
        summary["warnings"].append("Saldo SISDEV em PDF registrado como fonte pendente de parser com coordenadas.")
        conn.execute("UPDATE import_runs SET status='SUCCESS', finished_at=CURRENT_TIMESTAMP, summary_json=? WHERE id=?", (json.dumps(summary, ensure_ascii=False), run_id))
        conn.commit()
        return {"run_id": run_id, **summary}
    except Exception as exc:
        conn.execute("UPDATE import_runs SET status='FAILED', finished_at=CURRENT_TIMESTAMP, summary_json=? WHERE id=?", (json.dumps({"error": str(exc)}), run_id))
        conn.commit()
        raise
    finally:
        conn.close()


def _flag_duplicate_sisdev(conn, run_id):
    rows = conn.execute("SELECT nf,series,product,lot,quantity,volume,unit,COUNT(*) n FROM actual_movements WHERE run_id=? GROUP BY nf,series,product,lot,quantity,volume,unit HAVING n>1", (run_id,)).fetchall()
    total = sum(r[7] - 1 for r in rows)
    if total:
        conn.execute("INSERT INTO audit_issues(run_id,severity,category,message,details_json) VALUES (?,?,?,?,?)", (run_id, "ALTA", "DUPLICIDADE_SISDEV", f"{total} registros SISDEV repetidos por chave operacional; aguardando regra de tratamento.", json.dumps({"groups": len(rows), "extra_rows": total})))


def _reconcile(conn, run_id):
    expected = conn.execute("SELECT * FROM expected_movements WHERE run_id=?", (run_id,)).fetchall()
    actual = conn.execute("SELECT * FROM actual_movements WHERE run_id=?", (run_id,)).fetchall()
    used = set()
    for e in expected:
        candidates = [a for a in actual if a["nf"] == e["nf"] and a["series"] == e["series"]]
        if not candidates:
            status, diagnosis, confidence, chosen = "NAO_LANCADO", "NF e série não encontrados no SISDEV.", "ALTA", None
        else:
            exact = [a for a in candidates if a["lot"] and a["lot"] == (e["manufacturer_lot"] or e["lot"])]
            sap_lot_match = [a for a in candidates if a["lot"] and a["lot"] == e["lot"]]
            chosen = next((a for a in exact if a["id"] not in used), next((a for a in sap_lot_match if a["id"] not in used), candidates[0]))
            used.add(chosen["id"])
            if not exact and sap_lot_match:
                status, diagnosis, confidence = "DIVERGENCIA_LOTE_FABRICANTE", "Lote SAP corresponde, mas o lote fabricante prioritário diverge.", "MEDIA"
            elif not exact:
                status, diagnosis, confidence = "DIVERGENCIA_LOTE", "Documento encontrado, porém lote fabricante não corresponde.", "MEDIA"
            elif e["quantity"] is not None and chosen["quantity"] is not None and chosen["volume"] is not None and abs(e["quantity"] - (chosen["quantity"] * chosen["volume"])) > 0.001:
                status, diagnosis, confidence = "DIVERGENCIA_QUANTIDADE", "Lote corresponde, mas a quantidade SAP difere do total SISDEV (embalagens × volume).", "MEDIA"
            else:
                status, diagnosis, confidence = "CORRETO", "Documento, lote fabricante e quantidade compatíveis.", "ALTA"
        conn.execute("INSERT INTO reconciliations(run_id,expected_id,actual_id,status,diagnosis,details_json,confidence) VALUES (?,?,?,?,?,?,?)", (run_id, e["id"], chosen["id"] if chosen else None, status, diagnosis, json.dumps({"nf": e["nf"], "sap_lot": e["lot"], "manufacturer_lot": e["manufacturer_lot"]}), confidence))
    for a in actual:
        if a["id"] not in used:
            conn.execute("INSERT INTO reconciliations(run_id,actual_id,status,diagnosis,details_json,confidence) VALUES (?,?,?,?,?,?)", (run_id, a["id"], "SEM_ORIGEM_SAP", "Movimentação SISDEV sem origem SAP encontrada.", json.dumps({"nf": a["nf"]}), "BAIXA"))


def dashboard():
    conn = connect()
    run = conn.execute("SELECT id,started_at,summary_json FROM import_runs WHERE status='SUCCESS' ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        conn.close(); return {"ready": False}
    # Indicadores principais seguem a linha de base aprovada: NF distintas do SAP,
    # e não linhas de lote ou movimentos SISDEV adicionais.
    statuses = {r["status"]: r["n"] for r in conn.execute("""
        SELECT r.status, COUNT(DISTINCT e.nf || '|' || COALESCE(e.series,'')) n
        FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id
        WHERE r.run_id=? GROUP BY r.status
    """, (run["id"],))}
    issues = [dict(r) for r in conn.execute("SELECT severity,category,message FROM audit_issues WHERE run_id=? ORDER BY id DESC LIMIT 8", (run["id"],))]
    rows = [dict(r) for r in conn.execute("SELECT r.status,r.diagnosis,e.nf,e.doc_date,e.sap_material,e.manufacturer_lot,e.quantity,a.volume FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id LEFT JOIN actual_movements a ON a.id=r.actual_id WHERE r.run_id=? AND r.status!='CORRETO' ORDER BY r.id DESC LIMIT 10", (run["id"],))]
    total = conn.execute("SELECT COUNT(DISTINCT nf || '|' || COALESCE(series,'')) FROM expected_movements WHERE run_id=?", (run["id"],)).fetchone()[0]
    correct = statuses.get("CORRETO", 0)
    conn.close()
    return {"ready": True, "run": dict(run), "total": total, "correct": correct, "efficacy": round(correct / total * 100, 1) if total else 0, "statuses": statuses, "issues": issues, "pending": rows}


def dashboard_v2(filters=None):
    """KPIs por NF distinta, com filtros aplicados antes da classificação."""
    filters = filters or {}
    conn = connect()
    run = conn.execute("SELECT id,started_at,summary_json FROM import_runs WHERE status='SUCCESS' ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        conn.close(); return {"ready": False}
    clauses, params = [], []
    for request_key, column in (("from", "doc_date >= ?"), ("to", "doc_date <= ?"), ("center", "center = ?"), ("direction", "direction = ?")):
        if filters.get(request_key): clauses.append(column); params.append(filters[request_key])
    where = " AND " + " AND ".join(clauses) if clauses else ""
    doc_query = """WITH linhas AS (
      SELECT e.nf,e.series,MAX(CASE WHEN r.status='NAO_LANCADO' THEN 3 WHEN r.status IN ('DIVERGENCIA_LOTE','DIVERGENCIA_QUANTIDADE') THEN 2 ELSE 1 END) p
      FROM expected_movements e JOIN reconciliations r ON r.expected_id=e.id
      WHERE e.run_id=?""" + where + " GROUP BY e.nf,e.series), docs AS (SELECT CASE p WHEN 3 THEN 'NAO_LANCADO' WHEN 2 THEN 'DIVERGENTE' ELSE 'CORRETO' END status FROM linhas) SELECT status,COUNT(*) n FROM docs GROUP BY status"
    statuses = {x["status"]: x["n"] for x in conn.execute(doc_query, [run["id"], *params])}
    statuses.setdefault("PENDENTE", 0)
    rows_query = "SELECT r.status,r.diagnosis,e.nf,e.doc_date,e.center,e.direction,e.sap_material,e.lot sap_lot,e.manufacturer_lot,e.quantity,a.product sisdev_product,a.lot sisdev_lot,a.quantity*a.volume actual_quantity FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id LEFT JOIN actual_movements a ON a.id=r.actual_id WHERE e.run_id=?" + where + " AND r.status!='CORRETO' ORDER BY e.doc_date DESC,r.id DESC LIMIT 100"
    rows = [dict(x) for x in conn.execute(rows_query, [run["id"], *params])]
    options = {"centers":[x[0] for x in conn.execute("SELECT DISTINCT center FROM expected_movements WHERE run_id=? AND center<>'' ORDER BY 1",(run["id"],))],"directions":[dict(value=x[0],label="Entrada" if x[0]=='1' else "Saída") for x in conn.execute("SELECT DISTINCT direction FROM expected_movements WHERE run_id=? ORDER BY 1",(run["id"],))],"range":dict(conn.execute("SELECT MIN(doc_date) min_date,MAX(doc_date) max_date FROM expected_movements WHERE run_id=?",(run["id"],)).fetchone())}
    issues=[dict(x) for x in conn.execute("SELECT severity,category,message FROM audit_issues WHERE run_id=? ORDER BY id DESC LIMIT 8",(run["id"],))]
    total=sum(statuses.values()); correct=statuses.get("CORRETO",0)
    conn.close()
    launched=total-statuses.get("NAO_LANCADO",0)
    return {"ready":True,"run":dict(run),"total":total,"correct":correct,"efficacy":round(correct/total*100,1) if total else 0,"launched":launched,"adherence":round(launched/total*100,1) if total else 0,"statuses":statuses,"issues":issues,"pending":rows,"options":options}


def page_records(page, filters=None):
    data=dashboard_v2(filters)
    if not data.get("ready"): return data
    conn=connect(); run_id=data["run"]["id"]
    if page == "movements":
        rows=[dict(x) for x in conn.execute("SELECT nf,series,movement_date,product,lot,volume,unit,status FROM actual_movements WHERE run_id=? ORDER BY movement_date DESC LIMIT 200",(run_id,))]
    elif page == "recipes":
        rows=[]
        for x in conn.execute("SELECT raw_json FROM source_records WHERE run_id=? AND source='agrotis_recipe' ORDER BY row_number LIMIT 200",(run_id,)):
            raw=json.loads(x["raw_json"]); rows.append({"receituario":field(raw,"Número do receituário"),"data":field(raw,"Data de Emissão"),"produto":field(raw,"Produto"),"propriedade":field(raw,"Nome da Propriedade"),"quantidade":field(raw,"Quantidade")})
    else: rows=data["pending"]
    conn.close(); return {"page":page,"rows":rows}


def validation_rows(limit=None):
    frame=pd.read_excel(ROOT / "Acompanhamento SISDEV.xlsx",sheet_name="Validação",dtype=object)
    if limit: frame=frame.head(limit)
    return [{str(k):(None if pd.isna(v) else (v.isoformat() if hasattr(v,"isoformat") else v)) for k,v in line.items()} for _,line in frame.iterrows()]


def _recipes_for_regularization(conn, run_id):
    """Return the operational fields from Agrotis recipes in a stable schema."""
    if run_id in RECIPE_CACHE:
        return RECIPE_CACHE[run_id]
    recipes = []
    for record in conn.execute("SELECT raw_json FROM source_records WHERE run_id=? AND source='agrotis_recipe'", (run_id,)):
        values = list(json.loads(record["raw_json"]).values())
        if len(values) < 15:
            continue
        recipes.append({
            "data_emissao": iso_date(values[2]), "produto": text(values[10]),
            "numero_receita": text(values[9]), "art": text(values[0]),
            "nome_rt": text(values[7]), "cultura": text(values[1]),
            "diagnostico": text(values[3]), "dose_recomendada": _recipe_number(values[4]),
            "tipo_dosagem": text(values[12]), "area_receita": _recipe_number(values[14]),
            "quantidade_receita": _recipe_number(values[11]), "unidade_receita": text(values[13]),
        })
    RECIPE_CACHE[run_id] = recipes
    return recipes


def _recipe_number(value):
    """Agrotis exports decimal values with a dot: 60.000 means 60."""
    raw = text(value)
    if "." in raw and "," not in raw:
        try:
            return float(Decimal(raw))
        except (InvalidOperation, ValueError):
            pass
    return number(value)


def preferred_rt(conn):
    row = conn.execute("SELECT value FROM app_settings WHERE key='preferred_rt'").fetchone()
    return row["value"] if row else "KARLA DANIELLY GARCIA DE LIMA"


def set_preferred_rt(value):
    conn = connect()
    conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES ('preferred_rt',?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", (text(value),))
    conn.commit(); conn.close()


def rt_preference_options():
    conn = connect()
    run = conn.execute("SELECT id FROM import_runs WHERE status='SUCCESS' ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        value = preferred_rt(conn)
        conn.close(); return {"preferred_rt": value, "options": []}
    options = sorted({recipe["nome_rt"] for recipe in _recipes_for_regularization(conn, run["id"]) if recipe["nome_rt"]})
    value = preferred_rt(conn)
    conn.close()
    return {"preferred_rt": value, "options": options}


def _product_matches(left, right):
    left, right = key(left), key(right)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    left_terms = {term for term in left.split() if len(term) > 3}
    right_terms = {term for term in right.split() if len(term) > 3}
    return bool(left_terms & right_terms)


def _normalized_dose(value, dosage_type):
    """Normalize operational dose units so the area calculation uses kg or L."""
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


def _regularization_rows(conn, run_id, preferred_name=None):
    """Operational queue: one SAP line that still needs a SISDEV posting."""
    recipes = _recipes_for_regularization(conn, run_id)
    recipes_by_date = {}
    for recipe in recipes:
        recipes_by_date.setdefault(recipe["data_emissao"], []).append(recipe)
    expected = conn.execute("""SELECT e.doc_date,e.cnpj,e.nf,e.series,e.direction,e.sap_material,
        e.lot,e.manufacturer_lot,e.quantity,e.unit,e.center
        FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id
        WHERE r.run_id=? AND r.status='NAO_LANCADO'
        ORDER BY e.doc_date DESC,e.nf,e.id""", (run_id,))
    rows = []
    for item in expected:
        row = dict(item)
        direction = "Entrada" if row["direction"] == "1" else "Saída"
        base = {
            "direcao": direction, "data_documento": row["doc_date"], "cnpj": row["cnpj"],
            "numero_nf": row["nf"], "serie": row["series"], "produto": row["sap_material"],
            "lote": row["manufacturer_lot"] or row["lot"], "lote_sap": row["lot"],
            "quantidade_sap": row["quantity"], "unidade_sap": row["unit"], "ure": row["center"],
            "volume_embalagem": None, "quantidade_embalagens": None,
        }
        if direction == "Entrada":
            base.update({"situacao": "PREENCHER_EMBALAGEM", "pendencia": "Informar volume da embalagem e quantidade de embalagens no SISDEV."})
            rows.append(base)
            continue
        document_date = datetime.strptime(row["doc_date"], "%Y-%m-%d").date() if row["doc_date"] else None
        date_candidates = [] if not document_date else recipes_by_date.get(document_date.isoformat(), []) + recipes_by_date.get((document_date - timedelta(days=1)).isoformat(), [])
        candidates = [recipe for recipe in date_candidates if _product_matches(row["sap_material"], recipe["produto"])]
        preferred_candidates = [recipe for recipe in candidates if preferred_name and key(recipe["nome_rt"]) == key(preferred_name)]
        if len(preferred_candidates) == 1:
            candidates = preferred_candidates
        if len(candidates) == 1:
            recipe = candidates[0]
            emission_window = "D" if recipe["data_emissao"] == document_date.isoformat() else "D-1"
            dose, dose_type = _normalized_dose(recipe["dose_recomendada"], recipe["tipo_dosagem"])
            calculated_area = round((row["quantity"] or 0) / dose, 6) if dose else None
            base.update({
                "numero_receita": recipe["numero_receita"], "art": recipe["art"], "nome_rt": recipe["nome_rt"],
                "cultura": recipe["cultura"], "diagnostico": recipe["diagnostico"],
                "dose_recomendada": _format_dose(dose, dose_type),
                "tipo_dosagem": dose_type,
                "area_receita": recipe["area_receita"], "area_calculada": calculated_area,
                "janela_receita": emission_window, "situacao": "RECEITA_SUGERIDA",
                "pendencia": "Validar volume e quantidade de embalagens; receita localizada em " + emission_window + ".",
            })
        elif len(candidates) > 1:
            base.update({"situacao": "RECEITAS_MULTIPLAS", "janela_receita": "D/D-1", "pendencia": f"Selecionar uma entre {len(candidates)} receitas candidatas; depois informar embalagem."})
        else:
            base.update({"situacao": "SEM_RECEITA", "janela_receita": "D/D-1", "pendencia": "Localizar receita do mesmo dia ou D-1 e informar dados da embalagem."})
        rows.append(base)
    return rows


def regularization_export_rows(filters=None):
    filters = filters or {}
    conn = connect()
    run = conn.execute("SELECT id FROM import_runs WHERE status='SUCCESS' ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        conn.close(); return []
    preference = filters.get("preferred_rt") or preferred_rt(conn)
    rows = _regularization_rows(conn, run["id"], preference)
    conn.close()
    columns = {
        "Data documento": "data_documento", "CNPJ": "cnpj", "Número de nota fiscal eletrônica": "numero_nf", "Séries": "serie", "Produto": "produto", "Lote": "lote", "Quantidade": "quantidade_sap", "Volume da embalagem": "volume_embalagem", "Quantidade de embalagem": "quantidade_embalagens", "Número de receituário": "numero_receita", "ART": "art", "Nome RT": "nome_rt", "Cultura": "cultura", "Diagnóstico": "diagnostico", "Unidade recebimento de embalagem (URE)": "ure", "Dose recomendada": "dose_recomendada", "Área (quantidade do lote/dose)": "area_calculada",
    }
    return [{name: row.get(key_name) for name, key_name in columns.items()} for row in rows if row["direcao"] == "Saída"]


def page_records_v2(page, filters=None):
    data = dashboard_v2(filters)
    if not data.get("ready"): return data
    conn=connect(); run_id=data["run"]["id"]
    queries={
      "analysis":"SELECT status,diagnosis,confidence,COUNT(*) ocorrencias FROM reconciliations WHERE run_id=? GROUP BY status,diagnosis,confidence ORDER BY ocorrencias DESC",
      "invoices":"SELECT nf,series,doc_date,center,CASE direction WHEN '1' THEN 'Entrada' WHEN '2' THEN 'Saída' ELSE direction END direcao,COUNT(*) linhas,ROUND(SUM(quantity),3) quantidade_sap FROM expected_movements WHERE run_id=? GROUP BY nf,series,doc_date,center,direction ORDER BY doc_date DESC LIMIT 500",
      "movements":"SELECT nf,series,CASE lower(movement_type) WHEN 'entrada' THEN 'Entrada' WHEN 'saida' THEN 'Saída' ELSE movement_type END direcao,movement_date,product,lot,quantity embalagens,volume volume_embalagem,quantity*volume quantidade_total,unit,status FROM actual_movements WHERE run_id=? ORDER BY movement_date DESC LIMIT 500",
      "materials":"SELECT sap_material material_sap,material_key normalizado,COUNT(*) ocorrencias FROM expected_movements WHERE run_id=? GROUP BY sap_material,material_key ORDER BY ocorrencias DESC",
      "lots":"SELECT sap_material material,lot lote_sap,manufacturer_lot lote_fabricante,COUNT(*) ocorrencias FROM expected_movements WHERE run_id=? GROUP BY sap_material,lot,manufacturer_lot ORDER BY ocorrencias DESC",
      "units":"SELECT center centro,cnpj,COUNT(*) ocorrencias FROM expected_movements WHERE run_id=? GROUP BY center,cnpj ORDER BY ocorrencias DESC",
      "movement_types":"SELECT direction codigo,CASE direction WHEN '1' THEN 'Entrada' WHEN '2' THEN 'Saída' ELSE direction END tipo,COUNT(*) ocorrencias FROM expected_movements WHERE run_id=? GROUP BY direction",
      "units_measure":"SELECT unit unidade,COUNT(*) ocorrencias FROM actual_movements WHERE run_id=? GROUP BY unit ORDER BY ocorrencias DESC",
      "history":"SELECT source,source_file,COUNT(*) linhas FROM source_records WHERE run_id=? GROUP BY source,source_file",
    }
    if page == "pending":
      rows=[dict(x) for x in conn.execute("""SELECT r.status,r.diagnosis,r.confidence,e.nf,e.series,e.doc_date,e.center,
        CASE e.direction WHEN '1' THEN 'Entrada' WHEN '2' THEN 'Saída' ELSE e.direction END direcao,
        e.sap_material,e.lot lote_sap,e.manufacturer_lot lote_fabricante,e.quantity quantidade_sap,e.unit unidade_sap,
        a.product produto_sisdev,a.lot lote_sisdev,a.quantity*a.volume quantidade_sisdev
        FROM reconciliations r JOIN expected_movements e ON e.id=r.expected_id LEFT JOIN actual_movements a ON a.id=r.actual_id
        WHERE r.run_id=? AND r.status!='CORRETO' ORDER BY e.doc_date DESC,r.id DESC LIMIT 1000""",(run_id,))]
    elif page == "regularization":
      rows=_regularization_rows(conn, run_id, filters.get("preferred_rt") or preferred_rt(conn))
    elif page == "recipes":
      rows=[]
      for x in conn.execute("SELECT raw_json FROM source_records WHERE run_id=? AND source='agrotis_recipe' ORDER BY row_number LIMIT 500",(run_id,)):
        raw=json.loads(x["raw_json"]); rows.append({"receituario":field(raw,"Número do receituário"),"data":field(raw,"Data de Emissão"),"produto":field(raw,"Produto"),"propriedade":field(raw,"Nome da Propriedade"),"quantidade":field(raw,"Quantidade")})
    if page == "recipes":
      premissa=pd.read_excel(ROOT / "Acompanhamento SISDEV.xlsx",sheet_name="Premissa",dtype=object)
      cnpj_by_property={key(v.iloc[18]):text(v.iloc[19]) for _,v in premissa.iterrows() if len(v)>19 and pd.notna(v.iloc[18]) and pd.notna(v.iloc[19])}
      rows=[]
      for x in conn.execute("SELECT raw_json FROM source_records WHERE run_id=? AND source='agrotis_recipe' ORDER BY row_number LIMIT 500",(run_id,)):
        vals=list(json.loads(x["raw_json"]).values()); prop=text(vals[8]) if len(vals)>8 else ""
        rows.append({"data_emissao":text(vals[2]),"produto":text(vals[10]),"volume_receita":text(vals[11]),"unidade":text(vals[13]),"emitido_por":text(vals[7]),"art":text(vals[0]),"numero_receita":text(vals[9]),"diagnostico":text(vals[3]),"nome_propriedade":prop,"cnpj":cnpj_by_property.get(key(prop),"Não mapeado")})
    elif page == "stocks":
      rows=[]
      for x in conn.execute("SELECT raw_json FROM source_records WHERE run_id=? AND source='sap_stock' ORDER BY row_number LIMIT 500",(run_id,)):
        raw=json.loads(x["raw_json"]); rows.append({"centro":field(raw,"Centro"),"deposito":field(raw,"Depósito"),"material":field(raw,"Texto breve material"),"lote":field(raw,"Lote"),"lote_fabricante":field(raw,"Lote Fabricante"),"quantidade_sap":field(raw,"Utilização livre"),"unidade":field(raw,"UMB")})
    if page in ("pending", "regularization", "recipes"):
      pass
    elif page == "stocks":
      sisdev=pd.read_excel(ROOT / "Acompanhamento SISDEV.xlsx",sheet_name="Estoque_SISDEV",skiprows=2,dtype=object).dropna(axis=1,how="all")
      saldo={}
      for _, line in sisdev.iterrows():
        raw={str(k):None if pd.isna(v) else str(v) for k,v in line.items()}
        saldo[(key(field(raw,"PRODUTO")),lot(field(raw,"LOTE")))]=(number(field(raw,"VOLUME")) or 0,unit(field(raw,"U.M.")))
      for row in rows:
        sisdev_qtd,sisdev_um=saldo.get((key(row["material"]),lot(row["lote"])),(0,unit(row["unidade"])))
        sap_qtd=number(row["quantidade_sap"]) or 0
        row["quantidade_sap"]=sap_qtd; row["quantidade_sisdev"]=sisdev_qtd; row["diferenca_sisdev_menos_sap"]=round(sisdev_qtd-sap_qtd,3); row["unidade"]=sisdev_um
    elif page == "reports": rows=validation_rows(500)
    elif page == "logs": rows=[dict(x) for x in conn.execute("SELECT id,started_at,finished_at,status,summary_json FROM import_runs ORDER BY id DESC")]
    elif page in queries: rows=[dict(x) for x in conn.execute(queries[page],(run_id,))]
    else: rows=[{"indicador":"Documentos SAP distintos","valor":data["total"]},{"indicador":"Eficácia","valor":f'{data["efficacy"]}%'},{"indicador":"Divergências","valor":data["statuses"].get("DIVERGENTE",0)}]
    summary = None
    if page == "regularization":
      summary = {"total": len(rows), "entries": sum(x["direcao"] == "Entrada" for x in rows), "exits": sum(x["direcao"] == "Saída" for x in rows), "recipes_suggested": sum(x["situacao"] == "RECEITA_SUGERIDA" for x in rows)}
    conn.close(); return {"page":page,"rows":rows,"summary":summary}
