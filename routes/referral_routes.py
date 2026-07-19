from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text
from database.init_db import get_db_connection
from services.referral_service import get_referral_info

referral_bp = Blueprint("referral", __name__)

# =========================
# REFERRAL LEADERBOARD (public)
# =========================
@referral_bp.route("/api/referral-leaderboard", methods=["GET"])
def referral_leaderboard():
    try:
        conn = get_db_connection()
        rows = conn.execute(text("""
            SELECT users.name,
                   COUNT(referral_transactions.id) AS total_referrals
            FROM referral_transactions
            JOIN users ON users.id = referral_transactions.referrer_id
            GROUP BY referral_transactions.referrer_id, users.name
            ORDER BY total_referrals DESC
            LIMIT 20
        """)).fetchall()

        return jsonify([dict(r._mapping) for r in rows])

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================
# NEARBY BUSINESS LEADS (public)
# =========================
@referral_bp.route("/api/nearby-leads", methods=["GET"])
def nearby_leads():
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)

    if lat is None or lng is None:
        return jsonify({"error": "lat and lng required"}), 400

    lat_grid = int(lat * 100)
    lng_grid = int(lng * 100)

    try:
        conn = get_db_connection()
        rows = conn.execute(text("""
            SELECT *
            FROM business_leads
            WHERE lat_grid BETWEEN :lat_min AND :lat_max
            AND lng_grid BETWEEN :lng_min AND :lng_max
            LIMIT 50
        """), {
            "lat_min": lat_grid - 1,
            "lat_max": lat_grid + 1,
            "lng_min": lng_grid - 1,
            "lng_max": lng_grid + 1
        }).fetchall()

        return jsonify([dict(r._mapping) for r in rows])

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================
# INVITE BUSINESS (authenticated)
# =========================
@referral_bp.route("/api/invite-business", methods=["POST"])
@jwt_required()
def invite_business():
    data = request.json

    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    business_name = data.get("business_name")
    phone = data.get("phone")
    category = data.get("category")
    city = data.get("city")
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if not all([business_name, phone, category, city, latitude, longitude]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        user_id = int(get_jwt_identity())
        conn = get_db_connection()

        # Duplicate check
        existing = conn.execute(
            text("SELECT id FROM business_leads WHERE phone = :phone"),
            {"phone": phone}
        ).fetchone()
        if existing:
            return jsonify({"error": "Business already invited"}), 409

        conn.execute(text("""
            INSERT INTO business_leads
            (business_name, phone, category, city, latitude, longitude, lat_grid, lng_grid, invited_by)
            VALUES (:bname, :phone, :cat, :city, :lat, :lng, :lat_grid, :lng_grid, :invited_by)
        """), {
            "bname": business_name,
            "phone": phone,
            "cat": category,
            "city": city,
            "lat": latitude,
            "lng": longitude,
            "lat_grid": int(latitude * 100),
            "lng_grid": int(longitude * 100),
            "invited_by": user_id
        })
        conn.commit()

        return jsonify({
            "success": True,
            "message": "Business invited successfully"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================
# REFERRAL INFO (public)
# =========================
@referral_bp.route("/api/referral/info")
def referral_info():
    user_id = request.args.get("user_id")
    data = get_referral_info(user_id)
    if not data:
        return jsonify({"error": "User not found"}), 404
    return jsonify(data)