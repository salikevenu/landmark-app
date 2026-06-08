from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token
from datetime import datetime, timedelta, date
from sqlalchemy import text
from database.init_db import get_db
from time import time
import razorpay
import os
import secrets
from routes.decorators import requires_active_plan

user_bp = Blueprint("user", __name__)

# ------------------------------------------------------------
# Razorpay client (uses environment variables from .env)
# ------------------------------------------------------------
razor_client = razorpay.Client(auth=(
    os.getenv("RAZORPAY_KEY_ID"),
    os.getenv("RAZORPAY_KEY_SECRET")
))

# ------------------------------------------------------------
# Helper: get user by ID
# ------------------------------------------------------------
def get_user_by_id(user_id):
    conn = get_db()
    user = conn.execute(
        text("SELECT name, phone, role, plan, referral_code, subscription_expiry FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    return dict(user._mapping) if user else None

# ------------------------------------------------------------
# Profile pages & API
# ------------------------------------------------------------
@user_bp.route("/profile")
def profile_page():
    return render_template("users/profile.html", role="", plan="")

@user_bp.route("/api/profile")
@jwt_required()
def api_profile():
    user_id = get_jwt_identity()
    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "name": user["name"] or user["phone"],
        "phone": user["phone"],
        "role": user["role"],
        "plan": user["plan"],
        "referral_code": user.get("referral_code", "")
    })

@user_bp.route("/api/profile/update", methods=["PUT"])
@jwt_required()
def update_profile():
    user_id = get_jwt_identity()
    data = request.json
    name = data.get("name")
    if not name:
        return jsonify({"error": "Name required"}), 400
    conn = get_db()
    conn.execute(
        text("UPDATE users SET name = :name WHERE id = :uid"),
        {"name": name, "uid": user_id}
    )
    conn.commit()
    return jsonify({"message": "Profile updated"}), 200

@user_bp.route("/logout", methods=["POST"])
def logout():
    return jsonify({"message": "Logged out"}), 200

# ------------------------------------------------------------
# PLAN DETAILS
# ------------------------------------------------------------
PLAN_DETAILS = {
    "service": {"amount": 49900, "role": "service_provider", "plan": "service"},
    "basic":   {"amount": 99900, "role": "business_basic",   "plan": "basic"},
    "premium": {"amount": 199900, "role": "business_premium", "plan": "premium"},
    "extra_business": {"amount": 24900, "role": None, "plan": "extra_business"}
}

# ------------------------------------------------------------
# VERIFY PAYMENT
# ------------------------------------------------------------
@user_bp.route("/verify-payment", methods=["POST"])
@jwt_required()
def verify_payment():
    user_id = get_jwt_identity()
    data = request.get_json()
    payment_id = data.get("razorpay_payment_id")
    order_id = data.get("razorpay_order_id")
    signature = data.get("razorpay_signature")
    plan_type = data.get("plan")

    if not all([payment_id, order_id, signature, plan_type]):
        return jsonify({"error": "Missing payment details"}), 400
    if plan_type not in PLAN_DETAILS:
        return jsonify({"error": "Invalid plan"}), 400

    plan_info = PLAN_DETAILS[plan_type]

    # Verify signature
    params = {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature
    }
    try:
        razor_client.utility.verify_payment_signature(params)
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"error": "Invalid payment signature"}), 400

    conn = get_db()

    # ----- Extra business purchase -----
    if plan_type == "extra_business":
        conn.execute(
            text("UPDATE users SET extra_businesses_purchased = extra_businesses_purchased + 1 WHERE id = :uid"),
            {"uid": user_id}
        )
        conn.commit()
        return jsonify({"message": "Extra business slot purchased successfully", "redirect": "/create-listing"})

    # ----- Normal plan upgrade -----
    expiry_date = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    if plan_type == "service":
        business_limit = 0
    elif plan_type == "basic":
        business_limit = 1
    elif plan_type == "premium":
        business_limit = 3
    else:
        business_limit = 0

    conn.execute(
        text("UPDATE users SET role = :role, plan = :plan, subscription_expiry = :expiry, business_limit = :blimit WHERE id = :uid"),
        {
            "role": plan_info["role"],
            "plan": plan_info["plan"],
            "expiry": expiry_date,
            "blimit": business_limit,
            "uid": user_id
        }
    )
    conn.commit()

    # Process referral commission
    from services.referral_commission import process_referral_commission
    process_referral_commission(user_id, plan_info["amount"] / 100)

    # Record transaction
    conn.execute(text("""
        INSERT INTO payment_transactions 
        (user_id, razorpay_order_id, razorpay_payment_id, amount, status) 
        VALUES (:uid, :order_id, :payment_id, :amount, :status)
    """), {
        "uid": user_id,
        "order_id": order_id,
        "payment_id": payment_id,
        "amount": plan_info["amount"],
        "status": "captured"
    })
    conn.commit()

    # Generate new JWT with updated role
    user = get_user_by_id(user_id)
    user_phone = user["phone"] if user else ""
    new_token = create_access_token(
        identity=str(user_id),
        additional_claims={"role": plan_info["role"], "phone": user_phone}
    )

    return jsonify({
        "message": "Payment successful, plan upgraded",
        "access_token": new_token,
        "redirect": "/dashboard"
    }), 200

# ------------------------------------------------------------
# Protected pages
# ------------------------------------------------------------
@user_bp.route("/dashboard")
def user_dashboard():
    return render_template("users/dashboard.html", wallet=0, user=None)

