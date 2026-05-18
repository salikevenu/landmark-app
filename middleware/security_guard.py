from flask import request, jsonify

def register_security_guard(app):

    @app.before_request
    def check_requests():
        # ✅ Allow form-data (file uploads)
        content_type = request.content_type or ""

        if request.method == "POST":
            if "application/json" in content_type:
                return  # allow JSON
            elif "multipart/form-data" in content_type:
                return  # allow file uploads
            elif "application/x-www-form-urlencoded" in content_type:
                return  # allow normal forms

            # ❌ Block only unknown types
            return jsonify({"error": "Unsupported Content-Type"}), 415