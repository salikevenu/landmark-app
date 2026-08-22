from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token
from datetime import datetime, timedelta, date
from sqlalchemy import text
from database.init_db import get_db_connection
from time import time
import razorpay
import os
import secrets
import logging
from routes.decorators import requires_active_plan
from services.subscription_access import is_subscription_active

user_bp = Blueprint("user", __name__)
logger = logging.getLogger(__name__)

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
_avatar_column_ready = False


def _as_user_id(identity):
    try:
        return int(identity)
    except (TypeError, ValueError):
        return identity


def get_user_by_id(user_id):
    global _avatar_column_ready
    user_id = _as_user_id(user_id)
    conn = get_db_connection()
    if not _avatar_column_ready:
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT"))
            conn.commit()
            _avatar_column_ready = True
        except Exception:
            pass
    user = conn.execute(
        text("""
            SELECT id, name, phone, role, plan, referral_code, subscription_expiry, avatar_url
            FROM users
            WHERE id = :uid
        """),
        {"uid": user_id},
    ).fetchone()
    return dict(user._mapping) if user else None


def _profile_payload(user):
    return {
        "id": user["id"],
        "phone": user["phone"],
        "name": user["name"] or user["phone"] or "",
        "role": user["role"] or "user",
        "plan": user["plan"] or "free",
        "referral_code": user.get("referral_code") or "",
        "avatar_url": user.get("avatar_url") or "",
    }


# ------------------------------------------------------------
# Profile pages & API
# Blueprint prefix is /api/user → final paths:
#   GET  /api/user/profile
#   PUT  /api/user/profile/update
#   GET  /api/user/profile/data
# ------------------------------------------------------------
@user_bp.route("/profile", methods=["GET"])
def profile():
    """
    Browser navigation (no Bearer) → HTML page.
    Authenticated fetch (Authorization: Bearer …) → JSON profile
    (matches templates/users/profile.html).
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from flask_jwt_extended import verify_jwt_in_request
            verify_jwt_in_request()
        except Exception:
            return jsonify({"error": "Unauthorized"}), 401

        user_id = get_jwt_identity()
        try:
            user = get_user_by_id(user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404
            return jsonify(_profile_payload(user)), 200
        except Exception as e:
            logger.exception("profile error")
            return jsonify({"error": "Something went wrong. Please try again."}), 500

    return render_template("users/profile.html", role="", plan="")


@user_bp.route("/profile/data", methods=["GET"])
@jwt_required()
def profile_data():
    """Optional explicit JSON profile endpoint."""
    user_id = _as_user_id(get_jwt_identity())
    conn = None
    try:
        conn = get_db_connection()
        user = conn.execute(
            text("""
                SELECT id, name, phone, role, plan, referral_code, subscription_expiry, avatar_url
                FROM users
                WHERE id = :uid
            """),
            {"uid": user_id},
        ).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404
        return jsonify(_profile_payload(dict(user._mapping))), 200
    except Exception:
        logger.exception("profile_data error")
        return jsonify({"error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@user_bp.route("/profile/update", methods=["PUT"])
@jwt_required()
def update_profile():
    """Update the current user's display name."""
    user_id = _as_user_id(get_jwt_identity())
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({"error": "Name required"}), 400
    if len(name) > 100:
        return jsonify({"error": "Name must be 100 characters or fewer"}), 400

    try:
        conn = get_db_connection()
        result = conn.execute(
            text("UPDATE users SET name = :name WHERE id = :uid RETURNING id"),
            {"name": name, "uid": user_id},
        ).fetchone()
        conn.commit()
        if not result:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"message": "Name updated successfully", "name": name}), 200
    except Exception as e:
        logger.exception("update_profile error")
        return jsonify({"error": "Something went wrong. Please try again."}), 500


