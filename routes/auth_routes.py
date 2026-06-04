import os
import secrets
import requests
import redis
import logging
logger = logging.getLogger(__name__)
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify, render_template, current_app
import random
import re
import string
from sqlalchemy import text
from redis_client import get_redis_client

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

# =================================
# HELPER: Get Message Central Token
# =================================
def get_message_central_token():
    """Fetch a fresh token from Message Central (valid for 24 hours)."""
    url = "https://cpaas.messagecentral.com/auth/v1/authentication/token"
    params = {
        "customerId": os.getenv("MESSAGE_CENTRAL_CUSTOMER_ID"),
        "key": os.getenv("MESSAGE_CENTRAL_API_KEY"),
        "scope": "NEW",
        "country": "91"
    }
    try:
        response = requests.get(url, headers={"accept": "*/*"}, params=params, timeout=10)
        response.raise_for_status()
        token = response.json().get("token")
        if token:
            current_app.logger.info("Message Central token acquired")
            return token
        else:
            current_app.logger.error("No token in response")
            return None
    except Exception as e:
        current_app.logger.exception(f"Token fetch failed: {e}")
        return None
    
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
        logger.info("🔥 REGISTER ERROR:", e)
        import traceback
        logger.error(traceback.format_exc())
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

    # 1. Generate a secure 6-digit OTP
    otp_code = f"{secrets.randbelow(1000000):06d}"

    # 2. Store OTP and expiry in Redis (5 minutes)
    redis_key = f"otp:{phone}"
    redis_client = get_redis_client()
    redis_client.setex(redis_key, 300, otp_code)  # 300 seconds = 5 minutes

    # 3. Get authentication token from Message Central
    auth_token = get_message_central_token()
    if not auth_token:
        # If token fails, delete the stored OTP to avoid inconsistency
        redis_client.delete(redis_key)
        return jsonify({"error": "OTP service authentication failed"}), 500

    # 4. Send OTP via Message Central VerifyNow API (v3)
    send_url = "https://cpaas.messagecentral.com/verification/v3/send"
    headers = {"authToken": auth_token}
    params = {
        "customerId": os.getenv("MESSAGE_CENTRAL_CUSTOMER_ID"),
        "countryCode": "91",
        "mobileNumber": phone,
        "flowType": "SMS",
        "otpLength": 6
    }

    try:
        response = requests.post(send_url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        resp_data = response.json()
        verification_id = resp_data.get("verificationId")

        # Store verification ID for later use (optional, but good for debugging)
        redis_client.setex(f"verification:{phone}", 300, verification_id)

        current_app.logger.info(f"OTP sent to {phone} via Message Central. Verification ID: {verification_id}")

        # Success – do NOT return OTP in response (security)
        return jsonify({
            "status": "success",
            "message": "OTP sent successfully"
        }), 200

    except requests.exceptions.RequestException as e:
        current_app.logger.exception(f"Message Central send failed: {e}")
        # Clean up stored OTP since sending failed
        redis_client.delete(redis_key)
        return jsonify({"error": "Failed to send OTP, please try again"}), 500
	
# =================================
# VERIFY OTP & LOGIN/REGISTER
# =================================
@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    data = request.get_json() or {}
    phone = data.get("phone")
    otp = str(data.get("otp"))
    name = data.get("name", "")
    remember_me = data.get("remember_me", False)

    if not phone or not otp:
        return jsonify({"error": "Phone and OTP required"}), 400

    # ---------- OTP validation using Redis ----------
    redis_client = get_redis_client()
    redis_key = f"otp:{phone}"
    stored_otp = redis_client.get(redis_key)

    # Allow a debug master OTP only if DEBUG is True (optional, remove in production)
    if current_app.config.get('DEBUG') and otp == "000000":
        # bypass OTP check
        pass
    else:
        if not stored_otp:
            return jsonify({"error": "No OTP requested or OTP expired"}), 400
        if stored_otp.decode('utf-8') != otp:
            return jsonify({"error": "Invalid OTP"}), 400
        # OTP is correct – delete it immediately (one‑time use)
        redis_client.delete(redis_key)

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

    # ---------- JWT expiry based on remember_me ----------
    if remember_me:
        access_expires = timedelta(days=30)
        refresh_expires = timedelta(days=365)
    else:
        access_expires = timedelta(minutes=15)
        refresh_expires = timedelta(days=7)

    access_token = create_access_token(
        identity=str(user_id),
        additional_claims={"role": role, "phone": phone},
        expires_delta=access_expires
    )
    refresh_token = create_refresh_token(
        identity=str(user_id),
        expires_delta=refresh_expires
    )

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

    # Attach tokens as secure cookies with matching max_age
    set_access_cookies(response, access_token, max_age=access_expires)
    set_refresh_cookies(response, refresh_token, max_age=refresh_expires)

    return response, 200

# =================================
# LOGOUT (stateless)
# =================================
from flask_jwt_extended import unset_jwt_cookies

@auth_bp.route("/logout", methods=["POST"])
def logout():
    response = jsonify({"message": "Logged out successfully"})
    unset_jwt_cookies(response)
    return response, 200


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