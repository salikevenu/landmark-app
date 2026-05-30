import os
import time
import traceback
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from werkzeug.utils import secure_filename
from functools import wraps
from sqlalchemy import text

from database.init_db import get_db
from services.listing_service import add_review_service, get_reviews_service
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
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# -----------------------------------------------------------
listing_bp = Blueprint("listing", __name__)

# =========================
# HELPER: subscription check (consistent with app.py)
# =========================
def is_subscription_active(user_row):
    plan = user_row.get("plan", "free")
    if plan == "free":
        return False
    expiry_str = user_row.get("subscription_expiry")
    if not expiry_str:
        return False
    try:
        expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
        return expiry >= datetime.utcnow()
    except:
        return False

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

        conn = get_db()
        user = conn.execute(
            text("SELECT id, role, plan, subscription_expiry, is_active FROM users WHERE id = :uid"),
            {"uid": user_id}
        ).fetchone()

        if not user or not user._mapping["is_active"]:
            return jsonify({"error": "User not found or inactive"}), 404

        user_dict = dict(user._mapping)
        if not is_subscription_active(user_dict):
            return jsonify({"error": "Active subscription required"}), 403

        plan = user._mapping["plan"]
        listing_count = conn.execute(
            text("SELECT COUNT(*) as cnt FROM listings WHERE user_id = :uid"),
            {"uid": user_id}
        ).fetchone()._mapping["cnt"]

        if plan == "business_basic" and listing_count >= 1:
            return jsonify({"error": "Basic plan allows only 1 listing. Upgrade to Premium."}), 403
        elif plan == "business_premium" and listing_count >= 3:
            return jsonify({"error": "Premium plan allows up to 3 listings."}), 403
        elif plan == "service_provider" and listing_count >= 10:
            return jsonify({"error": "Service provider limit reached (10 listings)."}), 403

        business_name = request.form.get("business_name")
        category = request.form.get("category")
        latitude = request.form.get("latitude")
        longitude = request.form.get("longitude")

        if not business_name or not category:
            return jsonify({"success": False, "error": "Business name and category required"}), 400
        if not latitude or not longitude:
            return jsonify({"success": False, "error": "Location required"}), 400

        # Insert listing with RETURNING id
        result = conn.execute(text("""
            INSERT INTO listings (
                user_id, user_phone, listing_type, business_name, category,
                city, state, latitude, longitude,
                description, whatsapp, website, status
            ) VALUES (
                :user_id, :user_phone, :listing_type, :business_name, :category,
                :city, :state, :latitude, :longitude,
                :description, :whatsapp, :website, :status
            )
            RETURNING id
        """), {
            "user_id": user_id,
            "user_phone": user_phone,
            "listing_type": request.form.get("listing_type", "business"),
            "business_name": business_name,
            "category": category,
            "city": request.form.get("city", ""),
            "state": request.form.get("state", ""),
            "latitude": latitude,
            "longitude": longitude,
            "description": request.form.get("description", ""),
            "whatsapp": request.form.get("whatsapp", ""),
            "website": request.form.get("website", ""),
            "status": "pending"
        })
        listing_id = result.fetchone()[0]

        # Image uploads
        upload_dir = current_app.config['UPLOAD_FOLDER']
        images = request.files.getlist("images")
        for img in images:
            if img and img.filename:
                filename = f"{int(time.time()*1000)}_{secure_filename(img.filename)}"
                path = os.path.join(upload_dir, filename)
                img.save(path)
                conn.execute(text(
                    "INSERT INTO listing_images (listing_id, image_url, image_type) VALUES (:lid, :url, :type)"
                ), {
                    "lid": listing_id,
                    "url": f"/static/uploads/{filename}",
                    "type": "shop"
                })

        # Optional video
        video = request.files.get("video")
        if video and video.filename:
            filename = f"{int(time.time()*1000)}_{secure_filename(video.filename)}"
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
@role_required(["admin"])
def admin_listings():
    conn = get_db()
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
    conn = get_db()
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
@role_required(["service_provider", "business_basic", "business_premium"])
def update_listing(listing_id):
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or request.form.to_dict()

    conn = get_db()
    listing = conn.execute(
        text("SELECT id FROM listings WHERE id = :lid AND user_id = :uid"),
        {"lid": listing_id, "uid": user_id}
    ).fetchone()
    if not listing:
        return jsonify({"error": "Not found or unauthorized"}), 404

    conn.execute(text("""
        UPDATE listings
        SET business_name = :bname, category = :cat, city = :city, state = :state, description = :desc
        WHERE id = :lid
    """), {
        "bname": data.get("business_name"),
        "cat": data.get("category"),
        "city": data.get("city"),
        "state": data.get("state"),
        "desc": data.get("description"),
        "lid": listing_id
    })
    conn.commit()
    return jsonify({"message": "Listing updated"})


