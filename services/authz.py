# services/authz.py
"""Authorization helpers that must not import the routes package."""
from sqlalchemy import text

from database.init_db import get_db_connection


def db_user_is_admin(identity):
    """True only if the JWT identity maps to users.role = 'admin'."""
    if identity is None:
        return False
    conn = get_db_connection()
    try:
        if str(identity).isdigit():
            row = conn.execute(
                text("SELECT role FROM users WHERE id = :id"),
                {"id": int(identity)},
            ).fetchone()
        else:
            row = conn.execute(
                text("SELECT role FROM users WHERE phone = :phone"),
                {"phone": identity},
            ).fetchone()
        role = (row._mapping.get("role") if row else None) or ""
        return role == "admin"
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
