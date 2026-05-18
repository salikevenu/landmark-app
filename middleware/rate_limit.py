import time
from functools import wraps
from flask import request, jsonify

requests_log = {}

RATE_LIMIT = 60
WINDOW = 60


def rate_limit(f):

    @wraps(f)
    def decorated_function(*args, **kwargs):

        ip = request.remote_addr
        current_time = time.time()

        if ip not in requests_log:
            requests_log[ip] = []

        requests_log[ip] = [
            t for t in requests_log[ip]
            if current_time - t < WINDOW
        ]

        if len(requests_log[ip]) >= RATE_LIMIT:
            return jsonify({
                "error": "Too many requests"
            }), 429

        requests_log[ip].append(current_time)

        return f(*args, **kwargs)

    return decorated_function
