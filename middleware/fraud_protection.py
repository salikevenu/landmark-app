# middleware/fraud_check.py
from functools import wraps
from flask import request, jsonify
from sqlalchemy import text
from database.init_db import get_db

def fraud_check(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        ip = request.remote_addr
        # device = request.headers.get("User-Agent")   # unused, kept if needed later

        conn = get_db()
        result = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE ip_address = :ip"),
            {"ip": ip}
        )
        count = result.scalar()   # scalar() returns the first column of the first row
        # No need to close the connection; Flask's teardown handles it

        if count > 5:
            return jsonify({"error": "Suspicious activity detected"}), 403

        return f(*args, **kwargs)

    return decorated_function