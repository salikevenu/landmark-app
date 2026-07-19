from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import text
from database.init_db import get_db_connection
from middleware.rate_limit import rate_limit

heatmap_bp = Blueprint("heatmap", __name__)


@heatmap_bp.route("/api/heatmap", methods=["GET"])
@jwt_required()
@rate_limit
def heatmap():
    conn = get_db_connection()
    category = request.args.get("category")
    limit = int(request.args.get("limit", 1000))

    query = text("""
        SELECT latitude, longitude
        FROM listings
        WHERE is_active = 1
        AND (:category IS NULL OR category = :cat)
        LIMIT :limit
    """)

    rows = conn.execute(query, {
        "category": category,
        "cat": category,
        "limit": limit
    }).fetchall()

    return jsonify([
        {"lat": r._mapping["latitude"], "lng": r._mapping["longitude"]}
        for r in rows
    ])