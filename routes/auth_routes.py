import os
import random
import re
import string
import logging
from datetime import timedelta

from urllib.parse import quote

from flask import Blueprint, request, jsonify, current_app, render_template, redirect, session, g
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

from database.init_db import get_db_connection
from config.payment_config import BASE_URL
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    set_access_cookies,
    set_refresh_cookies,
    jwt_required,
    get_jwt,
    get_jwt_identity,
    verify_jwt_in_request,
)
from services.jwt_session import (
    ACCESS_TOKEN_TTL,
    DEFAULT_REFRESH_TTL,
    REMEMBER_ME_REFRESH_TTL,
    clear_auth_cookies,
    revoke_tokens_from_request,
)
from services import fraud_policy
from services.sms_service import get_sms_service
from extensions import limiter
from flask_limiter.util import get_remote_address

# Load environment variables
load_dotenv()

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)


def _limit(*args, **kwargs):
    """Apply Flask-Limiter only when the extension has been initialized.

    `limiter` is bound at IMPORT time, and this decorator is evaluated at
    import time too -- so whether the OTP endpoints are rate limited at
    all depends on `extensions.init_extensions()` having run BEFORE this
    module is imported. app.py does that (init_extensions at module
    scope, `from routes import register_routes` after it), but the
    ordering is load-bearing and invisible.

    Any other entrypoint that imports `routes` first -- a test harness, a
    management script, a future WSGI module -- would silently get
    COMPLETELY UNRATE-LIMITED OTP endpoints. Failing loudly in the log is
    the difference between noticing that in the first boot line and
    noticing it in an SMS bill.
    """
    def deco(fn):
        if limiter is None:
            logger.error(
                "RATE LIMIT NOT APPLIED to %s: extensions.init_extensions() "
                "has not run yet at import time. OTP endpoints are UNPROTECTED "
                "in this process.",
                getattr(fn, "__name__", fn),
            )
            return fn
        return limiter.limit(*args, **kwargs)(fn)
    return deco


def _shared_limit(*args, **kwargs):
    """Same load-order guard as _limit, for a limit shared by several routes."""
    def deco(fn):
        if limiter is None:
            logger.error(
                "RATE LIMIT NOT APPLIED to %s: extensions.init_extensions() "
                "has not run yet at import time.",
                getattr(fn, "__name__", fn),
            )
            return fn
        return limiter.shared_limit(*args, **kwargs)(fn)
    return deco


# Max OTP SMS per phone number per hour, shared by send-otp and resend-otp.
# Only requests that actually sent an OTP (HTTP 200) are counted, so a
# resend refused by the 60 s cooldown does not burn the user's quota.
OTP_REQUESTS_PER_NUMBER_PER_HOUR = "5 per hour"


def _otp_sent(response):
    return response.status_code == 200

# =================================
# DATABASE-BASED VERIFICATION STORAGE
# =================================
from database.init_db import engine
from sqlalchemy import text

# How long a sent OTP stays valid (checked by store_verification()/
# get_verification() below, entirely in PostgreSQL -- see NOW() usage
# there). 5 minutes: long enough to survive real-world SMS delivery
# latency plus the time a user needs to read and type the code, while
# staying well within typical OTP-provider (Message Central) validity
# windows, so a code we still consider valid is never one Message
# Central has already independently expired.
VERIFICATION_EXPIRY_SECONDS = 300
# How long a caller must wait before requesting another OTP for the same
# phone number. Deliberately a SEPARATE constant from
# VERIFICATION_EXPIRY_SECONDS: this is a resend throttle, not the OTP's
# validity window -- conflating the two (as a single "does a live row
# exist" check previously did) blocks legitimate resends for the entire
# validity period instead of a short cooldown.
RESEND_COOLDOWN_SECONDS = 60
COUNTRY_CODE = os.getenv("MESSAGE_CENTRAL_COUNTRY", "91")
MAX_OTP_ATTEMPTS = 3
# After MAX_OTP_ATTEMPTS wrong codes the number is locked: verify, send and
# resend all return 429 until this passes. Stored on the otp_verifications
# row itself (expires_at is pushed out to the lock end), so no schema change.
OTP_LOCKOUT_SECONDS = 15 * 60
PENDING_REFERRAL_TTL = timedelta(days=7)
REFERRAL_CODE_INSERT_ATTEMPTS = 8

