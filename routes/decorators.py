# routes/decorators.py
from functools import wraps
from flask import redirect, url_for, flash
from flask_jwt_extended import get_jwt_identity, jwt_required
from datetime import datetime
from sqlalchemy import text
from database.init_db import get_db_connection

def requires_active_plan(*allowed_roles):
    def decorator(f):
        @wraps(f)
        @jwt_required()
        def wrapped(*args, **kwargs):
            user_id = get_jwt_identity()
            db = get_db_connection()
            user = db.execute(
                text("SELECT role, subscription_expiry FROM users WHERE id = :uid"),
                {"uid": user_id}
            ).fetchone()

            if not user:
                flash("User not found.", "error")
                return redirect(url_for('auth.login'))

            # ----- Expiry check -----
            current_role = user._mapping["role"]
            expiry_str = user._mapping["subscription_expiry"]
            if expiry_str:
                try:
                    expiry = datetime.fromisoformat(expiry_str)
                    if datetime.utcnow() > expiry:
                        # Demote to free
                        db.execute(
                            text("UPDATE users SET role = 'free', plan = NULL, subscription_expiry = NULL, business_limit = 0 WHERE id = :uid"),
                            {"uid": user_id}
                        )
                        db.commit()
                        flash("Your subscription has expired. You are now a free user.", "warning")
                        return redirect(url_for('user.pricing'))
                except (ValueError, TypeError):
                    pass

            # ----- Role check -----
            if current_role not in allowed_roles:
                flash("Please upgrade your plan to access this feature.", "warning")
                return redirect(url_for('user.pricing'))

            return f(*args, **kwargs)
        return wrapped
    return decorator