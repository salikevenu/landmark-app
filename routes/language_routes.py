# language_routes.py
from flask import Blueprint, request, jsonify, make_response
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text
from database.init_db import get_db

lang_bp = Blueprint("language", __name__)

@lang_bp.route("/set-language", methods=["POST"])
@jwt_required(optional=True)
def set_language():
    data = request.json
    lang = data.get("lang")
    if not lang:
        return jsonify({"error": "Language required"}), 400

    # Persist to DB for logged-in users
    user_id = get_jwt_identity()
    if user_id:
        conn = get_db()
        conn.execute(text("UPDATE users SET language = :lang WHERE id = :uid"), {"lang": lang, "uid": user_id})
        conn.commit()

    # Set a cookie so it persists even for anonymous users
    resp = make_response(jsonify({"message": "Language updated"}))
    resp.set_cookie(
        "lang",
        lang,
        max_age=60*60*24*365,
        path="/",
        samesite="Lax",
        httponly=True,
        secure=False
    )
    return resp