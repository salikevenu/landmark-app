import os
import random
import re
import string
import logging
from datetime import timedelta

from flask import Blueprint, request, jsonify
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

# Load environment variables
load_dotenv()

print(f"🔍 DEBUG_SMS value in routes/auth.py: {os.getenv('DEBUG_SMS')}")

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

# =================================
# DATABASE-BASED VERIFICATION STORAGE
# =================================
from database.init_db import engine
from sqlalchemy import text

VERIFICATION_EXPIRY_SECONDS = 60
COUNTRY_CODE = os.getenv("MESSAGE_CENTRAL_COUNTRY", "91")
MAX_OTP_ATTEMPTS = 5


# =================================
# HELPER FUNCTIONS
# =================================

def generate_referral_code():
    """Generate a random 8-character alphanumeric referral code."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def validate_phone(phone):
    """Basic Indian mobile number validation (10 digits, starts with 6-9)."""
    return bool(re.match(r'^[6-9]\d{9}$', phone))

def clean_phone(raw_phone):
    """Strip everything except digits, then take the last 10 digits."""
    digits = ''.join(filter(str.isdigit, raw_phone or ''))
    return digits[-10:] if len(digits) >= 10 else digits

def get_or_create_user(phone, ip_address=None):
    """Get existing user or create a new one."""
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
# DATABASE OTP STORAGE FUNCTIONS
# =================================

def store_verification(phone, verification_id):
    """Store verification_id in PostgreSQL."""
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO otp_verifications (phone, verification_id, expires_at)
            VALUES (:phone, :verification_id, NOW() + INTERVAL '60 seconds')
            ON CONFLICT (phone) DO UPDATE SET
                verification_id = :verification_id,
                attempts = 0,
                expires_at = NOW() + INTERVAL '60 seconds'
        """), {
            "phone": phone,
            "verification_id": verification_id
        })
        conn.commit()

def get_verification(phone):
    """Retrieve verification data from PostgreSQL."""
    with engine.connect() as conn:
        # ✅ Let PostgreSQL handle the expiry check
        row = conn.execute(text("""
            SELECT verification_id, attempts, expires_at, created_at
            FROM otp_verifications
            WHERE phone = :phone
              AND expires_at > NOW()
        """), {"phone": phone}).fetchone()
        if row:
            return {
                "verification_id": row._mapping["verification_id"],
                "attempts": row._mapping["attempts"],
                "expires_at": row._mapping["expires_at"],
                "created_at": row._mapping["created_at"]
            }
        return None

def increment_attempts(phone):
    """Increment the attempt counter for a phone number."""
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE otp_verifications
            SET attempts = attempts + 1
            WHERE phone = :phone
        """), {"phone": phone})
        conn.commit()

def delete_verification(phone):
    """Delete the verification record."""
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM otp_verifications WHERE phone = :phone"), {"phone": phone})
        conn.commit()


# =================================
# ROUTES
# =================================

@auth_bp.route("/send-otp", methods=["POST"])
def send_otp():
    """Send OTP via Message Central VerifyNow API."""
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

        # Cooldown check
        existing = get_verification(full_phone)
        if existing:
            return jsonify({
                "success": False,
                "message": "Please wait 30 seconds before requesting another OTP."
            }), 429

        # Call the unified SMS service to send OTP
        sms_service = get_sms_service()
        success, response, verification_id = sms_service.send_otp(full_phone)

        if success and verification_id:
            store_verification(full_phone, verification_id)
            logger.info(f"OTP sent successfully to {full_phone} (Verification ID: {verification_id})")
            
            return jsonify({
                "success": True,
                "message": "OTP sent successfully",
                "data": {"phone": phone}
            })

        # If SMS failed, clean up
        delete_verification(full_phone)
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
    """Verify OTP using Message Central VerifyNow API."""
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

        # Retrieve the stored verification_id from PostgreSQL
        stored = get_verification(full_phone)
        if not stored:
            return jsonify({"success": False, "message": "No OTP found or OTP expired. Please request a new one."}), 401

        if stored["attempts"] >= MAX_OTP_ATTEMPTS:
            delete_verification(full_phone)
            return jsonify({
                "success": False,
                "message": "Too many incorrect attempts. Please request a new OTP."
            }), 429

        # ✅ Verify OTP FIRST, then increment attempts only on failure
        sms_service = get_sms_service()
        success, response = sms_service.verify_otp(str(stored["verification_id"]), user_otp)

        if not success:
            # ✅ Only increment attempts on failure
            increment_attempts(full_phone)
            return jsonify({"success": False, "message": "Incorrect OTP. Please try again."}), 401

        # OTP verified successfully - delete the record
        delete_verification(full_phone)

        # Create or login the user
        with engine.connect() as conn:
            
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

            set_access_cookies(response, access_token, max_age=int(access_expires.total_seconds()))
            set_refresh_cookies(response, refresh_token, max_age=int(refresh_expires.total_seconds()))

            return response, 200

    except Exception as e:
        logger.exception("verify_otp error")
        return jsonify({"success": False, "message": "Something went wrong. Please try again."}), 500
        
@auth_bp.route("/public/login", methods=["GET"])
def public_login_page():
    """Public user login page."""
    return render_template("public/login.html")
    
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

        stored = get_verification(full_phone)
        if stored:
            return jsonify({
                "success": False,
                "message": "Please wait before requesting another OTP."
            }), 429

        # Send a fresh OTP
        sms_service = get_sms_service()
        success, response, verification_id = sms_service.send_otp(full_phone)

        if success and verification_id:
            store_verification(full_phone, verification_id)
            return jsonify({"success": True, "message": "OTP resent successfully"})

        return jsonify({"success": False, "message": "Failed to resend OTP"}), 502

    except Exception as e:
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