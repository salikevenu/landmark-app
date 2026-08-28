from functools import wraps
from flask import jsonify
from flask_jwt_extended import get_jwt, get_jwt_identity

from services.authz import db_user_is_admin


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        if not db_user_is_admin(get_jwt_identity()):
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated_function
