import os
import sys
import random
import re
import string
import logging
from datetime import datetime, timedelta

import requests
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import text
from dotenv import load_dotenv

from database.init_db import get_db
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    set_access_cookies,
    set_refresh_cookies,
    jwt_required,
    get_jwt_identity,
    unset_jwt_cookies,
)

from dotenv import load_dotenv

load_dotenv()  # Ensure environment variables are loaded

# Debug: Print the value to confirm it's loaded
print(f"🔍 DEBUG_SMS value in routes/auth.py: {os.getenv('DEBUG_SMS')}")

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

# =================================
# OTP STORAGE
# In-memory dict — fine for a single dev process. In production with more
# than one worker/process (Gunicorn, Waitress threads across processes,
# multiple Render instances) this MUST be moved to Redis, since each
# worker would otherwise have its own separate otp_storage dict.
# =================================
otp_storage = {}

MAX_OTP_ATTEMPTS = 5
OTP_EXPIRY_MINUTES = 10
COUNTRY_CODE = os.getenv("MESSAGE_CENTRAL_COUNTRY", "91")

def send_otp_via_message_central(full_phone, raw_phone, otp):
    """Fallback: Print OTP to console and logs for testing"""
    print(f"\n🔴🔴🔴 DEBUG MODE - OTP for {full_phone}: {otp} 🔴🔴🔴\n", flush=True)
    logger.warning(f"DEBUG MODE - OTP for {full_phone}: {otp}")
    return True, "OTP sent (DEBUG MODE - check console)"
# =================================
# HELPER FUNCTIONS
# =================================

def generate_otp():
    """Generate a 6-digit OTP."""
    return str(random.randint(100000, 999999))


