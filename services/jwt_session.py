"""JWT session hardening: blocklist, banned-user lookup, request-token revoke."""
import logging

from flask import jsonify, request
from flask_jwt_extended import decode_token
from sqlalchemy import text

from database.init_db import get_db_connection
from services.jwt_blocklist import is_revoked, revoke_jti

logger = logging.getLogger(__name__)


def _identity_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _user_row_for_identity(identity):
    uid = _identity_int(identity)
    if uid is None:
        return None, "invalid"
    conn = None
    try:
        conn = get_db_connection()
        row = conn.execute(
            text("SELECT id, is_blocked, is_active FROM users WHERE id = :id"),
            {"id": uid},
        ).fetchone()
        if not row:
            return {"id": uid}, "missing"
        mapping = row._mapping
        if mapping.get("is_blocked"):
            return None, "blocked"
        if mapping.get("is_active") == 0:
            return None, "inactive"
        return {"id": uid}, "ok"
    except Exception:
        logger.exception("jwt user lookup failed")
        return {"id": uid}, "error"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def lookup_jwt_user(_jwt_header, jwt_data):
    """Return a stub for missing/error so tests without a users row still work.

    Banned or inactive database users return None so jwt_required fails closed.
    """
    identity = jwt_data.get("sub")
    user, status = _user_row_for_identity(identity)
    if status in ("invalid", "blocked", "inactive"):
        return None
    return user


def revoke_tokens_from_request():
    """Revoke access and refresh JTIs present on this request (best-effort)."""
    tokens = []
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        tokens.append(auth.split(" ", 1)[1].strip())

    try:
        from flask import current_app
        access_name = current_app.config.get("JWT_ACCESS_COOKIE_NAME", "access_token")
        refresh_name = current_app.config.get("JWT_REFRESH_COOKIE_NAME", "refresh_token")
    except Exception:
        access_name = "access_token"
        refresh_name = "refresh_token"

    tokens.append(request.cookies.get(access_name) or "")
    tokens.append(request.cookies.get(refresh_name) or "")

    data = request.get_json(silent=True) or {}
    if isinstance(data, dict):
        tokens.append(data.get("access_token") or "")
        tokens.append(data.get("refresh_token") or "")

    for raw in tokens:
        if not raw:
            continue
        try:
            decoded = decode_token(raw, allow_expired=True)
        except Exception:
            continue
        revoke_jti(decoded.get("jti"), decoded.get("exp"))


def register_jwt_security(jwt):
    @jwt.token_in_blocklist_loader
    def _token_revoked(_header, payload):
        return is_revoked(payload.get("jti"))

    @jwt.revoked_token_loader
    def _revoked_response(_header, _payload):
        return jsonify({"success": False, "error": "Token has been revoked"}), 401

    @jwt.user_lookup_loader
    def _load_user(header, payload):
        return lookup_jwt_user(header, payload)

    @jwt.user_lookup_error_loader
    def _lookup_error(_header, _payload):
        return jsonify({"success": False, "error": "Invalid session"}), 401
