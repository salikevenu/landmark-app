from flask import Blueprint, request, jsonify
import math
from database.init_db import get_db
from utils.geo_utils import calculate_distance

geo_bp = Blueprint("geo", __name__)

GRID_SIZE = 0.05
MAX_SEARCH_RADIUS = 30
DEFAULT_RESULTS_LIMIT = 50
EARTH_RADIUS_KM = 6371


# --- Helper (could be moved to utils if needed) ---
def parse_float(value, name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}")


# --- Route: distance calculation ---
@geo_bp.route("/api/distance", methods=["GET"])
def get_distance():
    try:
        lat1 = parse_float(request.args.get("lat1"), "lat1")
        lon1 = parse_float(request.args.get("lon1"), "lon1")
        lat2 = parse_float(request.args.get("lat2"), "lat2")
        lon2 = parse_float(request.args.get("lon2"), "lon2")

        if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90):
            return jsonify({"error": "Invalid latitude"}), 400
        if not (-180 <= lon1 <= 180 and -180 <= lon2 <= 180):
            return jsonify({"error": "Invalid longitude"}), 400

        distance = calculate_distance(lat1, lon1, lat2, lon2)
        return jsonify({"distance_km": round(distance, 2)})

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Internal server error"}), 500


# --- Service: find nearby friends (can be moved to services/ directory) ---
def find_nearby_friends(user_lat, user_lng, radius):
    GRID_SIZE = 0.1
    lat_grid = int(user_lat / GRID_SIZE)
    lng_grid = int(user_lng / GRID_SIZE)
    grid_range = int(radius / 11) + 1

    conn = get_db()
    rows = conn.execute("""
        SELECT phone, name, latitude, longitude
        FROM users
        WHERE is_active = 1
          AND latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND lat_grid BETWEEN ? AND ?
          AND lng_grid BETWEEN ? AND ?
    """, (
        lat_grid - grid_range,
        lat_grid + grid_range,
        lng_grid - grid_range,
        lng_grid + grid_range
    )).fetchall()

    friends = []
    for r in rows:
        distance = calculate_distance(user_lat, user_lng, r["latitude"], r["longitude"])
        if distance <= radius:
            friends.append({
                "phone": r["phone"],
                "name": r["name"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "distance_km": round(distance, 2)
            })

    friends.sort(key=lambda x: x["distance_km"])
    return friends