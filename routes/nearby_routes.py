from flask import Blueprint, request, jsonify
from services.nearby_service import find_nearby_listings
from app import limiter
from services.geo_service import find_nearby_friends
from flask_jwt_extended import jwt_required

nearby_bp = Blueprint("nearby", __name__)

# ✅ SAFE DECORATOR (IMPORTANT)
def safe_limit(limit_value):
    def decorator(f):
        if limiter:
            return limiter.limit(limit_value)(f)
        return f
    return decorator


@nearby_bp.route("/nearby", methods=["GET"])
@jwt_required()
@safe_limit("60 per minute")   # ✅ FIXED
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
        user_lat,
        user_lng,
        category,
        listing_type,
        sort_type,
        radius
    )

    return jsonify(results)


@nearby_bp.route("/api/nearby-friends", methods=["GET"])
@jwt_required()
def nearby_friends():

    user_lat = request.args.get("lat", type=float)
    user_lng = request.args.get("lng", type=float)
    radius = request.args.get("radius", default=30, type=float)

    if user_lat is None or user_lng is None:
        return {"error": "User location required"}, 400

    friends = find_nearby_friends(user_lat, user_lng, radius)

    return jsonify({"friends": friends})