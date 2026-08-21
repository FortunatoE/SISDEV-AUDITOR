"""Vercel entry point.

Vercel discovers the Flask ``app`` object and executes it as a Python Function.
The local desktop workflow continues to use ``src/main.py``.
"""
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auditor.engine import (dashboard_v2, import_and_reconcile, page_records_v2,
                            regularization_export_rows, rt_preference_options,
                            set_preferred_rt, validation_rows)
from auditor.server import _xlsx
from auditor.database import connect

STATIC = ROOT / "src" / "web"
app = Flask(__name__)


@app.get("/api/dashboard")
def dashboard():
    return jsonify(dashboard_v2(request.args.to_dict()))


@app.get("/api/page/<page>")
def page(page):
    return jsonify(page_records_v2(page, request.args.to_dict()))


@app.get("/api/settings/rt-preference")
def get_rt_preference():
    return jsonify(rt_preference_options())


@app.post("/api/settings/rt-preference")
def post_rt_preference():
    value = (request.get_json(silent=True) or {}).get("preferred_rt", "")
    set_preferred_rt(value)
    return jsonify({"ok": True, "preferred_rt": value})


@app.post("/api/import")
def import_data():
    try:
        return jsonify(import_and_reconcile())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/upload")
def upload_data():
    source = request.form.get("source", "")
    upload = request.files.get("file")
    allowed = {"sap_entry_current", "sap_exit_current", "sap_entry_history", "sap_exit_history", "sap_stock", "sisdev_stock", "sisdev_movement", "agrotis_recipe"}
    if source not in allowed or not upload or not upload.filename:
        return jsonify({"error": "Fonte ou arquivo inválido."}), 400
    extension = Path(upload.filename).suffix.lower()
    if extension not in ({".pdf"} if source == "sisdev_stock" else {".xlsx", ".xls"}):
        return jsonify({"error": "Formato de arquivo inválido para esta fonte."}), 400
    from vercel.blob import BlobClient
    blob = BlobClient().put(f"sisdev/{source}/{secure_filename(upload.filename)}", upload.read(), access="private", add_random_suffix=True)
    conn = connect()
    job = conn.execute("INSERT INTO import_jobs(source,blob_path,status) VALUES (?,?,?) RETURNING id", (source, blob.pathname, "QUEUED")).fetchone()[0]
    conn.commit(); conn.close()
    return jsonify({"ok": True, "job_id": job, "source": source, "file": upload.filename, "url": blob.url, "pathname": blob.pathname})


@app.get("/api/export/<fmt>/<page>")
def export(fmt, page):
    if page == "reports":
        rows = validation_rows()
    elif page == "regularization":
        rows = regularization_export_rows(request.args.to_dict())
    else:
        rows = page_records_v2(page, request.args.to_dict()).get("rows", [])
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
            "Content-Disposition": f'attachment; filename="sisdev_{page}.csv"',
        }
    if fmt == "xlsx":
        return _xlsx(columns, rows), 200, {
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Content-Disposition": f'attachment; filename="sisdev_{page}.xlsx"',
        }
    return jsonify({"error": "Formato não suportado."}), 404


@app.get("/")
@app.get("/<path:path>")
def static_site(path="index.html"):
    return send_from_directory(STATIC, path or "index.html")
