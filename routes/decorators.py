# routes/decorators.py
from functools import wraps
from flask import redirect, url_for, flash
from flask_jwt_extended import get_jwt_identity, jwt_required
from datetime import datetime
from sqlalchemy import text
from database.init_db import get_db_connection
from services.subscription_access import is_subscription_active


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

def requires_active_plan(*allowed_roles):
    def decorator(f):
        @wraps(f)
        @jwt_required()
        def wrapped(*args, **kwargs):
            user_id = get_jwt_identity()
            db = get_db_connection()
            user = db.execute(
                text("SELECT role, plan, subscription_expiry FROM users WHERE id = :uid"),
                {"uid": user_id}
            ).fetchone()

            if not user:
                flash("User not found.", "error")
                return redirect(url_for('auth.login'))

            user_dict = dict(user._mapping)
            current_role = user_dict.get("role")
            expiry_str = user_dict.get("subscription_expiry")
            if expiry_str:
                try:
                    expiry = datetime.strptime(str(expiry_str)[:10], "%Y-%m-%d").date()
                    if datetime.utcnow().date() > expiry:
                        db.execute(
                            text("UPDATE users SET role = 'free', plan = 'free', subscription_expiry = NULL, business_limit = 0 WHERE id = :uid"),
                            {"uid": user_id}
                        )
                        db.commit()
                        flash("Your subscription has expired. You are now a free user.", "warning")
                        if allowed_roles == ("service_provider",):
                            return redirect("/api/user/pricing?page_type=service")
                        if "business_basic" in allowed_roles or "business_premium" in allowed_roles:
                            return redirect("/api/user/pricing?page_type=business")
                        return redirect("/pricing")
                except (ValueError, TypeError):
                    pass
                user_dict = dict(user._mapping)

            if not is_subscription_active(user_dict) or current_role not in allowed_roles:
                flash("Please upgrade your plan to access this feature.", "warning")
                if allowed_roles == ("service_provider",):
                    return redirect("/api/user/pricing?page_type=service")
                if "business_basic" in allowed_roles or "business_premium" in allowed_roles:
                    return redirect("/api/user/pricing?page_type=business")
                return redirect(url_for('user.pricing'))

            return f(*args, **kwargs)
        return wrapped
    return decorator