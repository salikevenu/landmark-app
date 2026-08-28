import math
import logging
logger = logging.getLogger(__name__)
from datetime import datetime, timedelta
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from database.init_db import get_db_connection

# ===============================
# CREATE LISTING
# ===============================
def create_listing(data):
    """LEGACY / DISABLED. Live path: POST /api/listing/create-listing."""
    logger.error("LEGACY DISABLED: listing_service.create_listing must not insert listings")
    return None


# ===============================
# GET USER LISTINGS
# ===============================
def get_user_listings(user_id):
    conn = get_db_connection()
    rows = conn.execute(text("""
        SELECT *
        FROM listings
        WHERE user_id = :user_id
        ORDER BY id DESC
    """), {"user_id": user_id}).fetchall()
    return rows


# ===============================
# GET LISTING BY ID
# ===============================
def get_listing(listing_id):
    conn = get_db_connection()
    row = conn.execute(
        text("SELECT * FROM listings WHERE id = :id"), {"id": listing_id}
    ).fetchone()
    return row


# ===============================
# UPDATE LISTING
# ===============================
def update_listing(listing_id, data):
    """LEGACY / DISABLED. Live path: PUT /api/listing/update-listing/<id>."""
    logger.error("LEGACY DISABLED: listing_service.update_listing must not mutate listings")
    return None


# ===============================
# DELETE LISTING
# ===============================
def delete_listing(listing_id):
    """LEGACY / DISABLED. Live path: DELETE /api/listing/delete-listing/<id>."""
    logger.error("LEGACY DISABLED: listing_service.delete_listing must not delete listings")
    return None


# ===============================
# GET NEARBY LISTINGS (by grid)
# ===============================
def get_nearby_listings(lat_grid, lng_grid, category):
    conn = get_db_connection()
    rows = conn.execute(text("""
        SELECT id, business_name, category, city, state, latitude, longitude,
               user_phone, whatsapp, rating, is_verified, is_premium
        FROM listings
        WHERE lat_grid = :lat_grid
          AND lng_grid = :lng_grid
          AND category = :category
          AND is_active = 1
          AND status = 'approved'
    """), {
        "lat_grid": lat_grid,
        "lng_grid": lng_grid,
        "category": category
    }).fetchall()
    return rows


# --- Distance calculation helper ---
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def find_nearby(user_lat, user_lng, radius_km=10):
    """LEGACY / DISABLED. Live path: /api/nearby with approved listings only."""
    logger.error("LEGACY DISABLED: listing_service.find_nearby must not scan all listings")
    return []


def update_sponsored_status():
    """Expire stale listings.is_sponsored flags. Does not delete sponsored_ads."""
    from services.sponsorship import cleanup_expired_sponsorships
    return cleanup_expired_sponsorships()


# --- Browse with filters ---
def browse_listings(location, category, page):
    try:
        limit = 10
        offset = (page - 1) * limit
        conn = get_db_connection()

        query = text("""
            SELECT id, business_name, listing_type, category, city, state,
                   description, phone, whatsapp, is_verified, is_premium
            FROM listings
            WHERE is_active = 1
              AND status = 'approved'
            AND (:location IS NULL OR city LIKE :loc)
            AND (:category IS NULL OR category = :cat)
            ORDER BY is_premium DESC, id DESC
            LIMIT :lim OFFSET :off
        """)

        # Bind parameters
        params = {
            "location": location,
            "loc": f"%{location}%" if location else None,
            "category": category,
            "cat": category if category else None,
            "lim": limit,
            "off": offset
        }

        rows = conn.execute(query, params).fetchall()

        # Count total matching items (same filters without LIMIT/OFFSET)
        count_query = text("""
            SELECT COUNT(*)
            FROM listings
            WHERE is_active = 1
              AND status = 'approved'
            AND (:location IS NULL OR city LIKE :loc)
            AND (:category IS NULL OR category = :cat)
        """)
        count_params = {
            "location": location,
            "loc": f"%{location}%" if location else None,
            "category": category,
            "cat": category if category else None
        }
        total = conn.execute(count_query, count_params).fetchone()[0]

        listings = []
        for r in rows:
            listings.append({
                "id": r._mapping["id"],
                "business_name": r._mapping["business_name"],
                "type": r._mapping["listing_type"],
                "category": r._mapping["category"],
                "city": r._mapping["city"],
                "state": r._mapping["state"],
                "description": r._mapping["description"],
                "phone": r._mapping["phone"],
                "whatsapp": r._mapping["whatsapp"],
                "verified": bool(r._mapping["is_verified"]),
                "premium": bool(r._mapping["is_premium"])
            })

        return {
            "status": "success",
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit,
            "listings": listings
        }
    except Exception as e:
        logger.info("BROWSE SERVICE ERROR:", e)
        return {"status": "error", "message": "Server error"}


