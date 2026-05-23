# services/user_service.py
import random
import string
from datetime import datetime
from sqlalchemy import text
from database.init_db import get_db

GRID_SIZE = 0.1


def _generate_referral_code(length=8):
    """Simple random referral code generator."""
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=length))


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute(
        text("SELECT * FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    return dict(row._mapping) if row else None


def get_user_by_phone(phone):
    conn = get_db()
    row = conn.execute(
        text("SELECT * FROM users WHERE phone = :phone"),
        {"phone": phone}
    ).fetchone()
    return dict(row._mapping) if row else None


def create_user(phone, name, **kwargs):
    """
    Create a new user.
    Required: phone, name
    Optional kwargs: role, plan, referral_code, referred_by, device_id, ip_address, language
    Returns dict with user id or error.
    """
    conn = get_db()
    # Check duplicate
    if get_user_by_phone(phone):
        return {"error": "Phone already registered"}, 409

    # Defaults
    role = kwargs.get("role", "user")
    plan = kwargs.get("plan", "free")
    referral_code = kwargs.get("referral_code") or _generate_referral_code()
    referred_by = kwargs.get("referred_by")  # can be None
    device_id = kwargs.get("device_id")
    ip_address = kwargs.get("ip_address")
    language = kwargs.get("language", "en")
    created_at = datetime.utcnow()

    # Insert
    result = conn.execute(text("""
        INSERT INTO users
            (phone, name, role, plan, referral_code, referred_by,
             device_id, ip_address, language, created_at)
        VALUES
            (:phone, :name, :role, :plan, :referral_code, :referred_by,
             :device_id, :ip_address, :language, :created_at)
        RETURNING id
    """), {
        "phone": phone,
        "name": name,
        "role": role,
        "plan": plan,
        "referral_code": referral_code,
        "referred_by": referred_by,
        "device_id": device_id,
        "ip_address": ip_address,
        "language": language,
        "created_at": created_at
    })
    new_id = result.fetchone()[0]
    conn.commit()
    return {
        "id": new_id,
        "phone": phone,
        "name": name,
        "role": role,
        "plan": plan,
        "referral_code": referral_code
    }


def update_user(user_id, data):
    """
    Update basic profile fields.
    Allowed fields: name, language, device_id, ip_address
    (role/plan changes should go through admin service)
    """
    allowed = {"name", "language", "device_id", "ip_address"}
    set_clauses = []
    params = {"uid": user_id}
    for key in allowed:
        if key in data:
            set_clauses.append(f"{key} = :{key}")
            params[key] = data[key]
    if not set_clauses:
        return {"error": "No valid fields to update"}

    conn = get_db()
    query = text(f"UPDATE users SET {', '.join(set_clauses)} WHERE id = :uid")
    conn.execute(query, params)
    conn.commit()
    return {"status": "updated"}


def update_location(user_id, latitude, longitude):
    """Update user location and grid indices."""
    lat_grid = int(latitude / GRID_SIZE)
    lng_grid = int(longitude / GRID_SIZE)
    conn = get_db()
    conn.execute(text("""
        UPDATE users
        SET latitude = :lat, longitude = :lng,
            lat_grid = :lat_grid, lng_grid = :lng_grid
        WHERE id = :uid
    """), {
        "lat": latitude,
        "lng": longitude,
        "lat_grid": lat_grid,
        "lng_grid": lng_grid,
        "uid": user_id
    })
    conn.commit()
    return {"status": "location_updated"}


def deactivate_user(user_id):
    conn = get_db()
    conn.execute(text("UPDATE users SET is_active = 0 WHERE id = :uid"), {"uid": user_id})
    conn.commit()
    return {"status": "deactivated"}


def activate_user(user_id):
    conn = get_db()
    conn.execute(text("UPDATE users SET is_active = 1 WHERE id = :uid"), {"uid": user_id})
    conn.commit()
    return {"status": "activated"}


def get_user_referral_stats(user_id):
    """Count how many users were referred by this user."""
    conn = get_db()
    count = conn.execute(
        text("SELECT COUNT(*) FROM users WHERE referred_by = :uid"),
        {"uid": user_id}
    ).scalar()
    return {"referral_count": count}


def update_wallet_balance(user_id, amount):
    """
    Add amount (can be negative) to wallet_balance.
    For complete wallet operations, use wallet_service.
    """
    conn = get_db()
    conn.execute(text("""
        UPDATE users SET wallet_balance = wallet_balance + :amount WHERE id = :uid
    """), {"amount": amount, "uid": user_id})
    conn.commit()
    return {"status": "balance_updated"}


def list_users(page=1, limit=20, search=None):
    """Admin list users with pagination and optional search on phone/name."""
    offset = (page - 1) * limit
    conn = get_db()
    base_where = "WHERE 1=1"
    params = {"limit": limit, "offset": offset}

    if search:
        base_where += " AND (phone LIKE :search OR name ILIKE :search)"
        params["search"] = f"%{search}%"

    rows = conn.execute(text(f"""
        SELECT id, phone, name, role, plan, is_active, created_at
        FROM users
        {base_where}
        ORDER BY id DESC
        LIMIT :limit OFFSET :offset
    """), params).fetchall()

    total = conn.execute(
        text(f"SELECT COUNT(*) FROM users {base_where}"), params
    ).scalar()

    users_list = [dict(r._mapping) for r in rows]
    return {
        "users": users_list,
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit
    }