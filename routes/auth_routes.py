import os
import random
import re
import string
import logging
from datetime import timedelta

from urllib.parse import quote

from flask import Blueprint, request, jsonify, current_app, render_template, redirect, session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
from services.jwt_session import revoke_tokens_from_request
from services.sms_service import get_sms_service
from extensions import limiter
from flask_limiter.util import get_remote_address

# Load environment variables
load_dotenv()

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)


def _limit(*args, **kwargs):
    """Apply Flask-Limiter only when the extension has been initialized."""
    def deco(fn):
        if limiter is None:
            return fn
        return limiter.limit(*args, **kwargs)(fn)
    return deco

# =================================
# DATABASE-BASED VERIFICATION STORAGE
# =================================
from database.init_db import engine
from sqlalchemy import text

VERIFICATION_EXPIRY_SECONDS = 60
COUNTRY_CODE = os.getenv("MESSAGE_CENTRAL_COUNTRY", "91")
MAX_OTP_ATTEMPTS = 5
PENDING_REFERRAL_TTL = timedelta(days=7)
REFERRAL_CODE_INSERT_ATTEMPTS = 8


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


def extract_referral_code(data=None):
    """Resolve ref from query, JSON body, then Flask session (cache)."""
    data = data or {}
    for candidate in (
        request.args.get("ref") if request else None,
        data.get("ref"),
        data.get("referral_code"),
        session.get("ref_code") if session else None,
    ):
        if candidate is None:
            continue
        value = str(candidate).strip()
        if value:
            return value
    return ""


def register_url_with_ref(ref_code):
    """Canonical signup URL that preserves a referral code."""
    code = str(ref_code or "").strip()
    if not code:
        return "/register"
    return "/register?ref=" + quote(code, safe="")


def fetch_referrer_by_code(ref_code):
    """Return referrer user dict or None. referral_code match is exact."""
    code = (ref_code or "").strip()
    if not code:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, phone, referral_code FROM users WHERE referral_code = :code"),
            {"code": code},
        ).fetchone()
    if not row:
        return None
    return dict(row._mapping)


def cache_landing_referral_code(ref_code):
    """Session cache for landing pages that have no phone yet. Invalid codes are ignored."""
    referrer = fetch_referrer_by_code(ref_code)
    if not referrer:
        return False
    session["ref_code"] = referrer.get("referral_code") or str(ref_code).strip()
    return True


def get_pending_referral(phone):
    """Load a non-expired pending referral for a normalized 10-digit phone."""
    if not phone:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT ref_code, referrer_id, expires_at
                FROM pending_referrals
                WHERE phone = :phone
                  AND expires_at > NOW()
            """),
            {"phone": phone},
        ).fetchone()
    if not row:
        return None
    return dict(row._mapping)


def upsert_pending_referral(phone, ref_code, referrer_id):
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO pending_referrals (phone, ref_code, referrer_id, created_at, expires_at)
                VALUES (
                    :phone, :ref_code, :referrer_id,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP + INTERVAL '7 days'
                )
                ON CONFLICT (phone) DO UPDATE SET
                    ref_code = EXCLUDED.ref_code,
                    referrer_id = EXCLUDED.referrer_id,
                    created_at = CURRENT_TIMESTAMP,
                    expires_at = CURRENT_TIMESTAMP + INTERVAL '7 days'
            """),
            {
                "phone": phone,
                "ref_code": ref_code,
                "referrer_id": int(referrer_id),
            },
        )
        conn.commit()


def clear_pending_referral(phone):
    if not phone:
        return
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM pending_referrals WHERE phone = :phone"),
            {"phone": phone},
        )
        conn.commit()
    session.pop("ref_code", None)


