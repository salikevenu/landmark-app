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

from database.init_db import get_db_connection
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    set_access_cookies,
    set_refresh_cookies,
    jwt_required,
    get_jwt_identity,
    unset_jwt_cookies,
)
from services.sms_service import get_sms_service
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

# =================================
# HELPER FUNCTIONS
# =================================


def send_otp_via_message_central(full_phone, raw_phone, otp):
    """Send OTP using unified service"""
    try:
        sms_service = get_sms_service()
        success, response, sent_otp = sms_service.send_otp(full_phone, otp)
        return success, "OTP sent successfully" if success else "Failed to send OTP"
    except Exception as e:
        logger.error(f"OTP send error: {e}")
        return False, "Failed to send OTP"
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

def get_or_create_user(phone, ip_address=None):
    """Get existing user or create a new one."""
    
    # Use the database engine directly
    from database.init_db import engine
    from sqlalchemy import text
    
    # Open and close the connection automatically using 'with'
    with engine.connect() as conn:
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
        conn.commit()  # You can commit inside the 'with' block

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

# In routes/auth_routes.py - replace the send_otp_via_message_central function

from services.sms_service import get_sms_service

# Remove the old send_otp_via_message_central function
# Add this new one:

@auth_bp.route("/send-otp", methods=["POST"])
def send_otp():
    """Send OTP via Message Central (Unified Service)"""
    try:
        data = request.get_json(silent=True) or {}
        raw_phone = data.get("phone", "")

        # 1. Clean and validate phone
        phone = clean_phone(raw_phone)
        if not validate_phone(phone):
            return jsonify({
                "success": False,
                "message": "Enter a valid 10-digit mobile number starting with 6-9."
            }), 400

        full_phone = COUNTRY_CODE + phone

        # 2. Cooldown check: prevent spam (30 seconds)
        existing = otp_storage.get(full_phone)
        if existing and (datetime.now() - existing["created_at"]) < timedelta(seconds=30):
            return jsonify({
                "success": False,
                "message": "Please wait 30 seconds before requesting another OTP."
            }), 429

        # 3. Generate OTP and store it
        otp = generate_otp()
        logger.info(f"Generated OTP for {full_phone}")  # Never log the OTP itself!
        
        # Save to in-memory storage (or Redis in production)
        otp_storage[full_phone] = _new_otp_record(otp)

        # 4. Send SMS using the unified service
        from services.sms_service import get_sms_service
        sms_service = get_sms_service()
        
        # The service returns: (success, response_dict, sent_otp)
        success, response, sent_otp = sms_service.send_otp(full_phone, otp)

        if success:
            return jsonify({
                "success": True,
                "message": "OTP sent successfully",
                "data": {"phone": phone}
            })

        # 5. If SMS failed, remove the OTP so user can try again
        otp_storage.pop(full_phone, None)
        logger.error(f"Failed to send OTP to {full_phone}: {response}")
        return jsonify({
            "success": False, 
            "message": "Failed to send OTP. Please try again later."
        }), 502

    except Exception as e:
        logger.exception("send_otp error")
        return jsonify({
            "success": False, 
            "message": "Something went wrong. Please try again."
        }), 500
    
@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    """Verify OTP and login/create user."""
    from database.init_db import engine
    from sqlalchemy import text
    from flask_jwt_extended import create_access_token, create_refresh_token, set_access_cookies, set_refresh_cookies
    
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

        # ✅ OTP verified - clear it so it can't be reused
        otp_storage.pop(full_phone, None)

        # ✅ Use ONE single database connection for the entire request
        with engine.connect() as conn:
            
            # Get or create user using this connection
            user = conn.execute(
                text("SELECT id, phone, name, role, referral_code FROM users WHERE phone = :phone"),
                {"phone": phone}
            ).fetchone()

            if user:
                user_data = dict(user._mapping)
                status = "existing"
            else:
                referral_code = generate_referral_code()
                result = conn.execute(text("""
                    INSERT INTO users (phone, name, role, referral_code, ip_address, created_at)
                    VALUES (:phone, '', 'free', :code, :ip, CURRENT_TIMESTAMP)
                    RETURNING id
                """), {
                    "phone": phone,
                    "code": referral_code,
                    "ip": request.remote_addr,
                })
                user_id = result.fetchone()[0]
                conn.commit()
                
                user_data = {
                    "id": user_id,
                    "phone": phone,
                    "name": "",
                    "role": "free",
                    "referral_code": referral_code,
                }
                status = "new"

            # Generate JWT tokens
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

            response = jsonify({
                "success": True,
                "message": "Login successful" if status == "existing" else "Account created successfully",
                "data": {
                    "status": status,
                    "user": user_data,
                },
            })

            # Cookie-based auth
            set_access_cookies(response, access_token, max_age=int(access_expires.total_seconds()))
            set_refresh_cookies(response, refresh_token, max_age=int(refresh_expires.total_seconds()))

            return response, 200

    except Exception as e:
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
    conn = get_db_connection()
    user = conn.execute(
        text("SELECT id, phone, name, role, referral_code FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()

    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify(dict(user._mapping)), 200
