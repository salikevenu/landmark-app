from flask import Blueprint, request, jsonify, render_template
from flask_jwt_extended import jwt_required, get_jwt_identity
from database.init_db import get_db_connection
from sqlalchemy import text

promotions_bp = Blueprint(
    "promotions",
    __name__,
    url_prefix="/promotions"
)

@promotions_bp.route("/")
@jwt_required()
def promotions():
    return render_template("promotions/index.html")


@promotions_bp.route("/api/promotions/onboard", methods=["POST"])
@jwt_required()
def create_promotion():
    user_id = get_jwt_identity()
    data = request.get_json() or {}

    plan = data.get("plan")
    listing_url = data.get("listing_url")
    headline = data.get("headline")

    conn = get_db_connection()
    conn.execute(
        text("""
            INSERT INTO sponsored_ads
            (listing_id, plan, start_date, end_date, is_active)
            VALUES
            (:lid, :plan, NOW(), NOW() + INTERVAL '1 month', TRUE)
        """),
        {"lid": 1, "plan": plan},
    )
    conn.commit()

    return jsonify({
        "success": True,
        "message": "Promotion started successfully!"
    })