# admin_settings keys backing the three constants above, so OTP timing can
# be retuned without a deploy. Each falls back to its hardcoded default on
# a missing row, unparseable value, too-low a value, or a lookup failure --
# OTP send/verify must never break because a settings read failed.


def _otp_setting_int(key, default, minimum=1, maximum=None):
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM admin_settings WHERE key = :key"), {"key": key}
            ).fetchone()
        if row is None:
            return default
        value = int(float(row._mapping["value"]))
        if value < minimum:
            return default
        # Security ceilings: an admin setting may tighten these, never loosen them.
        return min(value, maximum) if maximum is not None else value
    except Exception:
        logger.exception("OTP setting lookup failed for key=%s; using default=%s", key, default)
        return default


def _verification_expiry_seconds():
    return _otp_setting_int(
        "otp_verification_expiry_seconds", VERIFICATION_EXPIRY_SECONDS,
        minimum=30, maximum=VERIFICATION_EXPIRY_SECONDS,
    )


def _resend_cooldown_seconds():
    return _otp_setting_int("otp_resend_cooldown_seconds", RESEND_COOLDOWN_SECONDS, minimum=1)


def _max_otp_attempts():
    return _otp_setting_int("otp_max_attempts", MAX_OTP_ATTEMPTS, minimum=1, maximum=MAX_OTP_ATTEMPTS)

# Canonical pre-auth URLs. Single source of truth: every redirect, link
# and fallback in the codebase builds from these, so the flow cannot
# drift apart again the way /register, /public/login and
# /api/auth/public/login did.
SIGNUP_PATH = "/signup"
LOGIN_PATH = "/login"
WELCOME_PATH = "/welcome"
DASHBOARD_PATH = "/dashboard"


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
    """Canonical signup URL that preserves a referral code.

    Now points at SIGNUP_PATH (/signup). /register still exists as a
    permanent redirect to it (routes/public_routes.py), so links and QR
    codes already printed, shared or scanned keep working -- but nothing
    in the codebase generates the old URL any more.
    """
    code = str(ref_code or "").strip()
    if not code:
        return SIGNUP_PATH
    return SIGNUP_PATH + "?ref=" + quote(code, safe="")


