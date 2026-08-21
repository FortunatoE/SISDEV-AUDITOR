import os, sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; DB_PATH=ROOT/'banco'/'sisdev_auditor.sqlite'
BASE='''CREATE TABLE IF NOT EXISTS import_runs (id {id} PRIMARY KEY, started_at {time} DEFAULT CURRENT_TIMESTAMP, finished_at {time}, status TEXT NOT NULL, summary_json TEXT); CREATE TABLE IF NOT EXISTS source_records (id {id} PRIMARY KEY, run_id BIGINT NOT NULL, source TEXT NOT NULL, source_file TEXT NOT NULL, row_number INTEGER NOT NULL, fingerprint TEXT NOT NULL, raw_json TEXT NOT NULL, created_at {time} DEFAULT CURRENT_TIMESTAMP, UNIQUE(run_id,source,row_number)); CREATE TABLE IF NOT EXISTS audit_issues (id {id} PRIMARY KEY,run_id BIGINT NOT NULL,severity TEXT NOT NULL,category TEXT NOT NULL,reference TEXT,message TEXT NOT NULL,details_json TEXT,created_at {time} DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS expected_movements (id {id} PRIMARY KEY,run_id BIGINT NOT NULL,source_record_id BIGINT,nf TEXT,series TEXT,direction TEXT,doc_date TEXT,sap_material TEXT,material_key TEXT,lot TEXT,manufacturer_lot TEXT,quantity DOUBLE PRECISION,unit TEXT,center TEXT,cnpj TEXT,status TEXT NOT NULL DEFAULT 'PENDENTE'); CREATE TABLE IF NOT EXISTS actual_movements (id {id} PRIMARY KEY,run_id BIGINT NOT NULL,source_record_id BIGINT,nf TEXT,series TEXT,movement_type TEXT,movement_date TEXT,product TEXT,product_key TEXT,lot TEXT,quantity DOUBLE PRECISION,volume DOUBLE PRECISION,unit TEXT,cnpj TEXT,status TEXT); CREATE TABLE IF NOT EXISTS reconciliations (id {id} PRIMARY KEY,run_id BIGINT NOT NULL,expected_id BIGINT,actual_id BIGINT,status TEXT NOT NULL,diagnosis TEXT NOT NULL,details_json TEXT,confidence TEXT NOT NULL,created_at {time} DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS action_history (id {id} PRIMARY KEY,reconciliation_id BIGINT,action TEXT NOT NULL,user_name TEXT,reason TEXT,created_at {time} DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at {time} DEFAULT CURRENT_TIMESTAMP);'''
class Row(dict):
 def __init__(self,x): super().__init__(x); self.v=list(x.values())
 def __getitem__(self,k): return self.v[k] if isinstance(k,int) else super().__getitem__(k)
class Cursor:
 def __init__(self,c): self.c=c
 def __iter__(self): return iter(self.fetchall())
 def fetchone(self): x=self.c.fetchone(); return Row(x) if x else None
 def fetchall(self): return [Row(x) for x in self.c.fetchall()]
class Connection:
 def __init__(self,c): self.c=c
 def execute(self,q,p=None): return Cursor(self.c.execute(q.replace('?','%s'),p or ()))
 def commit(self): self.c.commit()
 def rollback(self): self.c.rollback()
 def close(self): self.c.close()
def connect():
 url=os.getenv('DATABASE_URL')
 if url:
  import psycopg
  from psycopg.rows import dict_row
  c=psycopg.connect(url,row_factory=dict_row)
  with c.cursor() as x:
   for s in BASE.format(id='BIGSERIAL',time='TIMESTAMPTZ').split(';'):
    if s.strip(): x.execute(s)
   x.execute("INSERT INTO app_settings(key,value) VALUES ('preferred_rt','KARLA DANIELLY GARCIA DE LIMA') ON CONFLICT(key) DO NOTHING")
  c.commit(); return Connection(c)
 DB_PATH.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
 c.executescript('PRAGMA foreign_keys=ON;'+BASE.format(id='INTEGER',time='TEXT'))
 c.execute("INSERT INTO app_settings(key,value) VALUES ('preferred_rt','KARLA DANIELLY GARCIA DE LIMA') ON CONFLICT(key) DO NOTHING"); c.commit(); return c
