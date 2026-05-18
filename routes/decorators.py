from functools import wraps
from flask import redirect, url_for, flash
from flask_jwt_extended import get_jwt_identity, jwt_required
from datetime import datetime
from database.init_db import get_db

def requires_active_plan(*allowed_roles):
    """
    Decorator that checks:
    - User is logged in.
    - Subscription hasn't expired (demotes to 'free' if so).
    - User's role is in allowed_roles.
    """
    def decorator(f):
        @wraps(f)
        @jwt_required()
        def wrapped(*args, **kwargs):
            user_id = get_jwt_identity()
            db = get_db()
            user = db.execute(
                "SELECT role, subscription_expiry FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()

            if not user:
                flash("User not found.", "error")
                return redirect(url_for('auth.login'))

            # ----- Expiry check -----
            current_role = user["role"]
            if user["subscription_expiry"]:
                try:
                    expiry = datetime.fromisoformat(user["subscription_expiry"])
                    if datetime.utcnow() > expiry:
                        # Demote to free
                        db.execute(
                            "UPDATE users SET role = 'free', plan = NULL, subscription_expiry = NULL, business_limit = 0 WHERE id = ?",
                            (user_id,)
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