def referral_link_for(ref_code):
    """Absolute, shareable referral URL for the Invite Friends share
    link/QR. Reuses the existing BASE_URL config (config/payment_config.py)
    rather than hardcoding a domain -- BASE_URL already defaults to the
    production domain there, while still honoring an explicit dev
    override via the BASE_URL env var, so this never hardcodes localhost
    itself.

    This is the single source of truth for that shareable URL: both
    /api/user/api/invite (the copy-link text, routes/user_routes.py) and
    /qr/<code> (the QR image, app.py's generate_qr()) call this same
    function, so the two can never encode different URLs.

    Points at /install (not /register): a shared referral link/QR is the
    pre-authentication entry point a new user opens first, matching the
    QR/link -> /install -> register/login -> OTP -> dashboard flow.
    register_url_with_ref() above is a separate, unrelated helper used by
    /, /join, and /download-app to land an already-browsing visitor on
    the registration form directly -- intentionally untouched here.
    """
    code = str(ref_code or "").strip()
    if not code:
        return ""
    return BASE_URL.rstrip("/") + "/install?ref=" + quote(code, safe="")


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
    """Session cache for landing pages that have no phone yet. Invalid codes are ignored.

    Marked permanent (uses the existing PERMANENT_SESSION_LIFETIME config in
    app.py, already set to 10 years) so the code survives a real browser/PWA
    close and later reopen — not just the current tab. Without this, Flask's
    default non-permanent session cookie carries no Max-Age and can be
    dropped the moment the browser/app fully closes, e.g. between scanning a
    referral QR and installing the PWA before ever submitting a phone
    number (see resolve_referrer_id_for_signup's session fallback below).
    """
    try:
        referrer = fetch_referrer_by_code(ref_code)
    except Exception:
        logger.exception("cache_landing_referral_code: referrer lookup failed")
        return False
    if not referrer:
        return False
    session.permanent = True
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
    An unknown code (typo, old link) is ignored and logged, never an error:
    signup carries on without an agent and ``g.referral_code_ignored`` lets
    the OTP response tell the user. A code that belongs to this same phone
    is dropped the same way (fraud_policy logs it).
    Returns (ok, error_message); ok is only False if a future rule refuses.
    """
    ref = extract_referral_code(data)
    if not ref:
        pending = get_pending_referral(phone)
        if pending:
            session["ref_code"] = pending.get("ref_code") or session.get("ref_code")
        return True, None

    referrer = fetch_referrer_by_code(ref)
    if not referrer:
        fraud_policy.note_unknown_referral_code(phone, ref)
        g.referral_code_ignored = True
        return True, None

    referrer_phone = clean_phone(referrer.get("phone") or "")
    stored_code = referrer.get("referral_code") or ref
    if fraud_policy.is_self_referral(referrer_phone, phone):
        # Do not attribute self-referral, but never block OTP/login.
        if (session.get("ref_code") or "").strip() in (stored_code, ref, str(ref).strip()):
            session.pop("ref_code", None)
        return True, None

    session["ref_code"] = stored_code
    upsert_pending_referral(phone, stored_code, referrer["id"])
    return True, None


def _with_referral_note(payload):
    """Tell the client when a submitted referral code was ignored (see persist_referral_for_phone)."""
    if getattr(g, "referral_code_ignored", False):
        payload["referral_ignored"] = True
        payload["referral_note"] = "Referral code not recognised; continuing without it."
    return payload


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
            if fraud_policy.is_self_referral(referrer_phone, phone):
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
                SELECT id, phone, name, role, referral_code, referred_by,
                       is_blocked, is_active
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
                if fraud_policy.is_self_referral_id(bound_referrer, user_id):
                    conn.execute(
                        text("UPDATE users SET referred_by = NULL WHERE id = :uid AND referred_by = :uid"),
                        {"uid": user_id},
                    )
                    conn.commit()
                    bound_referrer = None
                fraud_policy.evaluate_signup(
                    conn, user_id, phone, ip_address or request.remote_addr, referrer_id=bound_referrer,
                )
                return {
                    "id": user_id,
                    "phone": phone,
                    "name": "",
                    "role": "free",
                    "referral_code": referral_code,
                    "referred_by": bound_referrer,
                    "is_blocked": False,
                    "is_active": 1,
                }, "new"
            except IntegrityError as exc:
                last_integrity = exc
                try:
                    conn.rollback()
                except Exception:
                    pass
                raced = conn.execute(
                    text("""
                        SELECT id, phone, name, role, referral_code, referred_by,
                               is_blocked, is_active
                        FROM users WHERE phone = :phone
                    """),
                    {"phone": phone},
                ).fetchone()
                if raced:
                    return dict(raced._mapping), "existing"

        logger.exception("User insert failed after referral_code retries: %s", last_integrity)
        raise last_integrity


#: Columns loaded for the auth decision that must never be echoed back
#: to the client. is_blocked/is_active are moderation state -- useful to
#: this module, nobody else's business.
_INTERNAL_USER_FIELDS = ("is_blocked", "is_active")


def public_user(user_data):
    """The user object safe to return in an auth response body."""
    return {
        key: value
        for key, value in (user_data or {}).items()
        if key not in _INTERNAL_USER_FIELDS
    }


def account_is_usable(user_data):
    """False for a banned or deactivated account.

    Mirrors services.jwt_session.lookup_jwt_user's check, which runs on
    every subsequent request. Applying it HERE too is what stops a banned
    user from passing OTP, being handed valid cookies, being redirected to
    /dashboard, and only then being 401'd by the user loader -- which sent
    them round the silent-refresh loop back to login with no explanation
    of why. Fail the login where the user can be told, not four redirects
    later.
    """
    if not user_data:
        return False
    if user_data.get("is_blocked"):
        return False
    if user_data.get("is_active") == 0:
        return False
    return True


def generate_jwt_tokens(user_data, remember_me=False):
    """Generate access and refresh tokens.

    remember_me extends the REFRESH token only. The access token is
    always short-lived, because it is the credential that cannot be
    cheaply withdrawn: revocation goes through services.jwt_blocklist,
    which falls back to per-process memory whenever Redis is unreachable
    (and extensions.py treats Redis as optional). A 30-day access token
    on a lost phone therefore used to survive a logout entirely.

    Nothing is lost by shortening it: app.py's /api/refresh/silent
    transparently mints a new access token from the still-valid refresh
    cookie on any page navigation, so "stay logged in" keeps working
    exactly as before -- it is the refresh token's 30 days that deliver
    that, not the access token's.
    """
    access_expires = ACCESS_TOKEN_TTL
    refresh_expires = REMEMBER_ME_REFRESH_TTL if remember_me else DEFAULT_REFRESH_TTL

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
    """Store verification_id in PostgreSQL.

    expires_at is computed via make_interval(secs => ...) -- a safely
    parameterized PostgreSQL function call, not a hand-built interval
    string -- so VERIFICATION_EXPIRY_SECONDS is the single source of
    truth for OTP validity (no hardcoded '60 seconds'/'300 seconds'
    literal to drift out of sync with it). created_at is refreshed on
    every resend (ON CONFLICT) so it tracks "most recently sent", which
    is what the resend-cooldown check in get_verification() below needs
    -- it is otherwise unused elsewhere in the codebase (confirmed: no
    other query reads otp_verifications.created_at).
    """
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO otp_verifications (phone, verification_id, expires_at)
            VALUES (:phone, :verification_id, NOW() + make_interval(secs => :expiry_seconds))
            ON CONFLICT (phone) DO UPDATE SET
                verification_id = :verification_id,
                -- Message Central's 506 REQUEST_ALREADY_EXISTS hands back the
                -- SAME live code; its wrong-attempt count must survive.
                attempts = CASE
                    WHEN otp_verifications.verification_id = :verification_id
                         AND otp_verifications.expires_at > NOW()
                    THEN otp_verifications.attempts
                    ELSE 0
                END,
                created_at = NOW(),
                expires_at = NOW() + make_interval(secs => :expiry_seconds)
        """), {
            "phone": phone,
            "verification_id": verification_id,
            "expiry_seconds": _verification_expiry_seconds(),
        })
        conn.commit()

