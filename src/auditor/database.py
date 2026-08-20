import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "banco" / "sisdev_auditor.sqlite"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS import_runs (id INTEGER PRIMARY KEY, started_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT, status TEXT NOT NULL, summary_json TEXT);
CREATE TABLE IF NOT EXISTS source_records (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, source TEXT NOT NULL, source_file TEXT NOT NULL, row_number INTEGER NOT NULL, fingerprint TEXT NOT NULL, raw_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(run_id, source, row_number));
CREATE TABLE IF NOT EXISTS dim_material (id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS material_map (id INTEGER PRIMARY KEY, system_name TEXT NOT NULL, original_value TEXT NOT NULL, normalized_value TEXT NOT NULL, material_id INTEGER NOT NULL, approved INTEGER NOT NULL DEFAULT 0, UNIQUE(system_name, normalized_value));
CREATE TABLE IF NOT EXISTS dim_lot (id INTEGER PRIMARY KEY, canonical_value TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS lot_map (id INTEGER PRIMARY KEY, system_name TEXT NOT NULL, material_id INTEGER, original_value TEXT NOT NULL, normalized_value TEXT NOT NULL, lot_id INTEGER NOT NULL, approved INTEGER NOT NULL DEFAULT 0, UNIQUE(system_name, material_id, normalized_value));
CREATE TABLE IF NOT EXISTS audit_issues (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, severity TEXT NOT NULL, category TEXT NOT NULL, reference TEXT, message TEXT NOT NULL, details_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS expected_movements (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, source_record_id INTEGER, nf TEXT, series TEXT, direction TEXT, doc_date TEXT, sap_material TEXT, material_key TEXT, lot TEXT, manufacturer_lot TEXT, quantity REAL, unit TEXT, center TEXT, cnpj TEXT, status TEXT NOT NULL DEFAULT 'PENDENTE');
CREATE TABLE IF NOT EXISTS actual_movements (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, source_record_id INTEGER, nf TEXT, series TEXT, movement_type TEXT, movement_date TEXT, product TEXT, product_key TEXT, lot TEXT, quantity REAL, volume REAL, unit TEXT, cnpj TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS reconciliations (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, expected_id INTEGER, actual_id INTEGER, status TEXT NOT NULL, diagnosis TEXT NOT NULL, details_json TEXT, confidence TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS action_history (id INTEGER PRIMARY KEY, reconciliation_id INTEGER, action TEXT NOT NULL, user_name TEXT, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