def persist_referral_for_phone(phone, data=None):
    """
    Validate and persist a referral for this phone.
    Empty ref is allowed (no attribution).
    Invalid or self-referral codes are rejected.
    Returns (ok, error_message).
    """
    ref = extract_referral_code(data)
    if not ref:
        pending = get_pending_referral(phone)
        if pending:
            session["ref_code"] = pending.get("ref_code") or session.get("ref_code")
        return True, None

    referrer = fetch_referrer_by_code(ref)
    if not referrer:
        return False, "Invalid referral code."

    referrer_phone = clean_phone(referrer.get("phone") or "")
    stored_code = referrer.get("referral_code") or ref
    if referrer_phone and referrer_phone == phone:
        # Do not attribute self-referral, but never block OTP/login.
        if (session.get("ref_code") or "").strip() in (stored_code, ref, str(ref).strip()):
            session.pop("ref_code", None)
        return True, None

    session["ref_code"] = stored_code
    upsert_pending_referral(phone, stored_code, referrer["id"])
    return True, None


def resolve_referrer_id_for_signup(phone, data=None):
    """Referrer user id for a new account, or None. Does not reassign existing users."""
    ok, err = persist_referral_for_phone(phone, data)
    if not ok:
        return None, err

    pending = get_pending_referral(phone)
    if pending and pending.get("referrer_id"):
        return int(pending["referrer_id"]), None

    session_code = (session.get("ref_code") or "").strip()
    if session_code:
        referrer = fetch_referrer_by_code(session_code)
        if referrer:
            referrer_phone = clean_phone(referrer.get("phone") or "")
            if referrer_phone and referrer_phone == phone:
                session.pop("ref_code", None)
                return None, None
            return int(referrer["id"]), None
    return None, None


def otp_phone_key():
    """Rate-limit key by submitted phone (falls back to IP if missing)."""
    data = request.get_json(silent=True) or {}
    phone = clean_phone(data.get("phone", ""))
    if phone:
        return f"otp-phone:{phone}"
    return get_remote_address()

