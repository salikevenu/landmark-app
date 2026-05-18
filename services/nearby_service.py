from database.init_db import get_db
from utils.geo_utils import calculate_distance

GRID_SIZE = 0.1

def find_nearby_listings(user_lat, user_lng, category, listing_type, sort_type, radius):
    lat_grid = int(user_lat / GRID_SIZE)
    lng_grid = int(user_lng / GRID_SIZE)
    grid_range = int(radius / 11) + 1

    conn = get_db()
    query = """
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
          AND lat_grid BETWEEN ? AND ?
          AND lng_grid BETWEEN ? AND ?
    """
    rows = conn.execute(query, (
        lat_grid - grid_range,
        lat_grid + grid_range,
        lng_grid - grid_range,
        lng_grid + grid_range
    )).fetchall()

    results = []
    for row in rows:
        distance = calculate_distance(user_lat, user_lng, row["latitude"], row["longitude"])
        if distance > radius:
            continue
        if listing_type and row["listing_type"] != listing_type:
            continue
        if category and row["category"] != category:
            continue

        results.append({
            "id": row["id"],
            "business_name": row["business_name"],
            "type": row["listing_type"],
            "category": row["category"],
            "city": row["city"],
            "state": row["state"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "phone": row["phone"],
            "whatsapp": row["whatsapp"],
            "description": row["description"],
            "rating": row["rating"],
            "reviews": row["total_reviews"],
            "verified": bool(row["is_verified"]),
            "premium": bool(row["is_premium"]),
            "sponsored": bool(row["is_sponsored"]),
            "distance_km": round(distance, 2)
        })

    # Sorting
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