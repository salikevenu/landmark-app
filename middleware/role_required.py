from functools import wraps
from flask import jsonify
from flask_jwt_extended import get_jwt

def require_role(allowed_roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            role = claims.get("role")

            if role not in allowed_roles:
                return jsonify({"error": "Access denied"}), 403

            return f(*args, **kwargs)
        return wrapper
    return decorator