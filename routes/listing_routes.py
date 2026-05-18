import os
import time
import traceback
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from werkzeug.utils import secure_filename
from functools import wraps

from database.init_db import get_db
from services.listing_service import add_review_service, get_reviews_service

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
        return False          # free users can't create listings
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
@listing_bp.route("/api/create-listing", methods=["POST"])
@jwt_required()
def api_create_listing():
    try:
        user_id = get_jwt_identity()
        claims = get_jwt()
        user_phone = claims.get("phone")

        # 1. Get fresh user data using shared connection
        conn = get_db()
        user = conn.execute(
            "SELECT id, role, plan, subscription_expiry, is_active FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not user or not user["is_active"]:
            return jsonify({"error": "User not found or inactive"}), 404

        # 2. Check subscription
        if not is_subscription_active(dict(user)):
            return jsonify({"error": "Active subscription required"}), 403

        # 3. Count existing listings and enforce plan limits
        plan = user["plan"]
        listing_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM listings WHERE user_id = ?", (user_id,)
        ).fetchone()["cnt"]

        if plan == "business_basic" and listing_count >= 1:
            return jsonify({"error": "Basic plan allows only 1 listing. Upgrade to Premium."}), 403
        elif plan == "business_premium" and listing_count >= 3:
            return jsonify({"error": "Premium plan allows up to 3 listings."}), 403
        elif plan == "service_provider" and listing_count >= 10:
            return jsonify({"error": "Service provider limit reached (10 listings)."}), 403

        # 4. Process form data
        business_name = request.form.get("business_name")
        category = request.form.get("category")
        latitude = request.form.get("latitude")
        longitude = request.form.get("longitude")

        if not business_name or not category:
            return jsonify({"success": False, "error": "Business name and category required"}), 400
        if not latitude or not longitude:
            return jsonify({"success": False, "error": "Location required"}), 400

        # Insert listing
        cur = conn.execute("""
            INSERT INTO listings (
                user_id, user_phone, listing_type, business_name, category,
                city, state, latitude, longitude,
                description, whatsapp, website, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            user_phone,
            request.form.get("listing_type", "business"),
            business_name,
            category,
            request.form.get("city", ""),
            request.form.get("state", ""),
            latitude,
            longitude,
            request.form.get("description", ""),
            request.form.get("whatsapp", ""),
            request.form.get("website", ""),
            "pending"
        ))
        listing_id = cur.lastrowid

        # Image uploads – use config UPLOAD_FOLDER
        upload_dir = current_app.config['UPLOAD_FOLDER']
        images = request.files.getlist("images")
        for img in images:
            if img and img.filename:
                filename = f"{int(time.time()*1000)}_{secure_filename(img.filename)}"
                path = os.path.join(upload_dir, filename)
                img.save(path)
                conn.execute(
                    "INSERT INTO listing_images (listing_id, image_url, image_type) VALUES (?, ?, ?)",
                    (listing_id, f"/static/uploads/{filename}", "shop")
                )

        # Optional video
        video = request.files.get("video")
        if video and video.filename:
            filename = f"{int(time.time()*1000)}_{secure_filename(video.filename)}"
            path = os.path.join(upload_dir, filename)
            video.save(path)
            conn.execute(
                "UPDATE listings SET video=? WHERE id=?",
                (f"/static/uploads/{filename}", listing_id)
            )

        conn.commit()
        return jsonify({
            "success": True,
            "message": "Listing submitted for review",
            "listing_id": listing_id
        }), 201

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": "Internal Server Error"}), 500


# =========================
# ADMIN PENDING LISTINGS (HTML page)
# =========================
@listing_bp.route("/admin/listings")
@jwt_required()
@role_required(["admin"])
def admin_listings():
    conn = get_db()
    listings = conn.execute("SELECT * FROM listings WHERE status='pending'").fetchall()
    return render_template("admin/listings.html", listings=listings)


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
    rows = conn.execute("""
        SELECT l.*, 
            (SELECT image_url FROM listing_images WHERE listing_id = l.id LIMIT 1) as image_url
        FROM listings l
        WHERE l.user_id = ?
        ORDER BY l.id DESC
    """, (user_id,)).fetchall()

    listings = []
    for r in rows:
        listing = dict(r)
        listings.append(listing)
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
    # Check ownership
    listing = conn.execute(
        "SELECT id FROM listings WHERE id=? AND user_id=?", (listing_id, user_id)
    ).fetchone()
    if not listing:
        return jsonify({"error": "Not found or unauthorized"}), 404

    conn.execute("""
        UPDATE listings
        SET business_name=?, category=?, city=?, state=?, description=?
        WHERE id=?
    """, (data.get("business_name"), data.get("category"), data.get("city"),
          data.get("state"), data.get("description"), listing_id))
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
        "SELECT id FROM listings WHERE id=? AND user_phone=?", (listing_id, user_phone)
    ).fetchone()
    if not listing:
        return jsonify({"error": "Not found or unauthorized"}), 404

    conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
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
    # Use images/listings subfolder
    upload_subfolder = "static/images/listings"
    os.makedirs(upload_subfolder, exist_ok=True)
    filepath = os.path.join(upload_subfolder, filename)
    image.save(filepath)
    image_url = f"/static/images/listings/{filename}"

    conn = get_db()
    conn.execute(
        "INSERT INTO listing_images (listing_id, image_url, image_type) VALUES (?, ?, ?)",
        (listing_id, image_url, image_type)
    )
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
    row = conn.execute("""
        SELECT id, business_name, category, city, state, latitude, longitude, description
        FROM listings
        WHERE id=? AND user_phone=?
    """, (listing_id, user_phone)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


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
    conn.execute("INSERT INTO reviews (listing_id, rating) VALUES (?, ?)", (listing_id, rating))
    conn.execute("""
        UPDATE listings
        SET rating = (SELECT AVG(rating) FROM reviews WHERE listing_id=?),
            rating_count = rating_count + 1
        WHERE id=?
    """, (listing_id, listing_id))
    conn.commit()
    return jsonify({"status": "success"})


# =========================
# TRACK CALL / WHATSAPP CLICKS (public)
# =========================
@listing_bp.route("/click-call/<int:listing_id>", methods=["POST"])
def track_call_click(listing_id):
    conn = get_db()
    conn.execute("UPDATE listings SET call_clicks = call_clicks + 1 WHERE id=?", (listing_id,))
    conn.commit()
    return jsonify({"status": "ok"})

@listing_bp.route("/click-whatsapp/<int:listing_id>", methods=["POST"])
def track_whatsapp_click(listing_id):
    conn = get_db()
    conn.execute("UPDATE listings SET whatsapp_clicks = whatsapp_clicks + 1 WHERE id=?", (listing_id,))
    conn.commit()
    return jsonify({"status": "ok"})


# =========================
# GET LISTING IMAGES (public)
# =========================
@listing_bp.route("/listing-images/<int:listing_id>")
def get_listing_images(listing_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT image_url, image_type FROM listing_images WHERE listing_id=?", (listing_id,)
    ).fetchall()
    return jsonify({
        "images": [{"image_url": r["image_url"], "type": r["image_type"]} for r in rows]
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
@listing_bp.route("/api/browse")
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
        query = """
            SELECT l.*, 
                (6371 * acos(
                    cos(radians(?)) *
                    cos(radians(l.latitude)) *
                    cos(radians(l.longitude) - radians(?)) +
                    sin(radians(?)) *
                    sin(radians(l.latitude))
                )) AS distance,
                COALESCE(l.verified, 0) as verified,
                COALESCE(l.premium, 0) as premium,
                COALESCE(l.featured, 0) as featured,
                (SELECT image_url FROM listing_images WHERE listing_id = l.id LIMIT 1) as main_image
            FROM listings l
            WHERE l.status = 'approved'
        """
        params = [lat or 0, lng or 0, lat or 0]

        if search:
            query += " AND l.business_name LIKE ?"
            params.append(f"%{search}%")
        if category:
            query += " AND l.category = ?"
            params.append(category)
        if location:
            query += " AND l.city LIKE ?"
            params.append(f"%{location}%")
        if lat and lng and distance:
            query += " HAVING distance <= ?"
            params.append(distance)

        query += """
            ORDER BY l.featured DESC, l.premium DESC, l.verified DESC, distance ASC, l.rating DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        listings = []
        for r in rows:
            listings.append({
                "id": r["id"],
                "business_name": r["business_name"],
                "category": r["category"],
                "city": r["city"],
                "state": r["state"],
                "phone": r.get("phone"),
                "whatsapp": r.get("whatsapp"),
                "image": r["main_image"] or "/static/default.jpg",
                "video": r.get("video"),
                "rating": r["rating"] or 4.0,
                "rating_count": r["rating_count"] or 0,
                "distance": r["distance"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "verified": bool(r["verified"]),
                "premium": bool(r["premium"]),
                "featured": bool(r["featured"])
            })
        return jsonify({"listings": listings, "page": page, "count": len(listings)})
    except Exception as e:
        print("BROWSE API ERROR:", e)
        return jsonify({"error": "Server error"}), 500


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
    # Service layer will use its own get_db() (to be reviewed later)
    return get_reviews_service(listing_id)

@listing_bp.route("/api/listing/<int:listing_id>")
def public_listing_detail(listing_id):
    conn = get_db()
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not listing:
        return jsonify({"error": "Listing not found"}), 404

    # Grab the first image
    image_row = conn.execute(
        "SELECT image_url FROM listing_images WHERE listing_id = ? LIMIT 1",
        (listing_id,)
    ).fetchone()

    data = dict(listing)
    data["image"] = image_row["image_url"] if image_row else None
    return jsonify({"listing": data})