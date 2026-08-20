import json
import csv
import io
import zipfile
from xml.sax.saxutils import escape
from urllib.parse import parse_qs, urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .database import ROOT
from .engine import dashboard_v2, import_and_reconcile, page_records_v2, validation_rows

STATIC = ROOT / "src" / "web"

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)
    def do_GET(self):
        url=urlparse(self.path); query={key:value[-1] for key,value in parse_qs(url.query).items()}
        if url.path == "/api/dashboard":
            self._json(dashboard_v2(query)); return
        if url.path.startswith("/api/page/"):
            self._json(page_records_v2(url.path.rsplit("/",1)[-1],query)); return
        if url.path.startswith("/api/export/"):
            _, _, _, fmt, page = url.path.split("/", 4)
            self._export(page, fmt, query); return
        super().do_GET()
    def do_POST(self):
        if self.path == "/api/import":
            try: self._json(import_and_reconcile())
            except Exception as exc: self._json({"error": str(exc)}, 500)
            return
        self.send_error(404)
    def _json(self, value, status=200):
        raw=json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def _export(self, page, fmt, query):
        rows=validation_rows() if page == "reports" else page_records_v2(page,query).get("rows",[]); columns=list(rows[0]) if rows else []
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
    print("SISDEV AUDITOR em http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
