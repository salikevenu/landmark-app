from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from database.init_db import get_db               # <-- use shared helper
from middleware.rate_limit import rate_limit

heatmap_bp = Blueprint("heatmap", __name__)


@heatmap_bp.route("/api/heatmap", methods=["GET"])
@jwt_required()
@rate_limit
def heatmap():
    conn = get_db()

    category = request.args.get("category")
    limit = int(request.args.get("limit", 1000))

    query = """
        SELECT latitude, longitude
        FROM listings
        WHERE is_active = 1
    """
    params = []

    if category:
        query += " AND category = ?"
        params.append(category)

    query += " LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()

    return jsonify([
        {"lat": r["latitude"], "lng": r["longitude"]}
        for r in rows
    ])