# --- Admin service functions ---
def disable_listing_service(listing_id):
    """LEGACY / DISABLED. Live path: admin_service.disable_listing_admin."""
    logger.error("LEGACY DISABLED: listing_service.disable_listing_service must not mutate listings")
    return {"error": "legacy_listing_disable_disabled"}


def verify_listing_service(listing_id):
    """LEGACY / DISABLED. Live path: admin_service.verify_listing_admin."""
    logger.error("LEGACY DISABLED: listing_service.verify_listing_service must not mutate listings")
    return {"error": "legacy_listing_verify_disabled"}


def delete_listing_service(listing_id):
    """LEGACY / DISABLED. Live path: admin_service.delete_listing_admin."""
    logger.error("LEGACY DISABLED: listing_service.delete_listing_service must not delete listings")
    return {"error": "legacy_listing_delete_disabled"}


def sponsor_listing_service(listing_id):
    """LEGACY / DISABLED. Live path: admin_service.sponsor_listing_admin (admin-granted, unpaid)."""
    logger.error(
        "LEGACY DISABLED: listing_service.sponsor_listing_service must not set sponsored flags"
    )
    return {"error": "legacy_listing_sponsor_disabled"}


def _as_int_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _recalc_listing_ratings(conn, listing_id):
    conn.execute(text("""
        UPDATE listings
        SET rating = (SELECT AVG(rating) FROM reviews WHERE listing_id = :lid),
            total_reviews = (SELECT COUNT(*) FROM reviews WHERE listing_id = :lid),
            rating_count = (SELECT COUNT(*) FROM reviews WHERE listing_id = :lid)
        WHERE id = :lid
    """), {"lid": listing_id})


