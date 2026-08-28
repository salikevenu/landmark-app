import os
import time
import secrets
import traceback

from flask import Blueprint, request, jsonify, render_template, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from werkzeug.utils import secure_filename
from functools import wraps
from sqlalchemy import text

from database.init_db import get_db_connection
from services.listing_service import (
    add_review_service,
    get_reviews_service,
    update_review_service,
    delete_review_service,
)
from services.subscription_access import is_subscription_active
from services.authz import db_user_is_admin
from services.sponsorship import public_is_sponsored_sql, sponsorship_rank_sql
import logging
logger = logging.getLogger(__name__)
# ---------- Custom role‑required decorator (JWT) ----------
def role_required(required_roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            if claims.get("role") not in required_roles:
                return jsonify({"error": "Insufficient permissions"}), 403
            if "admin" in required_roles and not db_user_is_admin(get_jwt_identity()):
                return jsonify({"error": "Insufficient permissions"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# -----------------------------------------------------------
listing_bp = Blueprint("listing", __name__)

ALLOWED_LISTING_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_LISTING_IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
ALLOWED_LISTING_VIDEO_EXTS = {"mp4", "mov"}
ALLOWED_LISTING_VIDEO_MIMES = {"video/mp4", "video/quicktime"}


def _listing_upload_allowed(file_storage, allowed_exts, allowed_mimes):
    """Extension + MIME allowlist (same idea as avatar upload; MIME added here)."""
    filename = secure_filename(file_storage.filename or "")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = (file_storage.mimetype or "").split(";")[0].strip().lower()
    return ext in allowed_exts and mime in allowed_mimes


def _parse_coord(value, lo, hi, field):
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {field}")
    if not (lo <= num <= hi):
        raise ValueError(f"Invalid {field}")
    return num


def _paid_listing_user(conn, user_id, *, for_update=False):
    """Load the authenticated user from DB. JWT role/plan claims are ignored."""
    sql = """
        SELECT id, role, plan, subscription_expiry, is_active, extra_businesses_purchased
        FROM users WHERE id = :uid
    """
    if for_update:
        sql += " FOR UPDATE"
    row = conn.execute(text(sql), {"uid": user_id}).fetchone()
    if not row or not row._mapping.get("is_active"):
        return None, (jsonify({"error": "User not found or inactive"}), 404)
    user_dict = dict(row._mapping)
    if not is_subscription_active(user_dict):
        return None, (jsonify({"error": "Active subscription required"}), 403)
    return user_dict, None


def _normalize_image_type(raw):
    allowed = {"logo", "shop", "service"}
    value = (raw or "shop").strip().lower()
    if value in ("gallery", "image", "photo"):
        return "shop"
    return value if value in allowed else "shop"

# =========================
# CREATE LISTING API
# =========================
@listing_bp.route("/create-listing", methods=["POST"])
@jwt_required()
def api_create_listing():
    try:
        user_id = get_jwt_identity()
        claims = get_jwt()
        user_phone = claims.get("phone")

        conn = get_db_connection()
        user_dict, err = _paid_listing_user(conn, user_id, for_update=True)
        if err:
            return err

        plan = user_dict.get("plan")
        listing_count = conn.execute(
            text("SELECT COUNT(*) as cnt FROM listings WHERE user_id = :uid"),
            {"uid": user_id}
        ).fetchone()._mapping["cnt"]

        extra_slots = int(user_dict.get("extra_businesses_purchased") or 0)
        if plan == "business_basic" and listing_count >= 1:
            return jsonify({"error": "Basic plan allows only 1 listing. Upgrade to Premium."}), 403
        elif plan == "business_premium" and listing_count >= 3 + extra_slots:
            return jsonify({"error": "Premium plan allows up to 3 listings."}), 403
        elif plan == "service_provider" and listing_count >= 10:
            return jsonify({"error": "Service provider limit reached (10 listings)."}), 403
        elif plan not in ("business_basic", "business_premium", "service_provider"):
            return jsonify({"error": "Active subscription required"}), 403

        business_name = request.form.get("business_name")
        category = request.form.get("category")
        try:
            latitude = _parse_coord(request.form.get("latitude"), -90, 90, "latitude")
            longitude = _parse_coord(request.form.get("longitude"), -180, 180, "longitude")
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

        listing_type = (request.form.get("listing_type") or "business").strip().lower()
        if listing_type not in ("business", "service"):
            listing_type = "business"

        if not business_name or not category:
            return jsonify({"success": False, "error": "Business name and category required"}), 400

        # Owner and status are server-controlled. Ignore client user_id/status/premium flags.
        result = conn.execute(text("""
            INSERT INTO listings (
                user_id, user_phone, listing_type, business_name, category,
                city, state, latitude, longitude,
                description, whatsapp, website, status, is_active,
                is_premium, is_featured, is_sponsored, is_verified
            ) VALUES (
                :user_id, :user_phone, :listing_type, :business_name, :category,
                :city, :state, :latitude, :longitude,
                :description, :whatsapp, :website, 'pending', 1,
                0, 0, 0, 0
            )
            RETURNING id
        """), {
            "user_id": user_id,
            "user_phone": user_phone,
            "listing_type": listing_type,
            "business_name": business_name,
            "category": category,
            "city": request.form.get("city", ""),
            "state": request.form.get("state", ""),
            "latitude": latitude,
            "longitude": longitude,
            "description": request.form.get("description", ""),
            "whatsapp": request.form.get("whatsapp", ""),
            "website": request.form.get("website", ""),
        })
        listing_id = result.fetchone()[0]

        # Image uploads
        upload_dir = current_app.config['UPLOAD_FOLDER']
        images = request.files.getlist("images")
        for img in images:
            if img and img.filename:
                if not _listing_upload_allowed(img, ALLOWED_LISTING_IMAGE_EXTS, ALLOWED_LISTING_IMAGE_MIMES):
                    return jsonify({
                        "success": False,
                        "error": "Invalid image type. Use jpg, jpeg, png, or webp"
                    }), 400
                safe = secure_filename(img.filename)
                ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else "jpg"
                filename = f"{listing_id}_{int(time.time()*1000)}_{secrets.token_hex(4)}.{ext}"
                os.makedirs(upload_dir, exist_ok=True)
                path = os.path.join(upload_dir, filename)
                img.save(path)
                conn.execute(text(
                    "INSERT INTO listing_images (listing_id, image_url, image_type) VALUES (:lid, :url, :type)"
                ), {
                    "lid": listing_id,
                    "url": f"/static/uploads/{filename}",
                    "type": "shop"
                })

        # Optional video (mp4 / mov)
        video = request.files.get("video")
        if video and video.filename:
            if not _listing_upload_allowed(video, ALLOWED_LISTING_VIDEO_EXTS, ALLOWED_LISTING_VIDEO_MIMES):
                return jsonify({
                    "success": False,
                    "error": "Invalid video type. Use mp4 or mov"
                }), 400
            safe = secure_filename(video.filename)
            ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else "mp4"
            filename = f"{listing_id}_{int(time.time()*1000)}_{secrets.token_hex(4)}.{ext}"
            os.makedirs(upload_dir, exist_ok=True)
            path = os.path.join(upload_dir, filename)
            video.save(path)
            conn.execute(text(
                "UPDATE listings SET video = :video WHERE id = :lid"
            ), {
                "video": f"/static/uploads/{filename}",
                "lid": listing_id
            })

        conn.commit()
        return jsonify({
            "success": True,
            "message": "Listing submitted for review",
            "listing_id": listing_id
        }), 201

    except Exception as e:
        logger.error(traceback.format_exc())
        return jsonify({"success": False, "error": "Internal Server Error"}), 500


# =========================
# ADMIN PENDING LISTINGS (HTML page)
# =========================
@listing_bp.route("/admin/listings")
@jwt_required()
def admin_listings():
    if not db_user_is_admin(get_jwt_identity()):
        return jsonify({"error": "Admin access required"}), 403
    conn = get_db_connection()
    listings = conn.execute(text("SELECT * FROM listings WHERE status='pending'")).fetchall()
    return render_template("admin/listings.html", listings=[dict(r._mapping) for r in listings])


# =========================
# MY LISTINGS PAGE (HTML)
# =========================
@listing_bp.route("/my-listings")
def my_listings_page():
    return render_template("users/my_listings.html")


# =========================
# MY LISTINGS DATA (JSON)
# =========================
@listing_bp.route("/my-listings-data", methods=["GET"])
@jwt_required()
def my_listings():
    user_id = get_jwt_identity()
    conn = get_db_connection()
    rows = conn.execute(text("""
        SELECT l.*, 
            (SELECT image_url FROM listing_images WHERE listing_id = l.id LIMIT 1) as image_url
        FROM listings l
        WHERE l.user_id = :uid
        ORDER BY l.id DESC
    """), {"uid": user_id}).fetchall()

    listings = [dict(r._mapping) for r in rows]
    return jsonify({"listings": listings})


# =========================
# UPDATE LISTING (JSON API)
# =========================
@listing_bp.route("/update-listing/<int:listing_id>", methods=["PUT"])
@jwt_required()
def update_listing(listing_id):
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or request.form.to_dict()

    conn = get_db_connection()
    _, err = _paid_listing_user(conn, user_id)
    if err:
        return err
    listing = conn.execute(
        text("SELECT id FROM listings WHERE id = :lid AND user_id = :uid AND is_active = 1"),
        {"lid": listing_id, "uid": user_id}
    ).fetchone()
    if not listing:
        return jsonify({"error": "Not found or unauthorized"}), 404

    conn.execute(text("""
        UPDATE listings
        SET business_name = :bname, category = :cat, city = :city, state = :state, description = :desc
        WHERE id = :lid AND user_id = :uid
    """), {
        "bname": data.get("business_name"),
        "cat": data.get("category"),
        "city": data.get("city"),
        "state": data.get("state"),
        "desc": data.get("description"),
        "lid": listing_id,
        "uid": user_id,
    })
    conn.commit()
    return jsonify({"message": "Listing updated"})


# =========================
# DELETE LISTING
# =========================
@listing_bp.route("/delete-listing/<int:listing_id>", methods=["DELETE"])
@jwt_required()
def delete_listing(listing_id):
    user_id = get_jwt_identity()
    conn = get_db_connection()
    _, err = _paid_listing_user(conn, user_id)
    if err:
        return err
    listing = conn.execute(
        text("SELECT id FROM listings WHERE id = :lid AND user_id = :uid"),
        {"lid": listing_id, "uid": user_id}
    ).fetchone()
    if not listing:
        return jsonify({"error": "Not found or unauthorized"}), 404

    conn.execute(text("DELETE FROM listing_images WHERE listing_id = :lid"), {"lid": listing_id})
    conn.execute(text("DELETE FROM listings WHERE id = :lid AND user_id = :uid"), {"lid": listing_id, "uid": user_id})
    conn.commit()
    return jsonify({"message": "Listing deleted"})


# =========================
# UPLOAD LISTING IMAGE (separate)
# =========================
@listing_bp.route("/upload-listing-image", methods=["POST"])
@jwt_required()
def upload_listing_image():
    user_id = get_jwt_identity()
    try:
        listing_id = int(request.form.get("listing_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "listing_id required"}), 400
    image_type = _normalize_image_type(request.form.get("image_type", "shop"))
    image = request.files.get("image")
    if not image or not image.filename:
        return jsonify({"error": "Image required"}), 400
    if not _listing_upload_allowed(image, ALLOWED_LISTING_IMAGE_EXTS, ALLOWED_LISTING_IMAGE_MIMES):
        return jsonify({"error": "Invalid image type. Use jpg, jpeg, png, or webp"}), 400

    conn = get_db_connection()
    _, err = _paid_listing_user(conn, user_id)
    if err:
        return err
    owned = conn.execute(
        text("SELECT id FROM listings WHERE id = :lid AND user_id = :uid"),
        {"lid": listing_id, "uid": user_id},
    ).fetchone()
    if not owned:
        return jsonify({"error": "Not found or unauthorized"}), 404

    image.stream.seek(0, os.SEEK_END)
    size = image.stream.tell()
    image.stream.seek(0)
    if size > 10 * 1024 * 1024:
        return jsonify({"error": "Image must be 10 MB or smaller"}), 400

    ext = secure_filename(image.filename).rsplit(".", 1)[-1].lower()
    filename = f"{listing_id}_{int(time.time()*1000)}_{secrets.token_hex(4)}.{ext}"
    upload_subfolder = os.path.join(current_app.root_path, "static", "images", "listings")
    os.makedirs(upload_subfolder, exist_ok=True)
    filepath = os.path.join(upload_subfolder, filename)
    image.save(filepath)
    image_url = f"/static/images/listings/{filename}"

    conn.execute(text(
        "INSERT INTO listing_images (listing_id, image_url, image_type) VALUES (:lid, :url, :type)"
    ), {"lid": listing_id, "url": image_url, "type": image_type})
    conn.commit()
    return jsonify({"success": True, "image_url": image_url})


# =========================
# GET SINGLE LISTING (owner only)
# =========================
@listing_bp.route("/listing/<int:listing_id>")
@jwt_required()
def get_listing(listing_id):
    user_id = get_jwt_identity()
    conn = get_db_connection()
    _, err = _paid_listing_user(conn, user_id)
    if err:
        return err
    row = conn.execute(text("""
        SELECT id, business_name, category, city, state, latitude, longitude, description
        FROM listings
        WHERE id = :lid AND user_id = :uid
    """), {"lid": listing_id, "uid": user_id}).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row._mapping))


# =========================
# RATE BUSINESS (public)
# =========================
@listing_bp.route("/rate", methods=["POST"])
@jwt_required()
def rate_business():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    user_id = get_jwt_identity()
    result = add_review_service(data, user_id)
    if result.get("error"):
        return jsonify({"error": result["error"]}), result.get("_http") or 400
    return jsonify({"status": "success"})


# =========================
# TRACK CALL / WHATSAPP CLICKS (public)
# =========================
@listing_bp.route("/click-call/<int:listing_id>", methods=["POST"])
def track_call_click(listing_id):
    conn = get_db_connection()
    conn.execute(text("""
        UPDATE listings SET clicks = COALESCE(clicks, 0) + 1
        WHERE id = :lid AND status = 'approved' AND is_active = 1
    """), {"lid": listing_id})
    conn.commit()
    return jsonify({"status": "ok"})

@listing_bp.route("/click-whatsapp/<int:listing_id>", methods=["POST"])
def track_whatsapp_click(listing_id):
    conn = get_db_connection()
    conn.execute(text("""
        UPDATE listings SET whatsapp_clicks = COALESCE(whatsapp_clicks, 0) + 1
        WHERE id = :lid AND status = 'approved' AND is_active = 1
    """), {"lid": listing_id})
    conn.commit()
    return jsonify({"status": "ok"})


# =========================
# GET LISTING IMAGES (public)
# =========================
@listing_bp.route("/listing-images/<int:listing_id>")
def get_listing_images(listing_id):
    conn = get_db_connection()
    listing = conn.execute(
        text("SELECT id FROM listings WHERE id = :lid AND status = 'approved'"),
        {"lid": listing_id},
    ).fetchone()
    if not listing:
        return jsonify({"images": []}), 404
    rows = conn.execute(
        text("SELECT image_url, image_type FROM listing_images WHERE listing_id = :lid"),
        {"lid": listing_id}
    ).fetchall()
    return jsonify({
        "images": [{"image_url": r._mapping["image_url"], "type": r._mapping["image_type"]} for r in rows]
    })


# =========================
# BROWSE PAGE (HTML)
# =========================
@listing_bp.route("/browse")
def browse_page():
    category = request.args.get("category", "")
    location = request.args.get("location", "")
    return render_template(
        "public/browse.html",
        page_title="Discover Businesses Near You | LANDMARK",
        category=category,
        location=location
    )


# =========================
# BROWSE API (JSON) – public
# =========================
@listing_bp.route("/data/browse")
def browse_api():
    try:
        search = request.args.get("search", "").strip()
        category = request.args.get("category", "").strip()
        location = request.args.get("location", "").strip()
        lat = request.args.get("lat", type=float)
        lng = request.args.get("lng", type=float)
        distance = request.args.get("distance", type=float)
        page = request.args.get("page", 1, type=int) or 1
        page = max(1, min(page, 10000))
        limit = 10
        offset = (page - 1) * limit

        conn = get_db_connection()
        live = public_is_sponsored_sql("l")
        rank = sponsorship_rank_sql("l")

        # Use a CTE to calculate distance once, then filter and order
        query = text(f"""
            WITH dist AS (
                SELECT l.*,
                    (6371 * acos(
                        cos(radians(:lat)) *
                        cos(radians(l.latitude)) *
                        cos(radians(l.longitude) - radians(:lng)) +
                        sin(radians(:lat)) *
                        sin(radians(l.latitude))
                    )) AS distance,
                    COALESCE(l.is_verified, 0) as verified,
                    COALESCE(l.is_premium, 0) as premium,
                    COALESCE(l.is_featured, 0) as featured,
                    CASE WHEN {live} THEN 1 ELSE 0 END as sponsored,
                    (SELECT image_url FROM listing_images WHERE listing_id = l.id LIMIT 1) as main_image
                FROM listings l
                WHERE l.status = 'approved'
                  AND (:search = '' OR l.business_name ILIKE :search)
                  AND (:category = '' OR l.category = :category)
                  AND (:location = '' OR l.city ILIKE :location)
            )
            SELECT *
            FROM dist
            WHERE (:distance IS NULL OR distance <= :distance)
            ORDER BY sponsored DESC, featured DESC, premium DESC, verified DESC, distance ASC, rating DESC
            LIMIT :limit OFFSET :offset
        """)

        params = {
            "lat": lat or 0,
            "lng": lng or 0,
            "search": f"%{search}%" if search else "",
            "category": category or "",
            "location": f"%{location}%" if location else "",
            "distance": distance,
            "limit": limit,
            "offset": offset
        }

        rows = conn.execute(query, params).fetchall()
        listings = []
        for r in rows:
            rm = r._mapping
            listings.append({
                "id": rm["id"],
                "business_name": rm["business_name"],
                "category": rm["category"],
                "city": rm["city"],
                "state": rm["state"],
                "phone": rm.get("user_phone") or rm.get("phone"),
                "whatsapp": rm.get("whatsapp"),
                "image": rm["main_image"] or "/static/default.jpg",
                "video": rm.get("video"),
                "rating": rm["rating"] or 4.0,
                "rating_count": rm["rating_count"] or 0,
                "distance": rm["distance"],
                "latitude": rm["latitude"],
                "longitude": rm["longitude"],
                "verified": bool(rm["verified"]),
                "premium": bool(rm["premium"]),
                "featured": bool(rm["featured"]),
                "sponsored": bool(rm.get("sponsored")),
                "is_sponsored": bool(rm.get("sponsored")),
            })
        return jsonify({"listings": listings, "page": page, "count": len(listings)})
    except Exception:
        logger.exception("browse API error")
        return jsonify({"error": "Something went wrong. Please try again."}), 500
    
# =========================
# ADD REVIEW (auth required)
# =========================
@listing_bp.route("/review", methods=["POST"])
@jwt_required()
def add_review():
    user_id = get_jwt_identity()
    result = add_review_service(request.get_json(silent=True) or {}, user_id)
    if result.get("error"):
        return jsonify({"error": result["error"]}), result.get("_http") or 400
    return jsonify(result)


# =========================
# GET REVIEWS (public)
# =========================
@listing_bp.route("/reviews/<int:listing_id>")
def get_reviews(listing_id):
    return jsonify(get_reviews_service(listing_id))


@listing_bp.route("/review/<int:review_id>", methods=["PUT", "PATCH"])
@jwt_required()
def update_review(review_id):
    user_id = get_jwt_identity()
    result = update_review_service(review_id, request.get_json(silent=True) or {}, user_id)
    if result.get("error"):
        return jsonify({"error": result["error"]}), result.get("_http") or 400
    return jsonify(result)


@listing_bp.route("/review/<int:review_id>", methods=["DELETE"])
@jwt_required()
def delete_review(review_id):
    user_id = get_jwt_identity()
    result = delete_review_service(review_id, user_id)
    if result.get("error"):
        return jsonify({"error": result["error"]}), result.get("_http") or 400
    return jsonify(result)


# =========================
# PUBLIC LISTING DETAIL
# =========================
@listing_bp.route("/api/listing/<int:listing_id>")
def public_listing_detail(listing_id):
    conn = get_db_connection()
    live = public_is_sponsored_sql("")
    listing = conn.execute(text(f"""
        SELECT id, business_name, category, city, state, latitude, longitude,
               description, whatsapp, website, rating, rating_count,
               is_verified, is_premium, is_featured, user_phone,
               CASE WHEN {live} THEN 1 ELSE 0 END AS is_sponsored
        FROM listings
        WHERE id = :lid AND status = 'approved'
    """), {"lid": listing_id}).fetchone()
    if not listing:
        return jsonify({"error": "Listing not found"}), 404

    image_row = conn.execute(
        text("SELECT image_url FROM listing_images WHERE listing_id = :lid LIMIT 1"),
        {"lid": listing_id}
    ).fetchone()

    data = dict(listing._mapping)
    data["image"] = image_row._mapping["image_url"] if image_row else None
    return jsonify({"listing": data})