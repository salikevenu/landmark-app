from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token
from datetime import datetime, timedelta, date
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
        "SELECT name, phone, role, plan, referral_code, subscription_expiry FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    return dict(user) if user else None

# ------------------------------------------------------------
# Profile pages & API (unchanged)
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
    conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
    conn.commit()
    return jsonify({"message": "Profile updated"}), 200

@user_bp.route("/logout", methods=["POST"])
def logout():
    return jsonify({"message": "Logged out"}), 200

# ------------------------------------------------------------
# PLAN DETAILS (incl. extra business)
# ------------------------------------------------------------
PLAN_DETAILS = {
    "service": {"amount": 49900, "role": "service_provider", "plan": "service"},
    "basic":   {"amount": 99900, "role": "business_basic",   "plan": "basic"},
    "premium": {"amount": 199900, "role": "business_premium", "plan": "premium"},
    "extra_business": {"amount": 24900, "role": None, "plan": "extra_business"}  # keeps existing role
}

# ------------------------------------------------------------
# CREATE ORDER (works for all plans + extra business)
# ------------------------------------------------------------
@user_bp.route("/create-order", methods=["POST"])
@jwt_required()
def create_order():
    """Create a Razorpay order for the selected plan / extra business."""
    user_id = get_jwt_identity()
    data = request.get_json()
    plan_type = data.get("plan")
    if plan_type not in PLAN_DETAILS:
        return jsonify({"error": "Invalid plan"}), 400

    plan_info = PLAN_DETAILS[plan_type]
    receipt = f"upgrade_{user_id}_{int(datetime.utcnow().timestamp())}"
    order_data = {
        "amount": plan_info["amount"],
        "currency": "INR",
        "receipt": receipt,
        "payment_capture": 1,
        "notes": {
            "user_id": str(user_id),
            "plan_type": plan_type,
            "role": plan_info["role"] or "",
            "plan": plan_info["plan"]
        }
    }
    try:
        order = razor_client.order.create(data=order_data)
        return jsonify({
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "key": os.getenv("RAZORPAY_KEY_ID")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------------------------------------------
# VERIFY PAYMENT (handles upgrades AND extra business)
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

    # ----- Extra business purchase (doesn't change role) -----
    if plan_type == "extra_business":
        conn.execute(
            "UPDATE users SET extra_businesses_purchased = extra_businesses_purchased + 1 WHERE id = ?",
            (user_id,)
        )
        conn.commit()
        # No referral commission for extra business (optional)
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
        "UPDATE users SET role = ?, plan = ?, subscription_expiry = ?, business_limit = ? WHERE id = ?",
        (plan_info["role"], plan_info["plan"], expiry_date, business_limit, user_id)
    )
    conn.commit()

    # Process referral commission
    from services.referral_commission import process_referral_commission
    process_referral_commission(user_id, plan_info["amount"] / 100)

    # Record transaction
    conn.execute(
        """INSERT INTO payment_transactions 
           (user_id, razorpay_order_id, razorpay_payment_id, amount, status) 
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, order_id, payment_id, plan_info["amount"], "captured")
    )
    conn.commit()
    conn.close()

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
# Protected pages (unchanged)
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
        "SELECT role, business_limit, extra_businesses_purchased FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    business_count = db.execute(
        "SELECT COUNT(*) FROM listings WHERE user_id = ?", (user_id,)
    ).fetchone()[0]

    max_allowed = user["business_limit"] + user["extra_businesses_purchased"]

    if business_count >= max_allowed:
        if user["role"] == "business_premium":
            flash("You have reached your free business limit. Purchase an extra slot for ₹259.", "warning")
            return redirect(url_for('user.extra_business_payment'))
        else:
            flash("You have reached your business limit. Upgrade to Premium for more slots.", "warning")
            return redirect(url_for('user.pricing'))

    return render_template('users/create_listing.html')

# --- EXTRA BUSINESS PAYMENT PAGE (shows Razorpay button) ---
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
    # ... (your existing browse API – unchanged)
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
        cursor = conn.cursor()

        query = """
        SELECT *,
        CASE 
            WHEN ? IS NOT NULL AND ? IS NOT NULL THEN (
                6371 * acos(
                    cos(radians(?)) *
                    cos(radians(latitude)) *
                    cos(radians(longitude) - radians(?)) +
                    sin(radians(?)) *
                    sin(radians(latitude))
                )
            )
            ELSE NULL
        END as distance
        FROM businesses
        WHERE is_active = 1
        """
        params = [lat, lng, lat, lng, lat]

        if search:
            query += " AND business_name LIKE ?"
            params.append(f"%{search}%")
        if category and category != "all":
            query += " AND category = ?"
            params.append(category)
        if lat and lng and distance:
            query += """
            AND (
                6371 * acos(
                    cos(radians(?)) *
                    cos(radians(latitude)) *
                    cos(radians(longitude) - radians(?)) +
                    sin(radians(?)) *
                    sin(radians(latitude))
                )
            ) <= ?
            """
            params.extend([lat, lng, lat, float(distance)])

        query += " ORDER BY featured DESC, premium DESC, distance ASC"
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()

        return jsonify({
            "listings": [dict(r) for r in rows],
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
    user = conn.execute("SELECT referral_code FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or not user["referral_code"]:
        code = secrets.token_urlsafe(8)
        conn.execute("UPDATE users SET referral_code = ? WHERE id = ?", (code, user_id))
        conn.commit()
        referral_code = code
    else:
        referral_code = user["referral_code"]
    return jsonify({"referral_code": referral_code})

# ------------------------------------------------------------
# Track, recommend, subscription status, pricing (unchanged)
# ------------------------------------------------------------
@user_bp.route("/api/track", methods=["POST"])
@jwt_required()
def track():
    data = request.json
    user_id = get_jwt_identity()
    conn = get_db()
    conn.execute(
        "INSERT INTO interactions (business_id, user_id, action) VALUES (?, ?, ?)",
        (data["business_id"], user_id, data["action"])
    )
    conn.commit()
    return jsonify({"status": "ok"})

@user_bp.route("/api/recommend")
def recommend():
    conn = get_db()
    rows = conn.execute("""
        SELECT b.*, COUNT(i.id) as score
        FROM businesses b
        LEFT JOIN interactions i ON b.id = i.business_id
        GROUP BY b.id
        ORDER BY score DESC
        LIMIT 10
    """).fetchall()
    return jsonify([dict(r) for r in rows])

@user_bp.route("/subscription-status", methods=["GET"])
@jwt_required()
def subscription_status():
    user_id = get_jwt_identity()
    conn = get_db()
    user = conn.execute(
        "SELECT role, plan, subscription_expiry FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404

    expiry_str = user["subscription_expiry"]
    is_active = False
    if expiry_str:
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            if expiry_date >= date.today():
                is_active = True
        except (ValueError, TypeError):
            pass

    role = user["role"]
    plan = user["plan"]
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