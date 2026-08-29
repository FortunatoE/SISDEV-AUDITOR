"""Authentication, role permissions, scopes and audit logging.

The web layer owns cookies and HTTP responses; this module owns durable
security state and can therefore be tested without Flask.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from werkzeug.security import check_password_hash, generate_password_hash

from .database import connect
from .normalization import key, text


PROFILES = ("ADMINISTRADOR", "GESTOR", "AUDITOR", "OPERADOR", "CONSULTA")

SESSION_IDLE_MINUTES = 30
SESSION_ABSOLUTE_HOURS = 8


class AuditIdentityError(RuntimeError):
    """Raised when an authenticated audit actor cannot be resolved safely."""


PROFILE_PERMISSIONS: dict[str, set[str]] = {
    "ADMINISTRADOR": {"*"},
    "GESTOR": {
        "view", "export", "import", "reconcile", "manage_mappings",
        "approve_mappings", "treat_pending", "view_audit", "manage_backup",
    },
    "AUDITOR": {"view", "export", "suggest_mappings", "treat_pending", "view_audit"},
    "OPERADOR": {"view", "treat_pending"},
    "CONSULTA": {"view", "export"},
}

PROFILE_MODULES: dict[str, set[str]] = {
    "ADMINISTRADOR": {"*"},
    "GESTOR": {"dashboard", "pending", "regularization", "analysis", "invoices", "recipes", "movements", "stocks", "reports", "history"},
    "AUDITOR": {"dashboard", "pending", "regularization", "analysis", "recipes", "materials", "lots", "rules", "reports", "history", "logs"},
    "OPERADOR": {"dashboard", "pending", "regularization", "invoices", "recipes", "stocks"},
    "CONSULTA": {"dashboard", "pending", "analysis", "invoices", "recipes", "movements", "stocks", "reports"},
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    """Normalize SQLite text and PostgreSQL timestamps for session checks."""

    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_email(value: Any) -> str:
    return text(value).casefold()


def hash_password(password: str) -> str:
    if len(password or "") < 10:
        raise ValueError("A senha deve possuir ao menos 10 caracteres.")
    return generate_password_hash(password, method="scrypt")


def create_user(
    email: str,
    display_name: str,
    password: str,
    profile: str,
    *,
    scopes: Iterable[tuple[str, str]] = (),
    active: bool = True,
) -> dict[str, Any]:
    normalized_email = normalize_email(email)
    normalized_profile = key(profile)
    if (
        not normalized_email
        or len(normalized_email) > 254
        or any(character.isspace() for character in normalized_email)
    ):
        raise ValueError("Informe um usuário ou e-mail válido.")
    if not text(display_name):
        raise ValueError("Informe o nome do usuário.")
    if normalized_profile not in PROFILES:
        raise ValueError("Perfil inválido.")
    password_hash = hash_password(password)
    connection = connect()
    try:
        row = connection.execute(
            """INSERT INTO app_users(email,display_name,password_hash,profile,active)
               VALUES (?,?,?,?,?) RETURNING *""",
            (normalized_email, text(display_name), password_hash, normalized_profile, int(active)),
        ).fetchone()
        for scope_type, scope_value in scopes:
            if key(scope_type) not in {"CENTER", "UNIT", "PROPERTY"} or not text(scope_value):
                continue
            connection.execute(
                "INSERT INTO user_scopes(user_id,scope_type,scope_value) VALUES (?,?,?) ON CONFLICT(user_id,scope_type,scope_value) DO NOTHING",
                (row["id"], key(scope_type), text(scope_value)),
            )
        connection.commit()
        return public_user(row, connection=connection)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_bootstrap_admin() -> bool:
    """Create the first administrator only from server-side environment values."""

    email = os.getenv("SISDEV_BOOTSTRAP_ADMIN_EMAIL", "").strip()
    password = os.getenv("SISDEV_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or not password:
        return False
    connection = connect()
    try:
        existing = connection.execute(
            "SELECT COUNT(*) n FROM app_users WHERE deleted_at IS NULL"
        ).fetchone()["n"]
    finally:
        connection.close()
    if existing:
        return False
    create_user(email, os.getenv("SISDEV_BOOTSTRAP_ADMIN_NAME", "Administrador SISDEV"), password, "ADMINISTRADOR")
    return True


def permissions_for(profile: str) -> list[str]:
    return sorted(PROFILE_PERMISSIONS.get(key(profile), set()))


def modules_for(profile: str) -> list[str]:
    return sorted(PROFILE_MODULES.get(key(profile), set()))


def has_permission(user: dict[str, Any] | None, permission: str) -> bool:
    if not user or not user.get("active"):
        return False
    granted = PROFILE_PERMISSIONS.get(key(user.get("profile")), set())
    return "*" in granted or permission in granted


def public_user(row: Any, *, connection: Any | None = None) -> dict[str, Any]:
    own_connection = connection is None
    connection = connection or connect()
    try:
        scopes = [dict(item) for item in connection.execute(
            "SELECT scope_type,scope_value FROM user_scopes WHERE user_id=? ORDER BY scope_type,scope_value",
            (row["id"],),
        )]
        profile = key(row["profile"])
        return {
            "id": int(row["id"]),
            "email": row["email"],
            "display_name": row["display_name"],
            "profile": profile,
            "active": bool(row["active"]),
            "permissions": permissions_for(profile),
            "modules": modules_for(profile),
            "scopes": scopes,
        }
    finally:
        if own_connection:
            connection.close()


def _recent_failures(connection: Any, email: str, ip_address: str, minutes: int = 15) -> int:
    instant = _utcnow() - timedelta(minutes=minutes)
    since = instant if connection.postgres else instant.strftime("%Y-%m-%d %H:%M:%S")
    row = connection.execute(
        """SELECT COUNT(*) n FROM login_attempts
           WHERE success=0 AND created_at>=? AND (email=? OR ip_address=?)""",
        (since, email, ip_address),
    ).fetchone()
    return int(row["n"] or 0)


def authenticate(email: str, password: str, ip_address: str = "") -> tuple[dict[str, Any] | None, str]:
    normalized_email = normalize_email(email)
    connection = connect()
    try:
        if _recent_failures(connection, normalized_email, ip_address) >= 5:
            return None, "Muitas tentativas. Aguarde 15 minutos e tente novamente."
        row = connection.execute(
            "SELECT * FROM app_users WHERE email=? AND deleted_at IS NULL",
            (normalized_email,),
        ).fetchone()
        valid = bool(row and row["active"] and check_password_hash(row["password_hash"], password or ""))
        connection.execute(
            "INSERT INTO login_attempts(email,ip_address,success) VALUES (?,?,?)",
            (normalized_email, ip_address, int(valid)),
        )
        if not valid:
            if row:
                connection.execute(
                    "UPDATE app_users SET failed_login_count=failed_login_count+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],),
                )
            connection.commit()
            return None, "Usuário ou senha inválidos."
        connection.execute(
            "UPDATE app_users SET failed_login_count=0,locked_until=NULL,last_login_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (row["id"],),
        )
        connection.commit()
        return public_user(row, connection=connection), ""
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_session(
    user_id: int,
    ip_address: str = "",
    user_agent: str = "",
    hours: int = SESSION_ABSOLUTE_HOURS,
) -> tuple[str, str]:
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    expires = _utcnow() + timedelta(hours=max(1, min(hours, 24)))
    connection = connect()
    try:
        connection.execute(
            """INSERT INTO auth_sessions(user_id,token_hash,csrf_hash,ip_address,user_agent,expires_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, _hash_token(token), _hash_token(csrf), ip_address, user_agent[:500], expires.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()
    return token, csrf


def validate_session(
    token: str,
    csrf: str | None = None,
    *,
    user_agent: str | None = None,
    touch: bool = True,
) -> dict[str, Any] | None:
    if not token:
        return None
    connection = connect()
    try:
        row = connection.execute(
            """SELECT u.id AS id,s.id AS session_id,s.user_id,s.token_hash,s.csrf_hash,
                      s.ip_address,s.user_agent,s.expires_at,s.last_seen_at,s.revoked_at,
                      u.email,u.display_name,u.profile,u.active,u.password_hash
               FROM auth_sessions s JOIN app_users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.revoked_at IS NULL""",
            (_hash_token(token),),
        ).fetchone()
        if row is None or not row["active"]:
            return None
        now = _utcnow()
        expires_at = _as_utc(row["expires_at"])
        last_seen_at = _as_utc(row["last_seen_at"])
        idle_cutoff = now - timedelta(minutes=SESSION_IDLE_MINUTES)
        if expires_at is None or expires_at <= now or last_seen_at is None or last_seen_at <= idle_cutoff:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE id=? AND revoked_at IS NULL",
                (row["session_id"],),
            )
            connection.commit()
            return None
        if user_agent is not None and not hmac.compare_digest(
            text(row["user_agent"]), text(user_agent)[:500]
        ):
            return None
        if csrf is not None and not hmac.compare_digest(row["csrf_hash"], _hash_token(csrf)):
            return None
        if touch:
            connection.execute(
                "UPDATE auth_sessions SET last_seen_at=CURRENT_TIMESTAMP WHERE id=?",
                (row["session_id"],),
            )
            connection.commit()
        return public_user(row, connection=connection)
    finally:
        connection.close()


