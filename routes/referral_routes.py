from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text
from database.init_db import get_db_connection
from services.referral_service import get_referral_info
import logging

referral_bp = Blueprint("referral", __name__)
logger = logging.getLogger(__name__)

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

    except Exception:
        logger.exception("referral_leaderboard error")
        return jsonify({"error": "Something went wrong. Please try again."}), 500


# =========================
# NEARBY BUSINESS LEADS (public)
# =========================
@referral_bp.route("/api/nearby-leads", methods=["GET"])
def nearby_leads():
    """Disabled: unauthenticated SELECT * leaked lead phone numbers."""
    return jsonify({"success": False, "error": "This endpoint is disabled"}), 410


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

    except Exception:
        logger.exception("invite_business error")
        return jsonify({"error": "Something went wrong. Please try again."}), 500


# =========================
# REFERRAL INFO (authenticated — own wallet/code only)
# =========================
@referral_bp.route("/api/referral/info")
@jwt_required()
def referral_info():
    user_id = get_jwt_identity()
    data = get_referral_info(user_id)
    if not data:
        return jsonify({"error": "User not found"}), 404
    return jsonify(data)