def get_verification(phone):
    """Retrieve verification data from PostgreSQL.

    seconds_since_created is computed by PostgreSQL itself
    (EXTRACT(EPOCH FROM (NOW() - created_at))) -- never derived from the
    app server's or a client's clock -- so send_otp()/resend_otp() can
    enforce RESEND_COOLDOWN_SECONDS using the same authoritative NOW()
    already used for the expires_at filter below.
    """
    with engine.connect() as conn:
        # ✅ Let PostgreSQL handle the expiry check
        row = conn.execute(text("""
            SELECT verification_id, attempts, expires_at, created_at,
                   EXTRACT(EPOCH FROM (NOW() - created_at)) AS seconds_since_created
            FROM otp_verifications
            WHERE phone = :phone
              AND expires_at > NOW()
        """), {"phone": phone}).fetchone()
        if row:
            return {
                "verification_id": row._mapping["verification_id"],
                "attempts": row._mapping["attempts"],
                "expires_at": row._mapping["expires_at"],
                "created_at": row._mapping["created_at"],
                "seconds_since_created": float(row._mapping["seconds_since_created"]),
            }
        return None

def reserve_attempt(phone, max_attempts):
    """Atomically claim one verify attempt BEFORE the code is checked.

    A single UPDATE ... WHERE attempts < :max_attempts, so concurrent
    verify requests for one number can never get more than max_attempts
    guesses between them: PostgreSQL row-locks the row and re-checks the
    WHERE clause for each waiting UPDATE. (Reading the count, asking the
    provider, then incrementing let simultaneous requests all pass the
    check.) Every claimed attempt counts, including one whose provider
    call errors or times out.

    The claim that reaches max_attempts also applies the lock (expires_at
    = NOW() + OTP_LOCKOUT_SECONDS). If that last guess is correct, the
    success path deletes the row, so the lock only ever outlives a wrong
    guess.

    Returns {"verification_id", "attempts"} for the claimed attempt, or
    None if the number is locked, the code expired or was used, or there
    is no row.
    """
    with engine.connect() as conn:
        row = conn.execute(text("""
            UPDATE otp_verifications
            SET attempts = attempts + 1,
                expires_at = CASE
                    WHEN attempts + 1 >= :max_attempts
                    THEN NOW() + make_interval(secs => :lock_seconds)
                    ELSE expires_at
                END
            WHERE phone = :phone
              AND expires_at > NOW()
              AND attempts < :max_attempts
            RETURNING verification_id, attempts
        """), {"phone": phone, "max_attempts": max_attempts, "lock_seconds": OTP_LOCKOUT_SECONDS}).fetchone()
        conn.commit()
    if row is None:
        return None
    return {
        "verification_id": row._mapping["verification_id"],
        "attempts": row._mapping["attempts"],
    }


