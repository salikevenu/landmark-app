from functools import wraps
from flask import jsonify
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity

from services.authz import db_user_is_admin


def require_role(allowed_roles):
    """Restrict by JWT role. Admin also requires users.role = admin in the DB."""
    def decorator(f):
        @wraps(f)
        @jwt_required()
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            user_role = claims.get("role")
            if user_role not in allowed_roles:
                return jsonify({"error": "Access denied. Insufficient permissions."}), 403
            if user_role == "admin" or "admin" in allowed_roles:
                if user_role == "admin" and not db_user_is_admin(get_jwt_identity()):
                    return jsonify({"error": "Access denied. Insufficient permissions."}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator
