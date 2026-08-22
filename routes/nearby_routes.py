from flask import Blueprint, request, jsonify
from services.nearby_service import find_nearby_listings
from extensions import limiter
from flask_jwt_extended import jwt_required
from sqlalchemy import text
from database.init_db import get_db_connection
import logging

nearby_bp = Blueprint("nearby", __name__)
logger = logging.getLogger(__name__)


def safe_limit(limit_value):
    def decorator(f):
        if limiter:
            return limiter.limit(limit_value)(f)
        return f
    return decorator


def _row_to_business(row):
    m = row._mapping
    return {
        "id": m["id"],
        "business_name": m["business_name"] or "Business",
        "category": m["category"] or "",
        "rating": float(m["rating"] or 0),
        "latitude": float(m["latitude"]),
        "longitude": float(m["longitude"]),
        "phone": m["phone"] or "",
        "whatsapp": m["whatsapp"] or m["phone"] or "",
    }


def _fetch_businesses(search=None, limit=500):
    conn = get_db_connection()
    clauses = [
        "is_active = 1",
        "(status = 'approved')",
        "latitude IS NOT NULL",
        "longitude IS NOT NULL",
        "latitude <> 0",
        "longitude <> 0",
    ]
    params = {"limit": limit}
    if search:
        clauses.append(
            "(LOWER(COALESCE(business_name, '')) LIKE LOWER(:q) OR LOWER(COALESCE(category, '')) LIKE LOWER(:q))"
        )
        params["q"] = f"%{search}%"
    where_sql = " AND ".join(clauses)
    rows = conn.execute(
        text(f"""
            SELECT
                id,
                business_name,
                category,
                COALESCE(rating, 0) AS rating,
                latitude,
                longitude,
                user_phone AS phone,
                whatsapp
            FROM listings
            WHERE {where_sql}
            ORDER BY is_sponsored DESC, is_premium DESC, COALESCE(rating, 0) DESC
            LIMIT :limit
        """),
        params,
    ).fetchall()
    return [_row_to_business(r) for r in rows]


@nearby_bp.route("/nearby", methods=["GET"])
@jwt_required()
@safe_limit("60 per minute")
def nearby_listings():
    user_lat = request.args.get("lat", type=float)
    user_lng = request.args.get("lng", type=float)
    category = request.args.get("category")
    listing_type = request.args.get("type")
    sort_type = request.args.get("sort", "smart")
    radius = request.args.get("radius", default=30, type=float)

    if user_lat is None or user_lng is None:
        return {"error": "User location required"}, 400

    results = find_nearby_listings(
        user_lat, user_lng, category, listing_type, sort_type, radius
    )
    return jsonify(results)


@nearby_bp.route("/api/nearby-friends", methods=["GET"])
@jwt_required()
def nearby_friends():
    """Disabled: listed every nearby user's phone and coordinates."""
    return jsonify({"success": False, "error": "This endpoint is disabled"}), 410


@nearby_bp.route("/businesses", methods=["GET"])
@jwt_required(optional=True)
def list_map_businesses():
    """All active listings with coordinates for the interactive map."""
    try:
        businesses = _fetch_businesses()
        return jsonify({"success": True, "businesses": businesses, "count": len(businesses)})
    except Exception as e:
        logger.exception("list_map_businesses error")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500


@nearby_bp.route("/businesses/<int:listing_id>", methods=["GET"])
@jwt_required(optional=True)
def get_map_business(listing_id):
    """Single listing for View on Map / flyTo."""
    try:
        conn = get_db_connection()
        row = conn.execute(
            text("""
                SELECT
                    id,
                    business_name,
                    category,
                    COALESCE(rating, 0) AS rating,
                    latitude,
                    longitude,
                    user_phone AS phone,
                    whatsapp
                FROM listings
                WHERE id = :id AND is_active = 1
                  AND status = 'approved'
            """),
            {"id": listing_id},
        ).fetchone()
        if not row:
            return jsonify({"success": False, "error": "Business not found"}), 404
        return jsonify({"success": True, "business": _row_to_business(row)})
    except Exception as e:
        logger.exception("get_map_business error")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500


@nearby_bp.route("/search", methods=["GET"])
@jwt_required(optional=True)
def search_map_businesses():
    """Filter listings by business name or category."""
    q = (request.args.get("q") or request.args.get("search") or "").strip()
    category = (request.args.get("category") or "").strip()
    try:
        search = q or category
        businesses = _fetch_businesses(search=search or None)
        if category and q:
            cat = category.lower()
            businesses = [
                b for b in businesses
                if cat in (b.get("category") or "").lower()
            ]
        return jsonify({
            "success": True,
            "businesses": businesses,
            "count": len(businesses),
            "q": q,
            "category": category,
        })
    except Exception as e:
        logger.exception("search_map_businesses error")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