def _parse_coord(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_or_create_user(phone, ip_address=None, latitude=None, longitude=None, referrer_id=None):
    """Get existing user or create a new one. Sets referred_by only on INSERT."""
    with engine.connect() as conn:
        user = conn.execute(
            text("""
                SELECT id, phone, name, role, referral_code, referred_by
                FROM users WHERE phone = :phone
            """),
            {"phone": phone}
        ).fetchone()

        if user:
            return dict(user._mapping), "existing"

        bound_referrer = None
        if referrer_id is not None:
            try:
                bound_referrer = int(referrer_id)
            except (TypeError, ValueError):
                bound_referrer = None

        last_integrity = None
        for _ in range(REFERRAL_CODE_INSERT_ATTEMPTS):
            referral_code = generate_referral_code()
            try:
                result = conn.execute(text("""
                    INSERT INTO users (
                        phone, name, role, referral_code, referred_by,
                        ip_address, latitude, longitude, created_at
                    )
                    VALUES (
                        :phone, '', 'free', :code, :referred_by,
                        :ip, :lat, :lng, CURRENT_TIMESTAMP
                    )
                    RETURNING id
                """), {
                    "phone": phone,
                    "code": referral_code,
                    "referred_by": bound_referrer,
                    "ip": ip_address or request.remote_addr,
                    "lat": _parse_coord(latitude),
                    "lng": _parse_coord(longitude),
                })
                user_id = result.fetchone()[0]
                conn.commit()
                if bound_referrer is not None and bound_referrer == user_id:
                    conn.execute(
                        text("UPDATE users SET referred_by = NULL WHERE id = :uid AND referred_by = :uid"),
                        {"uid": user_id},
                    )
                    conn.commit()
                    bound_referrer = None
                return {
                    "id": user_id,
                    "phone": phone,
                    "name": "",
                    "role": "free",
                    "referral_code": referral_code,
                    "referred_by": bound_referrer,
                }, "new"
            except IntegrityError as exc:
                last_integrity = exc
                try:
                    conn.rollback()
                except Exception:
                    pass
                raced = conn.execute(
                    text("""
                        SELECT id, phone, name, role, referral_code, referred_by
                        FROM users WHERE phone = :phone
                    """),
                    {"phone": phone},
                ).fetchone()
                if raced:
                    return dict(raced._mapping), "existing"

        logger.exception("User insert failed after referral_code retries: %s", last_integrity)
        raise last_integrity


def generate_jwt_tokens(user_data, remember_me=False):
    """Generate access and refresh tokens."""
    if remember_me:
        access_expires = timedelta(days=30)
        refresh_expires = timedelta(days=365)
    else:
        access_expires = timedelta(hours=2)
        refresh_expires = timedelta(days=7)

    access_token = create_access_token(
        identity=str(user_data["id"]),
        additional_claims={
            "role": user_data["role"],
            "phone": user_data["phone"],
            "remember_me": remember_me,
        },
        expires_delta=access_expires,
    )
    refresh_token = create_refresh_token(
        identity=str(user_data["id"]),
        additional_claims={"remember_me": remember_me},
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
@_limit("5 per minute")
@_limit("20 per hour")
@_limit("3 per hour", key_func=otp_phone_key)
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

        ok, ref_error = persist_referral_for_phone(phone, data)
        if not ok:
            return jsonify({
                "success": False,
                "message": ref_error or "Invalid referral code."
            }), 400

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
@_limit("10 per minute")
@_limit("30 per hour")
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

        explicit_ref = (
            (request.args.get("ref") or "").strip()
            or str(data.get("ref") or "").strip()
            or str(data.get("referral_code") or "").strip()
        )
        if explicit_ref:
            ok, ref_error = persist_referral_for_phone(phone, {"ref": explicit_ref})
            if not ok:
                return jsonify({"success": False, "message": ref_error or "Invalid referral code."}), 400

        full_phone = COUNTRY_CODE + phone

        # Retrieve the stored verification_id from PostgreSQL
        stored = get_verification(full_phone)
        if not stored:
            return jsonify({
                "success": False,
                "message": "This OTP has already been used or expired.",
                "reason": "ALREADY_CONSUMED"
            }), 401

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

        referrer_id, ref_error = resolve_referrer_id_for_signup(phone, data)
        if ref_error:
            return jsonify({"success": False, "message": ref_error}), 400

        # Create or login the user
        user_data, status = get_or_create_user(
            phone,
            ip_address=request.remote_addr,
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            referrer_id=referrer_id,
        )

        with engine.connect() as conn:
            try:
                # Fallback only for brand-new users whose INSERT did not bind referred_by.
                if status == "new" and not user_data.get("referred_by") and referrer_id:
                    if int(referrer_id) != int(user_data["id"]):
                        conn.execute(
                            text("""
                                UPDATE users
                                SET referred_by = :rid
                                WHERE id = :uid AND referred_by IS NULL
                            """),
                            {"rid": int(referrer_id), "uid": user_data["id"]},
                        )
                        conn.commit()
                        user_data["referred_by"] = int(referrer_id)
            finally:
                clear_pending_referral(phone)

            # Generate JWT tokens
            if remember_me:
                access_expires = timedelta(days=30)
                refresh_expires = timedelta(days=365)
            else:
                access_expires = timedelta(hours=2)
                refresh_expires = timedelta(days=7)

            access_token = create_access_token(
                identity=str(user_data["id"]),
                additional_claims={
                    "role": user_data["role"],
                    "phone": user_data["phone"],
                    "remember_me": remember_me,
                },
                expires_delta=access_expires,
            )
            refresh_token = create_refresh_token(
                identity=str(user_data["id"]),
                additional_claims={"remember_me": remember_me},
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
@_limit("5 per minute")
@_limit("20 per hour")
@_limit("3 per hour", key_func=otp_phone_key)
def resend_otp():
    """Resend OTP."""
    try:
        data = request.get_json(silent=True) or {}
        raw_phone = data.get("phone", "")
        phone = clean_phone(raw_phone)

        if not validate_phone(phone):
            return jsonify({"success": False, "message": "Invalid phone number"}), 400

        full_phone = COUNTRY_CODE + phone

        ok, ref_error = persist_referral_for_phone(phone, data)
        if not ok:
            return jsonify({
                "success": False,
                "message": ref_error or "Invalid referral code."
            }), 400

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

@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    revoke_tokens_from_request()
    if request.method == "GET":
        response = redirect("/logout")
        unset_jwt_cookies(response)
        return response
    response = jsonify({"success": True, "message": "Logged out successfully"})
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