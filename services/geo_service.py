from flask import Blueprint, jsonify

geo_bp = Blueprint("geo", __name__)

# This blueprint is NOT registered. Live distance API is routes/geo_routes.py
# (JWT required). These handlers stay fail-closed so a future mount cannot
# expose unauthenticated distance or dump nearby user phones.


@geo_bp.route("/api/distance", methods=["GET"])
def get_distance():
    return jsonify({"success": False, "error": "This endpoint is disabled"}), 410


def find_nearby_friends(user_lat, user_lng, radius):
    """Disabled: selected nearby users' phone numbers and coordinates."""
    raise RuntimeError("find_nearby_friends is disabled")
