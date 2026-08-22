-- Neon/PostgreSQL migration for asynchronous imports.
-- Safe to run more than once in the Neon SQL Editor.

CREATE TABLE IF NOT EXISTS import_batches (
  id BIGSERIAL PRIMARY KEY,
  run_id BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN',
  required_sources TEXT NOT NULL DEFAULT
    'sap_entry_current,sap_exit_current,sap_entry_history,sap_exit_history,sap_stock,sisdev_stock,sisdev_movement,agrotis_recipe',
  reconciliation_workflow_run_id TEXT,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reconciled_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS import_jobs (
  id BIGSERIAL PRIMARY KEY,
  batch_id BIGINT,
  run_id BIGINT,
  source TEXT NOT NULL,
  source_file TEXT,
  blob_path TEXT NOT NULL,
  blob_url TEXT,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  cursor_row INTEGER NOT NULL DEFAULT 0,
  batch_size INTEGER NOT NULL DEFAULT 1000,
  processed_rows INTEGER NOT NULL DEFAULT 0,
  inserted_rows INTEGER NOT NULL DEFAULT 0,
  duplicate_rows INTEGER NOT NULL DEFAULT 0,
  error_rows INTEGER NOT NULL DEFAULT 0,
  total_rows INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  workflow_run_id TEXT,
  warning_json TEXT,
  error_message TEXT,
  started_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Upgrade the original, smaller import_jobs table in place.
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS batch_id BIGINT;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS run_id BIGINT;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS source_file TEXT;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS blob_url TEXT;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS cursor_row INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS batch_size INTEGER NOT NULL DEFAULT 1000;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS inserted_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS duplicate_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS error_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS workflow_run_id TEXT;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS warning_json TEXT;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS import_job_events (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL,
  status TEXT NOT NULL,
  cursor_row INTEGER NOT NULL DEFAULT 0,
  processed_rows INTEGER NOT NULL DEFAULT 0,
  total_rows INTEGER NOT NULL DEFAULT 0,
  message TEXT,
  payload_json TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reconciliation_mappings (
  id BIGSERIAL PRIMARY KEY,
  mapping_type TEXT NOT NULL,
  source_value TEXT NOT NULL,
  target_value TEXT NOT NULL,
  metadata_json TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(mapping_type, source_value)
);

CREATE INDEX IF NOT EXISTS import_jobs_status_idx
  ON import_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS import_jobs_batch_source_idx
  ON import_jobs(batch_id, source, created_at);
CREATE INDEX IF NOT EXISTS import_jobs_workflow_idx
  ON import_jobs(workflow_run_id);
CREATE INDEX IF NOT EXISTS import_job_events_job_idx
  ON import_job_events(job_id, created_at);
CREATE INDEX IF NOT EXISTS reconciliation_mappings_type_idx
  ON reconciliation_mappings(mapping_type, status);

-- Jobs created by the old synchronous importer do not identify a coherent
-- shared run. Never attach them to the latest run: that could mix unrelated
-- data and unlock an invalid reconciliation. They remain auditable but cannot
-- be started; every new upload is registered in a fresh/shared batch by API.
UPDATE import_jobs
SET status = 'SUPERSEDED',
    error_message = COALESCE(error_message, 'Envio legado sem ciclo de importação.'),
    source_file = COALESCE(source_file, regexp_replace(blob_path, '^.*/', '')),
    updated_at = now()
WHERE batch_id IS NULL;
