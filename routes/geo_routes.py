from flask import Blueprint, request, jsonify
from utils.geo_utils import calculate_distance
from flask_jwt_extended import jwt_required   # <-- import

geo_bp = Blueprint("geo", __name__)

def parse_float(value, name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}")

@geo_bp.route("/api/distance", methods=["GET"])
@jwt_required()   # <-- now requires a valid JWT
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