def generate_referral_code():
    """Generate a random 8-character alphanumeric referral code."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


def validate_phone(phone):
    """Basic Indian mobile number validation (10 digits, starts with 6-9)."""
    return bool(re.match(r'^[6-9]\d{9}$', phone))


def clean_phone(raw_phone):
    """Strip everything except digits, then take the last 10 digits.
    Handles numbers pasted with +91, spaces, dashes, etc."""
    digits = ''.join(filter(str.isdigit, raw_phone or ''))
    return digits[-10:] if len(digits) >= 10 else digits


def _new_otp_record(otp):
    return {
        "otp": otp,
        "expires_at": datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES),
        "created_at": datetime.now(),
        "attempts": 0,
    }


def get_message_central_token():
    """Get authentication token from Message Central.

    If MESSAGE_CENTRAL_AUTH_TOKEN is set in the environment, it's used
    directly (a long-lived token generated from the Message Central
    dashboard) and the token-fetch API call below is skipped entirely.
    """
    static_token = os.getenv("MESSAGE_CENTRAL_AUTH_TOKEN")
    print(f"DEBUG: static_token from env present: {bool(static_token)}", flush=True)
    if static_token:
        return static_token

    # Fallback: fetch a fresh token using a static API key (older/alternate flow)
    url = "https://cpaas.messagecentral.com/auth/v1/authentication/token"
    params = {
        "customerId": os.getenv("MESSAGE_CENTRAL_CUSTOMER_ID"),
        "key": os.getenv("MESSAGE_CENTRAL_KEY"),
        "scope": "NEW",
        "country": COUNTRY_CODE,
        "email": os.getenv("MESSAGE_CENTRAL_EMAIL"),
    }
    headers = {"accept": "*/*"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code == 200:
            token = response.json().get("data", {}).get("token")
            if token:
                logger.info("Message Central token obtained successfully")
                return token
            logger.error("No token in Message Central response")
            return None
        logger.error(f"Message Central token error: {response.status_code} - {response.text}")
        return None
    except Exception as e:
        logger.error(f"Message Central token exception: {e}")
        return None

# routes/auth.py - Replace this function

def send_otp_via_message_central(full_phone, raw_phone, otp):
    """Send OTP via Message Central API."""
    try:
        print(f"\n🔍 DEBUG: send_otp_via_message_central called for {full_phone}", flush=True)
        
        # Check for debug mode - FORCE it if DEBUG_SMS is True
        debug_mode = os.getenv("DEBUG_SMS", "False").lower() == "true"
        print(f"🔍 DEBUG: DEBUG_SMS = {debug_mode}", flush=True)
        
        if debug_mode:
            print(f"\n🔴🔴🔴 DEBUG MODE - OTP for {full_phone}: {otp} 🔴🔴🔴\n", flush=True)
            logger.warning(f"DEBUG MODE - OTP for {full_phone}: {otp}")
            return True, "OTP sent (DEBUG MODE)"
        
        # If not in debug mode, try real SMS
        auth_token = get_message_central_token()
        print(f"🔍 DEBUG: auth_token obtained: {bool(auth_token)}", flush=True)
        
        if not auth_token:
            return False, "SMS service unavailable - check Message Central credentials"

        url = "https://cpaas.messagecentral.com/api/v1/send-sms"
        payload = {
            "flowType": "SMS",
            "type": "OTP",
            "country": COUNTRY_CODE,         # "91"
            "mobileNumber": raw_phone,       # "9052045741" (10 digits only)
            "message": f"Your OTP is {otp}. Do not share it with anyone.",
        }
        headers = {
            "authToken": auth_token,
            "Content-Type": "application/json",
        }

        response = requests.post(url, json=payload, headers=headers, timeout=15)

        if response.status_code == 200:
            logger.info(f"OTP sent successfully to {full_phone}")
            return True, "OTP sent successfully"

        logger.error(f"Message Central send error: {response.status_code} - {response.text}")
        return False, f"Failed to send OTP (HTTP {response.status_code})"

    except Exception as e:
        logger.error(f"send_otp_via_message_central exception: {e}")
        return False, "Failed to send OTP"

def get_or_create_user(phone, ip_address=None):
    """Get existing user or create a new one."""
    conn = get_db()

    user = conn.execute(
        text("SELECT id, phone, name, role, referral_code FROM users WHERE phone = :phone"),
        {"phone": phone}
    ).fetchone()

    if user:
        return dict(user._mapping), "existing"

    referral_code = generate_referral_code()
    result = conn.execute(text("""
        INSERT INTO users (phone, name, role, referral_code, ip_address, created_at)
        VALUES (:phone, '', 'free', :code, :ip, CURRENT_TIMESTAMP)
        RETURNING id
    """), {
        "phone": phone,
        "code": referral_code,
        "ip": ip_address or request.remote_addr,
    })
    user_id = result.fetchone()[0]
    conn.commit()

    return {
        "id": user_id,
        "phone": phone,
        "name": "",
        "role": "free",
        "referral_code": referral_code,
    }, "new"


def generate_jwt_tokens(user_data, remember_me=False):
    """Generate access and refresh tokens."""
    if remember_me:
        access_expires = timedelta(days=30)
        refresh_expires = timedelta(days=365)
    else:
        access_expires = timedelta(minutes=15)
        refresh_expires = timedelta(days=7)

    access_token = create_access_token(
        identity=str(user_data["id"]),
        additional_claims={"role": user_data["role"], "phone": user_data["phone"]},
        expires_delta=access_expires,
    )
    refresh_token = create_refresh_token(
        identity=str(user_data["id"]),
        expires_delta=refresh_expires,
    )

    return access_token, refresh_token, access_expires, refresh_expires


# =================================
# ROUTES
# =================================

@auth_bp.route("/send-otp", methods=["POST"])
def send_otp():
    """Send OTP via Message Central."""
    try:
        data = request.get_json(silent=True) or {}
        raw_phone = data.get("phone", "")

        phone = clean_phone(raw_phone)

        if not validate_phone(phone):
            return jsonify({
                "success": False,
                "message": "Enter a valid 10-digit mobile number starting with 6-9."
            }), 400

        full_phone = COUNTRY_CODE + phone

        # Respect an in-flight cooldown: don't spam Message Central if an
        # OTP was issued in the last 30 seconds for this number.
        existing = otp_storage.get(full_phone)
        if existing and (datetime.now() - existing["created_at"]) < timedelta(seconds=30):
            return jsonify({
                "success": False,
                "message": "Please wait before requesting another OTP."
            }), 429

        otp = generate_otp()
        logger.info(f"Generated OTP for {full_phone}")  # never log the OTP itself in real prod

        otp_storage[full_phone] = _new_otp_record(otp)

        success, message = send_otp_via_message_central(full_phone, phone, otp)

        if success:
            return jsonify({
                "success": True,
                "message": "OTP sent successfully",
                "data": {"phone": phone},
            })

        # Don't leave a dangling OTP record if the SMS never went out
        otp_storage.pop(full_phone, None)
        return jsonify({"success": False, "message": message}), 502

    except Exception:
        logger.exception("send_otp error")
        return jsonify({"success": False, "message": "Something went wrong. Please try again."}), 500


@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    """Verify OTP and login/create user."""
    try:
        data = request.get_json(silent=True) or {}
        raw_phone = data.get("phone", "")
        user_otp = (data.get("otp") or "").strip()
        remember_me = bool(data.get("remember_me", False))

        phone = clean_phone(raw_phone)

        if not validate_phone(phone) or not re.match(r'^\d{6}$', user_otp):
            return jsonify({"success": False, "message": "Invalid phone number or OTP"}), 400

        full_phone = COUNTRY_CODE + phone

        stored = otp_storage.get(full_phone)

        if not stored:
            return jsonify({"success": False, "message": "No OTP found. Please request a new one."}), 401

        if datetime.now() > stored["expires_at"]:
            otp_storage.pop(full_phone, None)
            return jsonify({"success": False, "message": "OTP has expired. Please request a new one."}), 401

        stored["attempts"] += 1
        if stored["attempts"] > MAX_OTP_ATTEMPTS:
            otp_storage.pop(full_phone, None)
            return jsonify({
                "success": False,
                "message": "Too many incorrect attempts. Please request a new OTP."
            }), 429

        if stored["otp"] != user_otp:
            return jsonify({"success": False, "message": "Incorrect OTP. Please try again."}), 401

        # OTP verified - clear it so it can't be reused
        otp_storage.pop(full_phone, None)

        user_data, status = get_or_create_user(phone)
        access_token, refresh_token, access_expires, refresh_expires = generate_jwt_tokens(
            user_data, remember_me
        )

        response = jsonify({
            "success": True,
            "message": "Login successful" if status == "existing" else "Account created successfully",
            "data": {
                "status": status,
                "user": user_data,
            },
        })

        # Cookie-based auth: tokens never touch client-side JS/localStorage.
        set_access_cookies(response, access_token, max_age=int(access_expires.total_seconds()))
        set_refresh_cookies(response, refresh_token, max_age=int(refresh_expires.total_seconds()))

        return response, 200

    except Exception:
        logger.exception("verify_otp error")
        return jsonify({"success": False, "message": "Something went wrong. Please try again."}), 500


@auth_bp.route("/resend-otp", methods=["POST"])
def resend_otp():
    """Resend OTP."""
    try:
        data = request.get_json(silent=True) or {}
        raw_phone = data.get("phone", "")
        phone = clean_phone(raw_phone)

        if not validate_phone(phone):
            return jsonify({"success": False, "message": "Invalid phone number"}), 400

        full_phone = COUNTRY_CODE + phone

        stored = otp_storage.get(full_phone)
        if stored and (datetime.now() - stored["created_at"]) < timedelta(seconds=30):
            return jsonify({
                "success": False,
                "message": "Please wait before requesting another OTP."
            }), 429

        if stored and datetime.now() < stored["expires_at"]:
            otp = stored["otp"]
            stored["attempts"] = 0  # reset attempt counter on resend
        else:
            otp = generate_otp()
            otp_storage[full_phone] = _new_otp_record(otp)

        logger.info(f"Resending OTP for {full_phone}")

        success, message = send_otp_via_message_central(full_phone, otp)

        if success:
            return jsonify({"success": True, "message": "OTP resent successfully"})

        return jsonify({"success": False, "message": message}), 502

    except Exception:
        logger.exception("resend_otp error")
        return jsonify({"success": False, "message": "Something went wrong. Please try again."}), 500

@auth_bp.route("/logout", methods=["POST"])
def logout():
    response = jsonify({"message": "Logged out successfully"})
    unset_jwt_cookies(response)
    return response, 200


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