def revoke_session(token: str) -> None:
    if not token:
        return
    connection = connect()
    try:
        connection.execute(
            "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE token_hash=? AND revoked_at IS NULL",
            (_hash_token(token),),
        )
        connection.commit()
    finally:
        connection.close()


def record_audit(
    *,
    user: dict[str, Any] | None,
    action: str,
    module: str,
    result: str,
    entity_type: str = "",
    entity_id: Any = None,
    old_value: Any = None,
    new_value: Any = None,
    justification: str = "",
    ip_address: str = "",
) -> None:
    def encoded(value: Any) -> str | None:
        return None if value is None else json.dumps(value, ensure_ascii=False, default=str)

    connection = connect()
    try:
        resolved_user_id = None
        resolved_user_email = None
        if user:
            requested_id = user.get("id")
            actor = None
            if requested_id is not None:
                try:
                    actor = connection.execute(
                        "SELECT id,email FROM app_users WHERE id=?", (int(requested_id),)
                    ).fetchone()
                except (TypeError, ValueError):
                    actor = None
            requested_email = normalize_email(user.get("email"))
            if actor is None and requested_email:
                actor = connection.execute(
                    "SELECT id,email FROM app_users WHERE email=?", (requested_email,)
                ).fetchone()
            if actor is None:
                raise AuditIdentityError(
                    "O usuário autenticado não existe em app_users; a auditoria foi rejeitada."
                )
            resolved_user_id = int(actor["id"])
            resolved_user_email = actor["email"]
        connection.execute(
            """INSERT INTO audit_log(
                 user_id,user_email,action,module,entity_type,entity_id,old_value_json,
                 new_value_json,justification,result,ip_address
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                resolved_user_id,
                resolved_user_email,
                key(action), key(module), text(entity_type), text(entity_id),
                encoded(old_value), encoded(new_value), text(justification), key(result), ip_address,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def scope_values(user: dict[str, Any], scope_type: str) -> list[str]:
    return [
        text(item.get("scope_value"))
        for item in user.get("scopes", [])
        if key(item.get("scope_type")) == key(scope_type) and text(item.get("scope_value"))
    ]
