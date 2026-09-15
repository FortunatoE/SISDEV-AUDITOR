-- SISDEV Auditor: deduplicação de uploads e retenção segura por ciclo.
-- Execute com conexão direta (DATABASE_URL_UNPOOLED).

ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS file_sha256 TEXT;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS file_size BIGINT NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS import_jobs_batch_source_hash_idx
  ON import_jobs(batch_id, source, file_sha256);

CREATE TABLE IF NOT EXISTS run_archives (
  id BIGSERIAL PRIMARY KEY,
  run_id BIGINT NOT NULL UNIQUE,
  requested_by BIGINT NOT NULL REFERENCES app_users(id),
  manifest_path TEXT NOT NULL,
  manifest_checksum TEXT NOT NULL,
  status TEXT NOT NULL,
  workflow_run_id TEXT,
  processed_table TEXT,
  retained_originals INTEGER NOT NULL DEFAULT 0,
  released_rows INTEGER NOT NULL DEFAULT 0,
  details_json TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);

ALTER TABLE run_archives ADD COLUMN IF NOT EXISTS workflow_run_id TEXT;
ALTER TABLE run_archives ADD COLUMN IF NOT EXISTS processed_table TEXT;

CREATE INDEX IF NOT EXISTS run_archives_status_idx
  ON run_archives(status, created_at);

INSERT INTO app_settings(key, value)
VALUES ('storage_keep_recent_runs', '3')
ON CONFLICT(key) DO NOTHING;
