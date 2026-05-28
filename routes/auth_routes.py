from flask import Blueprint, request, jsonify, render_template, current_app
from datetime import datetime, timedelta
import random
import re
import string
from sqlalchemy import text
import secrets
from database.init_db import get_db
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    set_access_cookies,
    set_refresh_cookies,
    jwt_required,
    get_jwt_identity,
)

auth_bp = Blueprint("auth", __name__)

# In-memory OTP storage (for demo; use Redis in production)
otp_storage = {}  # {phone: {"code": "123456", "expires": timestamp}}


# =================================
# HELPER: Generate unique referral code
# =================================
def generate_referral_code():
    """Generate a random 8-character alphanumeric code"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


# =================================
# HELPER: Validate phone number
# =================================
def validate_phone(phone):
    """Basic Indian phone number validation (10 digits, starts with 6-9)"""
    return bool(re.match(r'^[6-9]\d{9}$', phone))


# =========================
# REGISTER (GET = page, POST = API)
# =========================
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("public/register.html")

    try:
        data = request.get_json()
        phone = data.get("phone")
        name = data.get("name")
        ref_code = data.get("ref_code")  # referrer's referral code

        if not phone or not name:
            return jsonify({"error": "Phone and name required"}), 400

        conn = get_db()

        # Check if phone already exists
        existing = conn.execute(
            text("SELECT id FROM users WHERE phone = :phone"),
            {"phone": phone}
        ).fetchone()
        if existing:
            return jsonify({"error": "Phone already registered"}), 409

        # Generate a unique referral code for the new user
        new_referral_code = secrets.token_urlsafe(6).upper()
        # Ensure uniqueness (simple loop, though probability of collision is low)
        while conn.execute(text("SELECT id FROM users WHERE referral_code = :code"), {"code": new_referral_code}).fetchone():
            new_referral_code = secrets.token_urlsafe(6).upper()

        # Find referrer ID if a valid ref_code was provided
        referrer_id = None
        if ref_code:
            referrer = conn.execute(
                text("SELECT id FROM users WHERE referral_code = :ref_code"),
                {"ref_code": ref_code}
            ).fetchone()
            if referrer:
                referrer_id = referrer[0]

        # Insert new user
        result = conn.execute(text("""
            INSERT INTO users (phone, name, role, plan, business_limit, referral_code, referred_by)
            VALUES (:phone, :name, 'free', 'free', 0, :new_code, :referred_by)
            RETURNING id
        """), {
            "phone": phone,
            "name": name,
            "new_code": new_referral_code,
            "referred_by": referrer_id
        })
        user_id = result.fetchone()[0]
        conn.commit()

        # Generate JWT tokens
        access_token = create_access_token(
            identity=str(user_id),
            additional_claims={"role": "free", "phone": phone}
        )
        refresh_token = create_refresh_token(identity=str(user_id))

        return jsonify({
            "message": "User created successfully",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user": {
                "id": user_id,
                "phone": phone,
                "role": "free",
                "referral_code": new_referral_code
            }
        }), 201

    except Exception as e:
        print("🔥 REGISTER ERROR:", e)
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Internal server error"}), 500
    
# =================================
# SEND OTP
# =================================
@auth_bp.route("/send-otp", methods=["POST"])
def send_otp():
    data = request.get_json() or {}
    phone = data.get("phone")

    if not phone:
        return jsonify({"error": "Phone number required"}), 400

    if not validate_phone(phone):
        return jsonify({"error": "Invalid phone number"}), 400

    otp_code = str(random.randint(100000, 999999))
    otp_storage[phone] = {
        "code": otp_code,
        "expires": datetime.utcnow() + timedelta(minutes=5)
    }

    # DEV MODE: print OTP in terminal
    print(f"🔥 TEST OTP for {phone}: {otp_code}")

    return jsonify({
        "status": "success",
        "message": "OTP generated (check terminal)",
        "otp": otp_code   # for frontend testing only – remove in production
    }), 200


# =================================
# VERIFY OTP & LOGIN/REGISTER
# =================================
@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    data = request.get_json() or {}
    phone = data.get("phone")
    otp = str(data.get("otp"))
    name = data.get("name", "")

    if not phone or not otp:
        return jsonify({"error": "Phone and OTP required"}), 400

    # ---------- OTP validation ----------
    if current_app.config['DEBUG'] and otp == "000000":
        # Test bypass – skip all OTP checks
        pass
    else:
        stored = otp_storage.get(phone)
        if not stored:
            return jsonify({"error": "No OTP requested"}), 400
        if datetime.utcnow() > stored["expires"]:
            del otp_storage[phone]
            return jsonify({"error": "OTP expired"}), 400
        if stored["code"] != otp:
            return jsonify({"error": "Invalid OTP"}), 400
        # OTP valid → delete it
        del otp_storage[phone]

    # ---------- Find or create user ----------
    conn = get_db()
    user = conn.execute(
        text("SELECT id, name, role, referral_code FROM users WHERE phone = :phone"),
        {"phone": phone}
    ).fetchone()

    if user:
        user_id = user._mapping["id"]
        role = user._mapping["role"]
        referral_code = user._mapping["referral_code"]
    else:
        referral_code = generate_referral_code()
        result = conn.execute(text("""
            INSERT INTO users (phone, name, role, referral_code, created_at)
            VALUES (:phone, :name, 'free', :referral_code, CURRENT_TIMESTAMP)
            RETURNING id
        """), {
            "phone": phone,
            "name": name,
            "referral_code": referral_code
        })
        user_id = result.fetchone()[0]
        conn.commit()
        role = "free"

    # ---------- Create JWT ----------
    access_token = create_access_token(
        identity=str(user_id),
        additional_claims={"role": role, "phone": phone}
    )
    refresh_token = create_refresh_token(identity=str(user_id))

    # Build response
    response = jsonify({
        "status": "success",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {
            "id": user_id,
            "phone": phone,
            "role": role,
            "referral_code": referral_code
        }
    })

    # Attach tokens as secure cookies
    set_access_cookies(response, access_token)
    set_refresh_cookies(response, refresh_token)

    return response, 200


# =================================
# LOGOUT (stateless)
# =================================
@auth_bp.route("/logout", methods=["POST"])
def logout():
    # JWT is stateless – client must delete tokens
    return jsonify({"message": "Logged out successfully"}), 200


# =================================
# GET CURRENT USER (protected)
# =================================
@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def get_current_user():
    user_id = get_jwt_identity()
    conn = get_db()
    user = conn.execute(
        text("SELECT id, phone, name, role, referral_code FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()

    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify(dict(user._mapping)), 200


# =================================
# LOGIN PAGE (public)
# =================================
@auth_bp.route("/public/login")
def auth_login_page():
    return render_template("public/login.html")