import json
import csv
import io
import zipfile
import cgi
import os
import shutil
from xml.sax.saxutils import escape
from urllib.parse import parse_qs, urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .database import ROOT
from .engine import SOURCES, dashboard_v2, import_and_reconcile, page_records_v2, validation_rows, regularization_export_rows, rt_preference_options, set_preferred_rt

STATIC = ROOT / "src" / "web"
UPLOADS = {source: path for source, path, _ in SOURCES
           if source in {"sap_entry_current", "sap_exit_current", "sap_stock", "sisdev_movement", "agrotis_recipe"}}
MAX_UPLOAD_SIZE = 50 * 1024 * 1024

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)
    def do_GET(self):
        url=urlparse(self.path); query={key:value[-1] for key,value in parse_qs(url.query).items()}
        if url.path == "/api/dashboard":
            self._json(dashboard_v2(query)); return
        if url.path == "/api/settings/rt-preference":
            self._json(rt_preference_options()); return
        if url.path.startswith("/api/page/"):
            self._json(page_records_v2(url.path.rsplit("/",1)[-1],query)); return
        if url.path.startswith("/api/export/"):
            _, _, _, fmt, page = url.path.split("/", 4)
            self._export(page, fmt, query); return
        super().do_GET()
    def do_POST(self):
        if self.path == "/api/settings/rt-preference":
            try:
                length = int(self.headers.get("Content-Length", 0))
                value = json.loads(self.rfile.read(length) or b"{}").get("preferred_rt", "")
                set_preferred_rt(value)
                self._json({"ok": True, "preferred_rt": value})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path == "/api/upload":
            try:
                self._json(self._upload())
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return
        if self.path == "/api/import":
            try: self._json(import_and_reconcile())
            except Exception as exc: self._json({"error": str(exc)}, 500)
            return
        self.send_error(404)

    def _upload(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length or length > MAX_UPLOAD_SIZE:
            raise ValueError("Envie um arquivo de até 50 MB.")
        if "multipart/form-data" not in self.headers.get("Content-Type", ""):
            raise ValueError("Formato de envio inválido.")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]})
        source = form.getfirst("source", "")
        item = form["file"] if "file" in form else None
        if source not in UPLOADS:
            raise ValueError("Fonte de dados inválida.")
        if not item or not getattr(item, "filename", ""):
            raise ValueError("Selecione um arquivo para enviar.")
        if Path(item.filename).suffix.lower() not in {".xlsx", ".xls"}:
            raise ValueError("Envie uma planilha Excel (.xlsx ou .xls).")
        target = UPLOADS[source]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            shutil.copyfileobj(item.file, output)
        return {"ok": True, "source": source, "file": target.name}
    def _json(self, value, status=200):
        raw=json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def _export(self, page, fmt, query):
        if page == "reports": rows=validation_rows()
        elif page == "regularization": rows=regularization_export_rows(query)
        else: rows=page_records_v2(page,query).get("rows",[])
        columns=list(rows[0]) if rows else []
        if fmt == "csv":
            stream=io.StringIO(); writer=csv.DictWriter(stream,fieldnames=columns,extrasaction="ignore"); writer.writeheader(); writer.writerows(rows); data=stream.getvalue().encode("utf-8-sig"); mime="text/csv; charset=utf-8"; ext="csv"
        elif fmt == "xlsx":
            data=_xlsx(columns,rows); mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"; ext="xlsx"
        else: self.send_error(404); return
        self.send_response(200); self.send_header("Content-Type",mime); self.send_header("Content-Disposition",f'attachment; filename="sisdev_{page}.{ext}"'); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)

def _xlsx(columns, rows):
    def cell(value):
        value="" if value is None else str(value)
        if value[:1] in "=+-@": value="'"+value
        return f'<c t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    lines=[]
    for values in [columns]+[[r.get(c,"") for c in columns] for r in rows]: lines.append("<row>"+"".join(cell(v) for v in values)+"</row>")
    sheet='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+"".join(lines)+"</sheetData></worksheet>"
    parts={"[Content_Types].xml":'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',"_rels/.rels":'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',"xl/workbook.xml":'<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Dados" sheetId="1" r:id="rId1"/></sheets></workbook>',"xl/_rels/workbook.xml.rels":'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',"xl/worksheets/sheet1.xml":sheet}
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as z:
        for name,content in parts.items(): z.writestr(name,content)
    return output.getvalue()

def run():
    host = os.getenv("SISDEV_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    print(f"SISDEV AUDITOR em http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
