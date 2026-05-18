from functools import wraps
from flask import jsonify
from flask_jwt_extended import jwt_required, get_jwt     # remove unused get_jwt_identity

# ============================================================
# Role‑based access decorator (uses JWT claims)
# ============================================================
def require_role(allowed_roles):
    """
    Decorator to restrict access to users with specific role(s).
    Must be used AFTER @jwt_required.
    Example:
        @jwt_required()
        @require_role(['admin', 'moderator'])
        def protected_route(): ...
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            user_role = claims.get("role")
            if user_role not in allowed_roles:
                return jsonify({"error": "Access denied. Insufficient permissions."}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator