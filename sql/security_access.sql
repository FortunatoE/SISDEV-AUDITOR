-- SISDEV Auditor: autenticação, autorização, escopos, auditoria e backups.
-- Execute com conexão direta (não pooler) nas migrações do Neon.

CREATE TABLE IF NOT EXISTS app_users (
  id BIGSERIAL PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  profile TEXT NOT NULL CHECK (profile IN ('ADMINISTRADOR','GESTOR','AUDITOR','OPERADOR','CONSULTA')),
  active INTEGER NOT NULL DEFAULT 1,
  failed_login_count INTEGER NOT NULL DEFAULT 0,
  locked_until TIMESTAMPTZ,
  last_login_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE app_users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS user_scopes (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES app_users(id),
  scope_type TEXT NOT NULL CHECK (scope_type IN ('CENTER','UNIT','PROPERTY')),
  scope_value TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, scope_type, scope_value)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES app_users(id),
  token_hash TEXT NOT NULL UNIQUE,
  csrf_hash TEXT NOT NULL,
  ip_address TEXT,
  user_agent TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS login_attempts (
  id BIGSERIAL PRIMARY KEY,
  email TEXT,
  ip_address TEXT,
  success INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES app_users(id),
  user_email TEXT,
  action TEXT NOT NULL,
  module TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  old_value_json TEXT,
  new_value_json TEXT,
  justification TEXT,
  result TEXT NOT NULL,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backup_registry (
  id BIGSERIAL PRIMARY KEY,
  requested_by BIGINT REFERENCES app_users(id),
  provider TEXT NOT NULL,
  storage_path TEXT,
  checksum TEXT,
  status TEXT NOT NULL,
  details_json TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  restored_at TIMESTAMPTZ,
  restore_result TEXT
);

CREATE INDEX IF NOT EXISTS app_users_profile_idx ON app_users(profile, active);
CREATE INDEX IF NOT EXISTS user_scopes_user_idx ON user_scopes(user_id, scope_type);
CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id, expires_at);
CREATE INDEX IF NOT EXISTS auth_sessions_validation_idx
  ON auth_sessions(token_hash, revoked_at, expires_at, last_seen_at);
CREATE INDEX IF NOT EXISTS login_attempts_identity_idx ON login_attempts(email, ip_address, created_at);
CREATE INDEX IF NOT EXISTS audit_log_created_idx ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS audit_log_user_idx ON audit_log(user_id, created_at);