def _locked_response():
    # Only ever called from inside the OTP request handlers.
    fraud_policy.note_otp_lockout(clean_phone((request.get_json(silent=True) or {}).get("phone", "")))
    minutes = max(1, OTP_LOCKOUT_SECONDS // 60)
    return jsonify({
        "success": False,
        "message": f"Too many incorrect attempts. Please try again in {minutes} minutes.",
        "reason": "OTP_LOCKED",
    }), 429


def _is_locked(verification):
    return bool(verification) and verification["attempts"] >= _max_otp_attempts()

def delete_verification(phone):
    """Delete the verification record."""
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM otp_verifications WHERE phone = :phone"), {"phone": phone})
        conn.commit()


# =================================
# ROUTES
# =================================

def _wants_json_tokens():
    """POS/mobile clients opt in via this header to also receive raw JWT
    strings in the JSON body, alongside the normal Set-Cookie tokens used
    by browser sessions. Browser clients never send this header, so their
    response body is byte-for-byte unchanged."""
    return request.headers.get("X-Client-Type", "").strip().lower() == "pos"


@auth_bp.route("/send-otp", methods=["POST"])
@_limit("5 per minute")
@_limit("20 per hour")
@_shared_limit(OTP_REQUESTS_PER_NUMBER_PER_HOUR, scope="otp-request-per-number",
               key_func=otp_phone_key, deduct_when=_otp_sent)
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

        # Resend cooldown -- independent of OTP validity
        # (VERIFICATION_EXPIRY_SECONDS): a caller is only ever blocked here
        # for up to RESEND_COOLDOWN_SECONDS, never for the OTP's full
        # validity window.
        existing = get_verification(full_phone)
        if _is_locked(existing):
            return _locked_response()
        cooldown_seconds = _resend_cooldown_seconds()
        if existing and existing["seconds_since_created"] < cooldown_seconds:
            fraud_policy.note_resend_cooldown(phone, cooldown_seconds)
            return jsonify({
                "success": False,
                "message": f"Please wait {cooldown_seconds} seconds before requesting another OTP."
            }), 429

        # Call the unified SMS service to send OTP
        sms_service = get_sms_service()
        success, response, verification_id = sms_service.send_otp(full_phone)

        if success and verification_id:
            store_verification(full_phone, verification_id)
            logger.info(f"OTP sent successfully to {full_phone} (Verification ID: {verification_id})")
            
            return jsonify(_with_referral_note({
                "success": True,
                "message": "OTP sent successfully",
                "data": {"phone": phone}
            }))

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
@_limit("10 per minute", key_func=otp_phone_key)
@_limit("30 per hour", key_func=otp_phone_key)
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

        max_attempts = _max_otp_attempts()
        if stored["attempts"] >= max_attempts:
            # Locked: the row is kept (expires_at = lock end) so send/resend
            # stay refused too, instead of deleting it and allowing a fresh OTP.
            return _locked_response()

        # Claim the attempt atomically before the provider sees the guess.
        claim = reserve_attempt(full_phone, max_attempts)
        if claim is None:
            # A concurrent request took the last attempt (now locked) or
            # used/expired the code between the read above and this claim.
            if _is_locked(get_verification(full_phone)):
                return _locked_response()
            return jsonify({
                "success": False,
                "message": "This OTP has already been used or expired.",
                "reason": "ALREADY_CONSUMED"
            }), 401

        sms_service = get_sms_service()
        success, response = sms_service.verify_otp(str(claim["verification_id"]), user_otp)

        if not success:
            if claim["attempts"] >= max_attempts:
                return _locked_response()
            return jsonify({"success": False, "message": "Incorrect OTP. Please try again."}), 401

        # The OTP is correct from here on, and Message Central has now
        # marked this verificationId as used on their side -- so it can
        # never be verified a second time even though our own row still
        # exists for a few more lines.
        #
        # Account resolution is therefore wrapped: if it fails, the code
        # really is spent, and the user must be TOLD to request a new one
        # rather than being handed a bare 500 (the old behaviour) or a
        # misleading "Incorrect OTP" on their retry. The row is cleaned
        # up so the next attempt starts from a clean state.
        try:
            referrer_id, ref_error = resolve_referrer_id_for_signup(phone, data)
            if ref_error:
                delete_verification(full_phone)
                return jsonify({"success": False, "message": ref_error}), 400

            # Create or login the user
            user_data, status = get_or_create_user(
                phone,
                ip_address=request.remote_addr,
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                referrer_id=referrer_id,
            )
        except Exception:
            logger.exception("verify_otp: account resolution failed after a valid OTP")
            try:
                delete_verification(full_phone)
            except Exception:
                logger.exception("verify_otp: cleanup of spent verification failed")
            return jsonify({
                "success": False,
                "message": "We could not finish signing you in. Please request a new OTP and try again.",
                "reason": "ACCOUNT_RESOLUTION_FAILED",
            }), 503

        # Refuse a banned/deactivated account BEFORE any cookie is set.
        if not account_is_usable(user_data):
            delete_verification(full_phone)
            logger.warning("Blocked/inactive account attempted login: user_id=%s", user_data.get("id"))
            return jsonify({
                "success": False,
                "message": "This account has been suspended. Please contact support.",
                "reason": "ACCOUNT_SUSPENDED",
            }), 403

        # Account resolved and allowed -- now the code has been spent.
        delete_verification(full_phone)

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

            # Single source of truth for token lifetimes -- the inline
            # copy that used to live here drifted from
            # generate_jwt_tokens() and issued a 30-day ACCESS token on
            # "remember me". See generate_jwt_tokens' docstring.
            access_token, refresh_token, access_expires, refresh_expires = (
                generate_jwt_tokens(user_data, remember_me=remember_me)
            )

            # A brand-new account has no name yet (the INSERT stores '').
            # The client uses this to send first-time users through the
            # /welcome step, which is the ONLY place the name has ever
            # actually been persisted -- the old register form collected
            # it before the OTP and silently discarded it.
            needs_profile = not (user_data.get("name") or "").strip()

            response_data = {
                "status": status,
                "user": public_user(user_data),
                "needs_profile": needs_profile,
                "next": WELCOME_PATH if needs_profile else DASHBOARD_PATH,
                "referral_link": referral_link_for(user_data.get("referral_code")),
            }
            if _wants_json_tokens():
                # POS/mobile only — browser clients rely on the cookies below.
                response_data["access_token"] = access_token
                response_data["refresh_token"] = refresh_token

            response = jsonify({
                "success": True,
                "message": "Login successful" if status == "existing" else "Account created successfully",
                "data": response_data,
            })

            set_access_cookies(response, access_token, max_age=int(access_expires.total_seconds()))
            set_refresh_cookies(response, refresh_token, max_age=int(refresh_expires.total_seconds()))

            return response, 200

    except Exception as e:
        logger.exception("verify_otp error")
        return jsonify({"success": False, "message": "Something went wrong. Please try again."}), 500
        
def _current_request_is_authenticated_user():
    """True only if THIS request already carries a currently-valid JWT
    access-token cookie for any user. Mirrors admin_routes.py's
    _current_request_is_admin: verify_jwt_in_request(optional=True) only
    swallows a genuinely MISSING token (treated as 'not logged in' below).
    An expired or otherwise invalid token is deliberately NOT caught here
    — that exception propagates to the app's existing global JWT error
    handlers (app.py's expired/invalid loaders), the same silent-refresh
    path every other protected page already relies on. No JWT claim is
    inspected or trusted here — only cryptographic/expiry validity of the
    token itself decides the answer, so there is nothing "arbitrary" to
    treat as sufficient.
    """
    return verify_jwt_in_request(optional=True) is not None


@auth_bp.route("/public/login", methods=["GET"])
def public_login_page():
    """Legacy login URL — now a redirect to the canonical /login.

    This used to render the login page itself, which left THREE surfaces
    serving the same OTP form (/register, /public/login and this one),
    two of which had drifted apart on whether an already-authenticated
    visitor gets bounced to the dashboard. The page now lives in exactly
    one place; this endpoint is kept only so old bookmarks, cached
    service-worker entries and any client still holding the old URL keep
    working.

    The ?ref code is carried through so a referral can survive an old
    link. The authenticated-visitor check is not repeated here -- /login
    performs it, and doing it twice just means two places to get it wrong.

    Deliberately a 302, not a 301: this is a PWA with a service worker,
    and a permanently-cached redirect on an auth URL is unrecoverable
    from the server side if the flow ever needs to change again.
    """
    ref = (request.args.get("ref") or "").strip()
    target = LOGIN_PATH + ("?ref=" + quote(ref, safe="") if ref else "")
    return redirect(target)

@auth_bp.route("/resend-otp", methods=["POST"])
@_limit("5 per minute")
@_limit("20 per hour")
@_shared_limit(OTP_REQUESTS_PER_NUMBER_PER_HOUR, scope="otp-request-per-number",
               key_func=otp_phone_key, deduct_when=_otp_sent)
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

        # Same resend cooldown as send_otp() -- see RESEND_COOLDOWN_SECONDS;
        # independent of VERIFICATION_EXPIRY_SECONDS.
        stored = get_verification(full_phone)
        if _is_locked(stored):
            return _locked_response()
        cooldown_seconds = _resend_cooldown_seconds()
        if stored and stored["seconds_since_created"] < cooldown_seconds:
            fraud_policy.note_resend_cooldown(phone, cooldown_seconds)
            return jsonify({
                "success": False,
                "message": f"Please wait {cooldown_seconds} seconds before requesting another OTP."
            }), 429

        # Send a fresh OTP
        sms_service = get_sms_service()
        success, response, verification_id = sms_service.send_otp(full_phone)

        if success and verification_id:
            store_verification(full_phone, verification_id)
            # send_otp() reports success here even when Message Central
            # rejected the resend as REQUEST_ALREADY_EXISTS (responseCode
            # 506) -- the ORIGINAL OTP is still valid, but no new SMS went
            # out. Say so explicitly rather than implying a fresh text was
            # just sent.
            message = "OTP resent successfully"
            if isinstance(response, dict):
                response_code = response.get("responseCode")
                if isinstance(response_code, str):
                    response_code = response_code.strip()
                already_exists = str(response.get("message") or "").strip().upper() == "REQUEST_ALREADY_EXISTS"
                if response_code in (506, "506") and already_exists:
                    message = "An OTP is already on its way — please check your messages"
            return jsonify(_with_referral_note({"success": True, "message": message}))

        return jsonify({"success": False, "message": "Failed to resend OTP"}), 502

    except Exception as e:
        logger.exception("resend_otp error")
        return jsonify({"success": False, "message": "Something went wrong. Please try again."}), 500

@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    revoke_tokens_from_request()
    if request.method == "GET":
        response = redirect("/logout")
        clear_auth_cookies(response)
        return response
    response = jsonify({"success": True, "message": "Logged out successfully"})
    clear_auth_cookies(response)
    return response, 200

@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def get_current_user():
    user_id = get_jwt_identity()
    with get_db_connection() as conn:
        user = conn.execute(
            text("SELECT id, phone, name, role, referral_code FROM users WHERE id = :uid"),
            {"uid": user_id}
        ).fetchone()

    if not user:
        return jsonify({"error": "User not found"}), 404

    data = dict(user._mapping)
    # Surfaces impersonate_user()'s short-lived "impersonated" access-token
    # claim (routes/admin_routes.py) so the client can show an "exit
    # impersonation" banner -- false for every normal session, since the
    # claim is absent unless an admin explicitly impersonated this user.
    data["impersonated"] = bool(get_jwt().get("impersonated", False))
    return jsonify(data), 200
