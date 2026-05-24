from functools import wraps
from flask import jsonify
from flask_jwt_extended import get_jwt, jwt_required

def admin_required(f):
    @wraps(f)
    @jwt_required()  
    def decorated_function(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated_function