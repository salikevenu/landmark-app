from sqlalchemy import text
from database.init_db import get_db
from utils.geo_utils import calculate_distance

GRID_SIZE = 0.1

def find_nearby_listings(user_lat, user_lng, category, listing_type, sort_type, radius):
    lat_grid = int(user_lat / GRID_SIZE)
    lng_grid = int(user_lng / GRID_SIZE)
    grid_range = int(radius / 11) + 1

    conn = get_db()
    query = text("""
        SELECT id,
               business_name,
               listing_type,
               category,
               city,
               state,
               latitude,
               longitude,
               phone,
               whatsapp,
               description,
               rating,
               total_reviews,
               is_verified,
               is_premium,
               is_sponsored
        FROM listings
        WHERE is_active = 1
          AND lat_grid BETWEEN :lat_min AND :lat_max
          AND lng_grid BETWEEN :lng_min AND :lng_max
    """)
    rows = conn.execute(query, {
        "lat_min": lat_grid - grid_range,
        "lat_max": lat_grid + grid_range,
        "lng_min": lng_grid - grid_range,
        "lng_max": lng_grid + grid_range
    }).fetchall()

    results = []
    for row in rows:
        # Safe column access via _mapping
        distance = calculate_distance(user_lat, user_lng,
                                      row._mapping["latitude"],
                                      row._mapping["longitude"])
        if distance > radius:
            continue
        if listing_type and row._mapping["listing_type"] != listing_type:
            continue
        if category and row._mapping["category"] != category:
            continue

        results.append({
            "id": row._mapping["id"],
            "business_name": row._mapping["business_name"],
            "type": row._mapping["listing_type"],
            "category": row._mapping["category"],
            "city": row._mapping["city"],
            "state": row._mapping["state"],
            "latitude": row._mapping["latitude"],
            "longitude": row._mapping["longitude"],
            "phone": row._mapping["phone"],
            "whatsapp": row._mapping["whatsapp"],
            "description": row._mapping["description"],
            "rating": row._mapping["rating"],
            "reviews": row._mapping["total_reviews"],
            "verified": bool(row._mapping["is_verified"]),
            "premium": bool(row._mapping["is_premium"]),
            "sponsored": bool(row._mapping["is_sponsored"]),
            "distance_km": round(distance, 2)
        })

    # Sorting (same as before)
    if sort_type == "distance":
        results.sort(key=lambda x: x["distance_km"])
    elif sort_type == "rating":
        results.sort(key=lambda x: -x["rating"])
    elif sort_type == "trending":
        results.sort(key=lambda x: -x["reviews"])
    elif sort_type == "sponsored":
        results.sort(key=lambda x: -x["sponsored"])
    else:  # default "smart"
        results.sort(
            key=lambda x: (
                -x["sponsored"],
                -x["premium"],
                -x["rating"],
                -x["verified"],
                x["distance_km"]
            )
        )

    return results[:100]