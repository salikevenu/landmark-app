from functools import wraps
from flask import request, jsonify
import sqlite3

DATABASE = "database.db"


def fraud_check(f):

    @wraps(f)
    def decorated_function(*args, **kwargs):

        ip = request.remote_addr
        device = request.headers.get("User-Agent")

        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM users WHERE ip_address=?",
            (ip,)
        )

        count = cursor.fetchone()[0]

        conn.close()

        if count > 5:
            return jsonify({
                "error": "Suspicious activity detected"
            }), 403

        return f(*args, **kwargs)

    return decorated_function
