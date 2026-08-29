-- SISDEV Auditor: document-oriented regularization queue and human decisions.
-- Run with the direct/unpooled Neon connection. Safe to run more than once.

BEGIN;

CREATE TABLE IF NOT EXISTS public.document_decisions (
  id BIGSERIAL PRIMARY KEY,
  run_id BIGINT NOT NULL,
  document_key TEXT NOT NULL,
  decision_type TEXT NOT NULL,
  selected_ids_json TEXT,
  status TEXT NOT NULL DEFAULT 'SAVED',
  justification TEXT,
  user_id BIGINT REFERENCES public.app_users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, document_key, decision_type)
);

CREATE INDEX IF NOT EXISTS document_decisions_document_idx
  ON public.document_decisions(run_id, document_key, updated_at);
CREATE INDEX IF NOT EXISTS expected_movements_document_idx
  ON public.expected_movements(run_id, nf, series, cnpj, direction, center);
CREATE INDEX IF NOT EXISTS expected_movements_match_idx
  ON public.expected_movements(run_id, direction, material_key, doc_date);
CREATE INDEX IF NOT EXISTS actual_movements_document_idx
  ON public.actual_movements(run_id, nf, series);
CREATE INDEX IF NOT EXISTS reconciliations_run_status_idx
  ON public.reconciliations(run_id, status, expected_id, actual_id);
CREATE INDEX IF NOT EXISTS reconciliations_expected_idx
  ON public.reconciliations(expected_id, run_id);
CREATE INDEX IF NOT EXISTS reconciliations_run_expected_idx
  ON public.reconciliations(run_id, expected_id);

COMMIT;