@user_bp.route("/profile/avatar", methods=["POST"])
@jwt_required()
def upload_profile_avatar():
    """Upload/replace the current user's profile picture."""
    from werkzeug.utils import secure_filename
    from flask import current_app

    user_id = _as_user_id(get_jwt_identity())
    file = request.files.get("avatar") or request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No image file provided"}), 400

    filename = secure_filename(file.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = {"jpg", "jpeg", "png", "webp", "gif"}
    if ext not in allowed:
        return jsonify({"error": "Invalid image type. Use jpg, png, webp, or gif"}), 400

    # Soft size guard (10MB) in addition to app MAX_CONTENT_LENGTH
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > 10 * 1024 * 1024:
        return jsonify({"error": "Avatar image must be 10 MB or smaller."}), 400

    try:
        upload_root = current_app.config.get("UPLOAD_FOLDER", "static/uploads")
        avatar_dir = os.path.join(current_app.root_path, upload_root, "avatars")
        os.makedirs(avatar_dir, exist_ok=True)

        stored_name = f"user_{user_id}_{int(time())}.{ext}"
        abs_path = os.path.join(avatar_dir, stored_name)
        file.save(abs_path)

        avatar_url = f"/static/uploads/avatars/{stored_name}"

        conn = get_db_connection()
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT"))
        result = conn.execute(
            text("UPDATE users SET avatar_url = :url WHERE id = :uid RETURNING id"),
            {"url": avatar_url, "uid": user_id},
        ).fetchone()
        conn.commit()
        if not result:
            return jsonify({"error": "User not found"}), 404

        return jsonify({
            "success": True,
            "message": "Avatar updated",
            "avatar_url": avatar_url,
        }), 200
    except Exception as e:
        logger.exception("upload_profile_avatar error")
        return jsonify({"error": "Something went wrong. Please try again."}), 500


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
# VERIFY PAYMENT (legacy URL — canonical flow is /api/payment/verify-payment)
# ------------------------------------------------------------
@user_bp.route("/verify-payment", methods=["POST"])
@jwt_required()
def verify_payment():
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    plan_type = data.get("plan")

    # extra_business is not on /pricing; keep the existing slot purchase path
    if plan_type == "extra_business":
        payment_id = data.get("razorpay_payment_id")
        order_id = data.get("razorpay_order_id")
        signature = data.get("razorpay_signature")
        if not all([payment_id, order_id, signature]):
            return jsonify({"success": False, "error": "Missing payment details"}), 400
        params = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        }
        try:
            razor_client.utility.verify_payment_signature(params)
        except razorpay.errors.SignatureVerificationError:
            return jsonify({"success": False, "error": "Invalid payment signature"}), 400
        conn = get_db_connection()
        conn.execute(
            text("UPDATE users SET extra_businesses_purchased = extra_businesses_purchased + 1 WHERE id = :uid"),
            {"uid": user_id},
        )
        conn.commit()
        return jsonify({
            "success": True,
            "message": "Extra business slot purchased successfully",
            "redirect": "/create-listing",
        })

    from services.payment_service import verify_payment_service
    from services.referral_commission import after_payment_finalized
    result = verify_payment_service(data, user_id)
    after_payment_finalized(result, razorpay_payment_id=data.get("razorpay_payment_id"))
    http = 200 if result.get("success") else result.pop("_http", 400)
    result.pop("_http", None)
    return jsonify(result), http

# ------------------------------------------------------------
# Protected pages
# ------------------------------------------------------------
@user_bp.route("/dashboard")
def user_dashboard():
    return render_template("users/dashboard.html", wallet=0, user=None)

@user_bp.route('/create-listing')
@requires_active_plan('service_provider', 'business_basic', 'business_premium')
def create_listing():
    user_id = get_jwt_identity()
    db = get_db_connection()
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
            return redirect("/api/user/pricing?page_type=business")

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

        conn = get_db_connection()

        # Base query with optional distance calculation
        query = text("""
            SELECT
                id,
                business_name,
                business_name AS name,
                category,
                city,
                state,
                user_phone AS phone,
                whatsapp,
                COALESCE(image, image_url, logo_url) AS image,
                rating,
                COALESCE(total_reviews, rating_count, 0) AS reviews,
                COALESCE(is_featured, 0) AS featured,
                COALESCE(is_verified, 0) AS verified,
                COALESCE(is_premium, 0) AS premium,
                latitude,
                longitude,
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
            FROM listings
            WHERE is_active = 1
            AND (status IS NULL OR status = 'approved')
            AND (:search = '' OR business_name ILIKE :search OR category ILIKE :search)
            AND (:category = '' OR category = :category)
            AND (:distance IS NULL OR (
                :lat IS NOT NULL AND :lng IS NOT NULL AND (
                    6371 * acos(
                        cos(radians(:lat)) *
                        cos(radians(latitude)) *
                        cos(radians(longitude) - radians(:lng)) +
                        sin(radians(:lat)) *
                        sin(radians(latitude))
                    )
                ) <= :distance
            ))
            ORDER BY featured DESC, premium DESC, distance ASC NULLS LAST
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
        logger.exception("browse listings error")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

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
    conn = get_db_connection()
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
    conn = get_db_connection()
    conn.execute(
        text("INSERT INTO interactions (business_id, user_id, action) VALUES (:bid, :uid, :action)"),
        {"bid": data["business_id"], "uid": user_id, "action": data["action"]}
    )
    conn.commit()
    return jsonify({"status": "ok"})

@user_bp.route("/api/recommend")
def recommend():
    conn = get_db_connection()
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
    conn = get_db_connection()
    user = conn.execute(
        text("SELECT role, plan, subscription_expiry FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404

    user_dict = dict(user._mapping)
    is_active = is_subscription_active(user_dict)
    can_create = is_active

    return jsonify({
        "can_create_listing": can_create,
        "role": user_dict.get("role"),
        "plan": user_dict.get("plan"),
        "subscription_active": is_active
    })

@user_bp.route("/pricing")
def pricing():
    page_type = (request.args.get("page_type") or "").strip().lower()
    if page_type not in ("service", "business"):
        page_type = ""
    return render_template("users/pricing.html", page_type=page_type)