# --- Review services: canonical identity is JWT user_id, never client phone/user_id ---
def add_review_service(data, user_id):
    payload = data or {}
    try:
        listing_id = int(payload.get("listing_id"))
    except (TypeError, ValueError):
        return {"error": "listing_id required", "_http": 400}
    try:
        rating = int(payload.get("rating"))
    except (TypeError, ValueError):
        return {"error": "rating required", "_http": 400}
    if rating < 1 or rating > 5:
        return {"error": "rating must be between 1 and 5", "_http": 400}
    review = str(payload.get("review") or "")[:2000]
    uid = _as_int_id(user_id)
    if uid is None:
        return {"error": "Authentication required", "_http": 401}

    conn = get_db_connection()
    try:
        listing = conn.execute(
            text("SELECT id FROM listings WHERE id = :lid AND status = 'approved' AND is_active = 1"),
            {"lid": listing_id},
        ).fetchone()
        if not listing:
            return {"error": "Listing not found", "_http": 404}

        user_row = conn.execute(
            text("SELECT id, phone FROM users WHERE id = :user_id AND COALESCE(is_active, 1) = 1"),
            {"user_id": uid},
        ).fetchone()
        if not user_row:
            return {"error": "User not found", "_http": 404}

        # Phone is denormalized from the authenticated user row. Client user_phone/user_id ignored.
        user_phone = user_row._mapping["phone"]

        existing = conn.execute(
            text("SELECT id FROM reviews WHERE listing_id = :lid AND user_id = :uid"),
            {"lid": listing_id, "uid": uid},
        ).fetchone()
        if existing:
            return {"error": "Review already exists", "_http": 409}

        conn.execute(text("""
            INSERT INTO reviews (listing_id, user_id, user_phone, rating, review)
            VALUES (:listing_id, :user_id, :user_phone, :rating, :review)
        """), {
            "listing_id": listing_id,
            "user_id": uid,
            "user_phone": user_phone,
            "rating": rating,
            "review": review,
        })
        _recalc_listing_ratings(conn, listing_id)
        conn.commit()
        return {"status": "review_added"}
    except IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"error": "Review already exists", "_http": 409}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_review_service(review_id, data, user_id):
    rid = _as_int_id(review_id)
    uid = _as_int_id(user_id)
    if rid is None or uid is None:
        return {"error": "Authentication required", "_http": 401}
    payload = data or {}
    fields = []
    params = {"rid": rid, "uid": uid}
    if payload.get("rating") is not None:
        try:
            rating = int(payload.get("rating"))
        except (TypeError, ValueError):
            return {"error": "rating required", "_http": 400}
        if rating < 1 or rating > 5:
            return {"error": "rating must be between 1 and 5", "_http": 400}
        fields.append("rating = :rating")
        params["rating"] = rating
    if "review" in payload:
        fields.append("review = :review")
        params["review"] = str(payload.get("review") or "")[:2000]
    if not fields:
        return {"error": "No review fields to update", "_http": 400}

    conn = get_db_connection()
    try:
        result = conn.execute(
            text(f"""
                UPDATE reviews
                SET {", ".join(fields)}
                WHERE id = :rid AND user_id = :uid
            """),
            params,
        )
        if result.rowcount != 1:
            return {"error": "Review not found", "_http": 404}
        listing = conn.execute(
            text("SELECT listing_id FROM reviews WHERE id = :rid AND user_id = :uid"),
            {"rid": rid, "uid": uid},
        ).fetchone()
        if listing:
            _recalc_listing_ratings(conn, listing._mapping["listing_id"])
        conn.commit()
        return {"status": "review_updated"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def delete_review_service(review_id, user_id):
    rid = _as_int_id(review_id)
    uid = _as_int_id(user_id)
    if rid is None or uid is None:
        return {"error": "Authentication required", "_http": 401}
    conn = get_db_connection()
    try:
        row = conn.execute(
            text("SELECT listing_id FROM reviews WHERE id = :rid AND user_id = :uid"),
            {"rid": rid, "uid": uid},
        ).fetchone()
        if not row:
            return {"error": "Review not found", "_http": 404}
        listing_id = row._mapping["listing_id"]
        result = conn.execute(
            text("DELETE FROM reviews WHERE id = :rid AND user_id = :uid"),
            {"rid": rid, "uid": uid},
        )
        if result.rowcount != 1:
            return {"error": "Review not found", "_http": 404}
        _recalc_listing_ratings(conn, listing_id)
        conn.commit()
        return {"status": "review_deleted"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_reviews_service(listing_id):
    conn = get_db_connection()
    try:
        listing = conn.execute(
            text("SELECT id FROM listings WHERE id = :lid AND status = 'approved' AND is_active = 1"),
            {"lid": listing_id},
        ).fetchone()
        if not listing:
            return {"reviews": []}
        rows = conn.execute(text("""
            SELECT COALESCE(u.name, 'Anonymous') AS user_name,
                   r.rating,
                   r.review,
                   r.owner_reply,
                   r.created_at
            FROM reviews r
            LEFT JOIN users u ON u.id = r.user_id
            WHERE r.listing_id = :listing_id
            ORDER BY r.created_at DESC
        """), {"listing_id": listing_id}).fetchall()
        reviews = [dict(r._mapping) for r in rows]
        return {"reviews": reviews}
    finally:
        try:
            conn.close()
        except Exception:
            pass