-- SISDEV Auditor: hardening for authenticated audit identity and session expiry.
-- Run with the direct/unpooled Neon connection. Safe to run more than once.

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE contype = 'f'
      AND conrelid = 'public.user_scopes'::regclass
      AND confrelid = 'public.app_users'::regclass
  ) THEN
    ALTER TABLE public.user_scopes
      ADD CONSTRAINT user_scopes_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES public.app_users(id) NOT VALID;
    ALTER TABLE public.user_scopes VALIDATE CONSTRAINT user_scopes_user_id_fkey;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE contype = 'f'
      AND conrelid = 'public.auth_sessions'::regclass
      AND confrelid = 'public.app_users'::regclass
  ) THEN
    ALTER TABLE public.auth_sessions
      ADD CONSTRAINT auth_sessions_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES public.app_users(id) NOT VALID;
    ALTER TABLE public.auth_sessions VALIDATE CONSTRAINT auth_sessions_user_id_fkey;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE contype = 'f'
      AND conrelid = 'public.audit_log'::regclass
      AND confrelid = 'public.app_users'::regclass
  ) THEN
    ALTER TABLE public.audit_log
      ADD CONSTRAINT audit_log_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES public.app_users(id) NOT VALID;
    ALTER TABLE public.audit_log VALIDATE CONSTRAINT audit_log_user_id_fkey;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE contype = 'f'
      AND conrelid = 'public.backup_registry'::regclass
      AND confrelid = 'public.app_users'::regclass
  ) THEN
    ALTER TABLE public.backup_registry
      ADD CONSTRAINT backup_registry_requested_by_fkey
      FOREIGN KEY (requested_by) REFERENCES public.app_users(id) NOT VALID;
    ALTER TABLE public.backup_registry VALIDATE CONSTRAINT backup_registry_requested_by_fkey;
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS auth_sessions_validation_idx
  ON public.auth_sessions(token_hash, revoked_at, expires_at, last_seen_at);

COMMIT;
