import math
from datetime import datetime, timedelta
from database.init_db import get_db

# ===============================
# CREATE LISTING
# ===============================
def create_listing(data):
    conn = get_db()
    conn.execute("""
        INSERT INTO listings
        (user_id, listing_type, business_name, category, city, state,
         latitude, longitude, description, phone, whatsapp, website,
         logo_url, premium, sponsored, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1)
    """, (
        data["user_id"],
        data.get("listing_type", "business"),
        data["business_name"],
        data.get("category"),
        data.get("city"),
        data.get("state"),
        data["latitude"],
        data["longitude"],
        data.get("description"),
        data.get("phone"),
        data.get("whatsapp"),
        data.get("website"),
        data.get("logo_url"),
    ))
    conn.commit()


# ===============================
# GET USER LISTINGS
# ===============================
def get_user_listings(user_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,)).fetchall()
    return rows


# ===============================
# GET LISTING BY ID
# ===============================
def get_listing(listing_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM listings WHERE id = ?", (listing_id,)
    ).fetchone()
    return row


# ===============================
# UPDATE LISTING
# ===============================
def update_listing(listing_id, data):
    conn = get_db()
    conn.execute("""
        UPDATE listings
        SET business_name = ?,
            category = ?,
            city = ?,
            state = ?,
            description = ?,
            phone = ?,
            whatsapp = ?,
            website = ?
        WHERE id = ?
    """, (
        data["business_name"],
        data["category"],
        data["city"],
        data["state"],
        data["description"],
        data["phone"],
        data["whatsapp"],
        data["website"],
        listing_id
    ))
    conn.commit()


# ===============================
# DELETE LISTING
# ===============================
def delete_listing(listing_id):
    conn = get_db()
    conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    conn.commit()


# ===============================
# GET NEARBY LISTINGS (by grid)
# ===============================
def get_nearby_listings(lat_grid, lng_grid, category):
    conn = get_db()
    rows = conn.execute("""
        SELECT *
        FROM listings
        WHERE lat_grid = ?
          AND lng_grid = ?
          AND category = ?
          AND is_active = 1
    """, (lat_grid, lng_grid, category)).fetchall()
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
    conn = get_db()
    # row_factory is already set by get_db() – no need to set again
    listings = conn.execute("SELECT * FROM listings").fetchall()
    results = []
    for row in listings:
        lat = row["latitude"]
        lng = row["longitude"]
        distance = calculate_distance(user_lat, user_lng, lat, lng)
        if distance <= radius_km:
            item = dict(row)
            item["distance"] = round(distance, 2)
            results.append(item)
    results.sort(key=lambda x: x["distance"])
    return results


def update_sponsored_status():
    conn = get_db()
    conn.executescript("""
        UPDATE listings
        SET is_sponsored = 0
        WHERE id IN (
            SELECT listing_id
            FROM sponsored_ads
            WHERE end_date < CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


# --- Browse with filters ---
def browse_listings(location, category, page):
    try:
        limit = 10
        offset = (page - 1) * limit
        conn = get_db()

        query = """
            SELECT id, business_name, listing_type, category, city, state,
                   description, phone, whatsapp, is_verified, is_premium
            FROM listings
            WHERE is_active = 1
        """
        params = []

        if location:
            query += " AND city LIKE ?"
            params.append(f"%{location}%")
        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY is_premium DESC, id DESC"
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()

        # Count total matching items
        count_query = "SELECT COUNT(*) FROM listings WHERE is_active = 1"
        count_params = []
        if location:
            count_query += " AND city LIKE ?"
            count_params.append(f"%{location}%")
        if category:
            count_query += " AND category = ?"
            count_params.append(category)

        total = conn.execute(count_query, count_params).fetchone()[0]

        listings = []
        for r in rows:
            listings.append({
                "id": r["id"],
                "business_name": r["business_name"],
                "type": r["listing_type"],
                "category": r["category"],
                "city": r["city"],
                "state": r["state"],
                "description": r["description"],
                "phone": r["phone"],
                "whatsapp": r["whatsapp"],
                "verified": bool(r["is_verified"]),
                "premium": bool(r["is_premium"])
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
        print("BROWSE SERVICE ERROR:", e)
        return {"status": "error", "message": "Server error"}


# --- Admin service functions ---
def disable_listing_service(listing_id):
    conn = get_db()
    conn.execute("UPDATE listings SET is_active = 0 WHERE id = ?", (listing_id,))
    conn.commit()
    return {"status": "disabled"}


def verify_listing_service(listing_id):
    conn = get_db()
    conn.execute("UPDATE listings SET is_verified = 1 WHERE id = ?", (listing_id,))
    conn.commit()
    return {"status": "verified"}


def delete_listing_service(listing_id):
    conn = get_db()
    conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    conn.commit()
    return {"status": "deleted"}


def sponsor_listing_service(listing_id):
    conn = get_db()
    start = datetime.utcnow()
    end = start + timedelta(days=30)
    conn.execute("""
        INSERT INTO sponsored_ads (listing_id, plan, amount, start_date, end_date)
        VALUES (?, 'top_30_days', 999, ?, ?)
    """, (listing_id, start, end))
    conn.execute("UPDATE listings SET is_sponsored = 1 WHERE id = ?", (listing_id,))
    conn.commit()
    return {"status": "sponsored"}


# --- Review services (uses user_id instead of phone session) ---
def add_review_service(data, user_id):
    listing_id = data.get("listing_id")
    rating = data.get("rating")
    review = data.get("review", "")

    conn = get_db()
    # Fetch user phone from users table using user_id
    user_row = conn.execute("SELECT phone FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user_row:
        return {"error": "User not found"}

    user_phone = user_row["phone"]   # row is a sqlite3.Row – key access works

    conn.execute("""
        INSERT INTO reviews (listing_id, user_phone, rating, review)
        VALUES (?, ?, ?, ?)
    """, (listing_id, user_phone, rating, review))

    conn.execute("""
        UPDATE listings
        SET rating = (SELECT AVG(rating) FROM reviews WHERE listing_id = ?),
            total_reviews = (SELECT COUNT(*) FROM reviews WHERE listing_id = ?)
        WHERE id = ?
    """, (listing_id, listing_id, listing_id))
    conn.commit()

    return {"status": "review_added"}


def get_reviews_service(listing_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT r.user_phone,
               u.name AS user_name,
               r.rating,
               r.review,
               r.created_at
        FROM reviews r
        JOIN users u ON r.user_phone = u.phone
        WHERE r.listing_id = ?
        ORDER BY r.created_at DESC
    """, (listing_id,)).fetchall()
    return {"reviews": [dict(r) for r in rows]}