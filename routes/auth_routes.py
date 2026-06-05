import os
import secrets
import requests
import logging
import base64
import random
import re
import string  # <-- added missing import
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, render_template, current_app
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
    unset_jwt_cookies
)

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

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


# ------------------------------
# Helper: Generate Message Central token
# ------------------------------
def get_message_central_token():
    """Obtain a fresh auth token using your Customer ID, Email and Password."""
    customer_id = os.getenv("MESSAGE_CENTRAL_CUSTOMER_ID")
    email = os.getenv("MESSAGE_CENTRAL_EMAIL")
    password = os.getenv("MESSAGE_CENTRAL_PASSWORD")

    if not all([customer_id, email, password]):
        current_app.logger.error("Missing Message Central token credentials (CUSTOMER_ID, EMAIL, PASSWORD)")
        return None

    # Base64 encode the account password
    encoded_key = base64.b64encode(password.encode('utf-8')).decode('utf-8')
    url = "https://cpaas.messagecentral.com/auth/v1/authentication/token"
    params = {
        "customerId": customer_id,
        "key": encoded_key,
        "scope": "NEW",
        "email": email
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        current_app.logger.info(f"Token generation status: {resp.status_code}")
        current_app.logger.info(f"Token generation response: {resp.text}")
        if resp.status_code == 200:
            token = resp.json().get("token")
            if token:
                return token
        current_app.logger.error("Token generation failed")
        return None
    except Exception as e:
        current_app.logger.exception(f"Token generation error: {e}")
        return None

# ------------------------------
# Send OTP (always use a fresh token)
# ------------------------------
@auth_bp.route("/send-otp", methods=["POST"])
def send_otp():
    data = request.get_json() or {}
    phone = data.get("phone")
    if not phone or not validate_phone(phone):
        return jsonify({"error": "Valid phone number required"}), 400

    redis_client = get_redis_client()
    pending_key = f"otp_pending:{phone}"
    if redis_client.get(pending_key):
        return jsonify({"error": "OTP already sent. Please wait 60 seconds."}), 429

    # 1. Get fresh token
    auth_token = get_message_central_token()
    if not auth_token:
        return jsonify({"error": "OTP service authentication failed"}), 500

    # 2. Request OTP
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
        resp = requests.post(send_url, headers=headers, params=params, timeout=15)
        current_app.logger.info(f"Send OTP status: {resp.status_code}")
        current_app.logger.info(f"Send OTP body: {resp.text}")
        if resp.status_code == 200:
            data = resp.json()
            ver_id = data.get("data", {}).get("verificationId") or data.get("verificationId")
            if ver_id:
                redis_client.setex(f"verification_id:{phone}", 300, str(ver_id))
            redis_client.setex(pending_key, 300, "pending")
            return jsonify({"status": "success", "message": "OTP sent"}), 200
        else:
            return jsonify({"error": "Failed to send OTP"}), 500
    except Exception as e:
        current_app.logger.exception(f"Send OTP error: {e}")
        return jsonify({"error": "Network error"}), 500

# ------------------------------
# Verify OTP (use a fresh token)
# ------------------------------
@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    data = request.get_json() or {}
    phone = data.get("phone")
    otp = str(data.get("otp"))
    name = data.get("name", "")
    remember_me = data.get("remember_me", False)

    if not phone or not otp:
        return jsonify({"error": "Phone and OTP required"}), 400

    redis_client = get_redis_client()
    pending_key = f"otp_pending:{phone}"
    verification_key = f"verification_id:{phone}"

    if not redis_client.get(pending_key):
        return jsonify({"error": "No OTP requested or OTP expired"}), 400

    # 1. Get a fresh token
    auth_token = get_message_central_token()
    if not auth_token:
        return jsonify({"error": "OTP service authentication failed"}), 500

    # 2. Verify OTP
    verify_url = "https://cpaas.messagecentral.com/verification/v3/verify"
    headers = {"authToken": auth_token}
    params = {
        "customerId": os.getenv("MESSAGE_CENTRAL_CUSTOMER_ID"),
        "countryCode": "91",
        "mobileNumber": phone,
        "otp": otp
    }
    stored_ver_id = redis_client.get(verification_key)
    if stored_ver_id:
        stored_ver_id = stored_ver_id.decode() if isinstance(stored_ver_id, bytes) else stored_ver_id
        params["verificationId"] = stored_ver_id

    try:
        resp = requests.post(verify_url, headers=headers, params=params, timeout=10)
        current_app.logger.info(f"Verify OTP status: {resp.status_code}")
        current_app.logger.info(f"Verify OTP body: {resp.text}")
        if resp.status_code == 200:
            resp_json = resp.json()
            if resp_json.get("isValid"):
                # OTP correct
                redis_client.delete(pending_key)
                redis_client.delete(verification_key)

                # Find or create user, issue JWT
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

                access_expires = timedelta(days=30) if remember_me else timedelta(minutes=15)
                refresh_expires = timedelta(days=365) if remember_me else timedelta(days=7)
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
                set_access_cookies(response, access_token, max_age=access_expires)
                set_refresh_cookies(response, refresh_token, max_age=refresh_expires)
                return response, 200
            else:
                return jsonify({"error": "Invalid OTP"}), 400
        else:
            current_app.logger.error(f"Verify OTP HTTP {resp.status_code}: {resp.text}")
            return jsonify({"error": "Verification failed, please try again"}), 500
    except Exception as e:
        current_app.logger.exception(f"Verify OTP exception: {e}")
        return jsonify({"error": "Verification error"}), 500


# =================================
# LOGOUT (stateless)
# =================================
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