@user_bp.route('/create-listing')
@requires_active_plan('business_basic', 'business_premium')
def create_listing():
    user_id = get_jwt_identity()
    db = get_db()
    user = db.execute(
        text("SELECT role, business_limit, extra_businesses_purchased FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()

    business_count = db.execute(
        text("SELECT COUNT(*) FROM listings WHERE user_id = :uid"),
        {"uid": user_id}
    ).scalar()  # Use scalar for aggregate

    max_allowed = user._mapping["business_limit"] + user._mapping["extra_businesses_purchased"]

    if business_count >= max_allowed:
        if user._mapping["role"] == "business_premium":
            flash("You have reached your free business limit. Purchase an extra slot for ₹259.", "warning")
            return redirect(url_for('user.extra_business_payment'))
        else:
            flash("You have reached your business limit. Upgrade to Premium for more slots.", "warning")
            return redirect(url_for('user.pricing'))

    return render_template('users/create_listing.html')

@user_bp.route("/extra-business")
@requires_active_plan('business_premium')
def extra_business_payment():
    return render_template("users/extra_business.html")

@user_bp.route("/browse")
@user_bp.route("/search")
def browse():
    return render_template("users/browse.html")

@user_bp.route("/api/browse")
def api_browse():
    try:
        page = int(request.args.get("page", 1))
        search = request.args.get("search", "")
        category = request.args.get("category", "")
        distance = request.args.get("distance")
        lat = request.args.get("lat")
        lng = request.args.get("lng")

        limit = 10
        offset = (page - 1) * limit

        conn = get_db()

        # Base query with optional distance calculation
        query = text("""
            SELECT *,
            CASE 
                WHEN :lat IS NOT NULL AND :lng IS NOT NULL THEN (
                    6371 * acos(
                        cos(radians(:lat)) *
                        cos(radians(latitude)) *
                        cos(radians(longitude) - radians(:lng)) +
                        sin(radians(:lat)) *
                        sin(radians(latitude))
                    )
                )
                ELSE NULL
            END as distance
            FROM businesses
            WHERE is_active = 1
            AND (:search = '' OR business_name ILIKE :search)
            AND (:category = '' OR category = :category)
            AND (:distance IS NULL OR (
                6371 * acos(
                    cos(radians(:lat)) *
                    cos(radians(latitude)) *
                    cos(radians(longitude) - radians(:lng)) +
                    sin(radians(:lat)) *
                    sin(radians(latitude))
                )
            ) <= :distance)
            ORDER BY featured DESC, premium DESC, distance ASC
            LIMIT :limit OFFSET :offset
        """)

        params = {
            "lat": float(lat) if lat else None,
            "lng": float(lng) if lng else None,
            "search": f"%{search}%" if search else "",
            "category": category if category else "",
            "distance": float(distance) if distance else None,
            "limit": limit,
            "offset": offset
        }

        rows = conn.execute(query, params).fetchall()
        listings = [dict(r._mapping) for r in rows]

        return jsonify({
            "listings": listings,
            "has_more": len(rows) == limit
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------------------------------------------
# Invite & referral
# ------------------------------------------------------------
@user_bp.route("/invite")
def invite():
    return render_template("users/invite.html")

@user_bp.route("/api/invite")
@jwt_required()
def api_invite():
    user_id = get_jwt_identity()
    conn = get_db()
    user = conn.execute(
        text("SELECT referral_code FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if not user or not user._mapping["referral_code"]:
        code = secrets.token_urlsafe(8)
        conn.execute(
            text("UPDATE users SET referral_code = :code WHERE id = :uid"),
            {"code": code, "uid": user_id}
        )
        conn.commit()
        referral_code = code
    else:
        referral_code = user._mapping["referral_code"]
    return jsonify({"referral_code": referral_code})

# ------------------------------------------------------------
# Track, recommend, subscription status, pricing
# ------------------------------------------------------------
@user_bp.route("/api/track", methods=["POST"])
@jwt_required()
def track():
    data = request.json
    user_id = get_jwt_identity()
    conn = get_db()
    conn.execute(
        text("INSERT INTO interactions (business_id, user_id, action) VALUES (:bid, :uid, :action)"),
        {"bid": data["business_id"], "uid": user_id, "action": data["action"]}
    )
    conn.commit()
    return jsonify({"status": "ok"})

@user_bp.route("/api/recommend")
def recommend():
    conn = get_db()
    rows = conn.execute(text("""
        SELECT b.*, COUNT(i.id) as score
        FROM businesses b
        LEFT JOIN interactions i ON b.id = i.business_id
        GROUP BY b.id
        ORDER BY score DESC
        LIMIT 10
    """)).fetchall()
    return jsonify([dict(r._mapping) for r in rows])

@user_bp.route("/subscription-status", methods=["GET"])
@jwt_required()
def subscription_status():
    user_id = get_jwt_identity()
    conn = get_db()
    user = conn.execute(
        text("SELECT role, plan, subscription_expiry FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404

    expiry_str = user._mapping["subscription_expiry"]
    is_active = False
    if expiry_str:
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            if expiry_date >= date.today():
                is_active = True
        except (ValueError, TypeError):
            pass

    role = user._mapping["role"]
    plan = user._mapping["plan"]
    allowed_roles = ["service_provider", "business_basic", "business_premium"]
    can_create = (role in allowed_roles and is_active)

    return jsonify({
        "can_create_listing": can_create,
        "role": role,
        "plan": plan,
        "subscription_active": is_active
    })

@user_bp.route("/pricing")
def pricing():
    return render_template("users/pricing.html")