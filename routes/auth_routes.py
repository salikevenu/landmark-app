import os
import secrets
import requests
import redis
import logging
import base64  # <-- added
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
# =================================
# SEND OTP – using Message Central
# =================================
@auth_bp.route("/send-otp", methods=["POST"])
def send_otp():
    data = request.get_json() or {}
    phone = data.get("phone")

    if not phone:
        return jsonify({"error": "Phone number required"}), 400
    if not validate_phone(phone):
        return jsonify({"error": "Invalid phone number"}), 400

    redis_client = get_redis_client()
    pending_key = f"otp_pending:{phone}"
    
    # Rate limiting (optional)
    if redis_client.get(pending_key):
        return jsonify({"error": "OTP already sent. Please wait 60 seconds."}), 429

    # --- Step 1: Generate a fresh authentication token ---
    auth_token = get_message_central_token()
    if not auth_token:
        current_app.logger.error("Failed to obtain Message Central auth token")
        return jsonify({"error": "OTP service authentication failed"}), 500

    # --- Step 2: Request OTP sending ---
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
        current_app.logger.info(f"Send OTP status: {response.status_code}")
        current_app.logger.info(f"Send OTP response: {response.text}")

        if response.status_code == 200:
            resp_json = response.json()
            # Store the verificationId for later use
            if resp_json.get("verificationId"):
                redis_client.setex(f"verification_id:{phone}", 300, resp_json["verificationId"])
            redis_client.setex(pending_key, 300, "pending")
            return jsonify({"status": "success", "message": "OTP sent successfully"}), 200
        else:
            return jsonify({"error": "Failed to send OTP, please try again"}), 500
    except Exception as e:
        current_app.logger.exception(f"Send OTP error: {e}")
        return jsonify({"error": "Network error, please try again"}), 500

# =================================
# VERIFY OTP – using Message Central
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

    redis_client = get_redis_client()
    pending_key = f"otp_pending:{phone}"
    verification_key = f"verification_id:{phone}"

    if not redis_client.get(pending_key):
        return jsonify({"error": "No OTP requested or OTP expired"}), 400

    # --- Step 1: Generate a fresh authentication token ---
    auth_token = get_message_central_token()
    if not auth_token:
        current_app.logger.error("Failed to obtain Message Central auth token")
        return jsonify({"error": "OTP service authentication failed"}), 500

    # --- Step 2: Verify the OTP ---
    verify_url = "https://cpaas.messagecentral.com/verification/v3/verify"
    headers = {"authToken": auth_token}
    params = {
        "customerId": os.getenv("MESSAGE_CENTRAL_CUSTOMER_ID"),
        "countryCode": "91",
        "mobileNumber": phone,
        "otp": otp
    }
    
    # Add verificationId if available for better reliability
    stored_verification_id = redis_client.get(verification_key)
    if stored_verification_id:
        params["verificationId"] = stored_verification_id.decode('utf-8') if isinstance(stored_verification_id, bytes) else stored_verification_id

    try:
        response = requests.post(verify_url, headers=headers, params=params, timeout=10)
        current_app.logger.info(f"Verify OTP status: {response.status_code}")
        current_app.logger.info(f"Verify OTP response: {response.text}")
        resp_json = response.json() if response.text else {}

        if response.status_code == 200 and resp_json.get("isValid"):
            # OTP is valid
            redis_client.delete(pending_key)
            redis_client.delete(verification_key)

            # --- Step 3: User Lookup/Creation & Token Generation ---
            conn = get_db()
            user = conn.execute(
                text("SELECT id, name, role, referral_code FROM users WHERE phone = :phone"),
                {"phone": phone}
            ).fetchone()

            # ... (rest of your user creation/login logic remains unchanged) ...

        else:
            error_msg = resp_json.get("message", "Invalid OTP")
            return jsonify({"error": error_msg}), 400

    except Exception as e:
        current_app.logger.exception(f"Verify OTP error: {e}")
        return jsonify({"error": "Verification failed, please try again"}), 500

def get_message_central_token():
    """Generate a new auth token using Message Central API."""
    customer_id = os.getenv("MESSAGE_CENTRAL_CUSTOMER_ID")
    email = os.getenv("MESSAGE_CENTRAL_EMAIL")
    password = os.getenv("MESSAGE_CENTRAL_PASSWORD")

    if not all([customer_id, email, password]):
        current_app.logger.error("Missing Message Central credentials for token generation")
        return None
        
    base64_encoded_key = base64.b64encode(password.encode('utf-8')).decode('utf-8')
    params = {
        "customerId": customer_id,
        "key": base64_encoded_key,
        "scope": "NEW",
        "email": email
    }

    try:
        response = requests.get("https://cpaas.messagecentral.com/auth/v1/authentication/token", 
                               params=params, timeout=10)
        if response.status_code == 200:
            token = response.json().get("token")
            current_app.logger.info("Message Central token generated successfully")
            return token
        else:
            current_app.logger.error(f"Token generation failed: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        current_app.logger.exception(f"Token generation error: {e}")
        return None
            
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