# =========================
# DELETE LISTING
# =========================
@listing_bp.route("/delete-listing/<int:listing_id>", methods=["DELETE"])
@jwt_required()
@role_required(["service_provider", "business_basic", "business_premium"])
def delete_listing(listing_id):
    user_phone = get_jwt().get("phone")
    conn = get_db()
    listing = conn.execute(
        text("SELECT id FROM listings WHERE id = :lid AND user_phone = :phone"),
        {"lid": listing_id, "phone": user_phone}
    ).fetchone()
    if not listing:
        return jsonify({"error": "Not found or unauthorized"}), 404

    conn.execute(text("DELETE FROM listings WHERE id = :lid"), {"lid": listing_id})
    conn.commit()
    return jsonify({"message": "Listing deleted"})


# =========================
# UPLOAD LISTING IMAGE (separate)
# =========================
@listing_bp.route("/upload-listing-image", methods=["POST"])
@jwt_required()
@role_required(["service_provider", "business_basic", "business_premium"])
def upload_listing_image():
    listing_id = request.form.get("listing_id")
    image_type = request.form.get("image_type", "gallery")
    image = request.files.get("image")
    if not image:
        return jsonify({"error": "Image required"}), 400

    filename = secure_filename(image.filename)
    upload_subfolder = "static/images/listings"
    os.makedirs(upload_subfolder, exist_ok=True)
    filepath = os.path.join(upload_subfolder, filename)
    image.save(filepath)
    image_url = f"/static/images/listings/{filename}"

    conn = get_db()
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
@role_required(["service_provider", "business_basic", "business_premium"])
def get_listing(listing_id):
    user_phone = get_jwt().get("phone")
    conn = get_db()
    row = conn.execute(text("""
        SELECT id, business_name, category, city, state, latitude, longitude, description
        FROM listings
        WHERE id = :lid AND user_phone = :phone
    """), {"lid": listing_id, "phone": user_phone}).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row._mapping))


# =========================
# RATE BUSINESS (public)
# =========================
@listing_bp.route("/rate", methods=["POST"])
def rate_business():
    data = request.json if request.is_json else request.form.to_dict()
    listing_id = data.get("listing_id")
    rating = data.get("rating")
    if not listing_id or not rating:
        return jsonify({"error": "listing_id and rating required"}), 400

    conn = get_db()
    conn.execute(text("INSERT INTO reviews (listing_id, rating) VALUES (:lid, :rating)"),
                 {"lid": listing_id, "rating": rating})
    conn.execute(text("""
        UPDATE listings
        SET rating = (SELECT AVG(rating) FROM reviews WHERE listing_id = :lid),
            rating_count = rating_count + 1
        WHERE id = :lid
    """), {"lid": listing_id})
    conn.commit()
    return jsonify({"status": "success"})


# =========================
# TRACK CALL / WHATSAPP CLICKS (public)
# =========================
@listing_bp.route("/click-call/<int:listing_id>", methods=["POST"])
def track_call_click(listing_id):
    conn = get_db()
    conn.execute(text("UPDATE listings SET call_clicks = call_clicks + 1 WHERE id = :lid"), {"lid": listing_id})
    conn.commit()
    return jsonify({"status": "ok"})

@listing_bp.route("/click-whatsapp/<int:listing_id>", methods=["POST"])
def track_whatsapp_click(listing_id):
    conn = get_db()
    conn.execute(text("UPDATE listings SET whatsapp_clicks = whatsapp_clicks + 1 WHERE id = :lid"), {"lid": listing_id})
    conn.commit()
    return jsonify({"status": "ok"})


# =========================
# GET LISTING IMAGES (public)
# =========================
@listing_bp.route("/listing-images/<int:listing_id>")
def get_listing_images(listing_id):
    conn = get_db()
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
        page = request.args.get("page", 1, type=int)
        limit = 10
        offset = (page - 1) * limit

        conn = get_db()

        # Use a CTE to calculate distance once, then filter and order
        query = text("""
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
            ORDER BY featured DESC, premium DESC, verified DESC, distance ASC, rating DESC
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
                "phone": rm.get("phone"),
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
                "featured": bool(rm["featured"])
            })
        return jsonify({"listings": listings, "page": page, "count": len(listings)})
    except Exception as e:
        import traceback
        logger.error(traceback.format_exc())
        logger.info("BROWSE API ERROR:", e)
        return jsonify({"error": str(e)}), 500
    
# =========================
# ADD REVIEW (auth required)
# =========================
@listing_bp.route("/review", methods=["POST"])
@jwt_required()
def add_review():
    user_phone = get_jwt().get("phone")
    return add_review_service(request.json, user_phone)


# =========================
# GET REVIEWS (public)
# =========================
@listing_bp.route("/reviews/<int:listing_id>")
def get_reviews(listing_id):
    return get_reviews_service(listing_id)


# =========================
# PUBLIC LISTING DETAIL
# =========================
@listing_bp.route("/api/listing/<int:listing_id>")
def public_listing_detail(listing_id):
    conn = get_db()
    listing = conn.execute(text("SELECT * FROM listings WHERE id = :lid"), {"lid": listing_id}).fetchone()
    if not listing:
        return jsonify({"error": "Listing not found"}), 404

    image_row = conn.execute(
        text("SELECT image_url FROM listing_images WHERE listing_id = :lid LIMIT 1"),
        {"lid": listing_id}
    ).fetchone()

    data = dict(listing._mapping)
    data["image"] = image_row._mapping["image_url"] if image_row else None
    return jsonify({"listing": data})