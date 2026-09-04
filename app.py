import os
import sys

def _boot(msg):
    """Flushing boot tracer — next Render deploy will show the last step that ran."""
    print(f"[BOOT] {msg}", file=sys.stderr, flush=True)
    print(f"[BOOT] {msg}", flush=True)

_boot("STARTING APP INIT")
_boot(f"PORT={os.environ.get('PORT')!r} RENDER={os.environ.get('RENDER')!r}")

_boot("import: stdlib/third-party")
import requests
import secrets
import redis
import random
import traceback

from datetime import timedelta, datetime
from urllib.parse import quote as urlquote
from dotenv import load_dotenv
from flask import Flask, g, request, redirect, render_template, session, jsonify, send_from_directory, send_file, make_response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import text
from functools import lru_cache
from redis_client import get_redis_client
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
    verify_jwt_in_request,
    set_access_cookies,
    set_refresh_cookies,
    unset_jwt_cookies
)
import hmac
from werkzeug.exceptions import HTTPException
from pydantic_settings import BaseSettings
from language.translations import TRANSLATIONS
from extensions import init_extensions
from master_agent import MasterAgent
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
logger = logging.getLogger(__name__)
_boot("imports complete")

# Load environment variables
_boot("load_dotenv()")
load_dotenv()

# === PRODUCTION VALIDATION ===
REQUIRED_ENV_VARS = ["SECRET_KEY", "JWT_SECRET_KEY", "DATABASE_URL"]
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_vars:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing_vars)}")
_boot("required env vars present")
_boot(f"DATABASE_URL set={bool(os.getenv('DATABASE_URL'))} host_hint={str(os.getenv('DATABASE_URL','')).split('@')[-1][:60]!r}")

# Database module import MUST NOT run migrations/connect at import time.
# (A previous trailing block in database/init_db.py called init_db() when RENDER=true
#  and hung the gunicorn worker before it could finish booting.)
_boot("import database.init_db (must be non-blocking)")
from database.init_db import get_db_connection, init_db
_boot("database.init_db imported OK")

# Initialize Flask app
_boot("Flask(__name__)")
app = Flask(__name__)

def _run_init_db_async():
    """Schema init only AFTER the worker has finished importing the app."""
    try:
        _boot("background init_db: starting")
        init_db()
        _boot("background init_db: done")
    except Exception as e:
        _boot(f"background init_db: FAILED (app continues): {e}")

# Defer DB work — never block module import / worker boot
if os.getenv("RENDER") == "true":
    import threading
    threading.Thread(target=_run_init_db_async, daemon=True, name="init_db").start()
    _boot("scheduled background init_db thread")
else:
    _boot("local mode — skipping init_db")

# ==================== MASTER AGENT INITIALIZATION ====================
# Disabled: MasterAgent pulls in SchedulerAgent / APScheduler at init.
master_agent = None
app.master_agent = None
_boot("MasterAgent disabled")

# ==================== APP CONFIGURATION ====================
_boot("app.config / secret_key")
_cookie_secure = os.getenv("RENDER") == "true"
app.config.update(
    SEND_FILE_MAX_AGE_DEFAULT=0,
    TEMPLATES_AUTO_RELOAD=True,
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    UPLOAD_FOLDER="static/uploads",
    JWT_SECRET_KEY=os.getenv("JWT_SECRET_KEY", "your-secure-jwt-secret-key"),
    JWT_ACCESS_TOKEN_EXPIRES=timedelta(hours=2),
    JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=7),
    JWT_TOKEN_LOCATION=["cookies", "headers"],
    JWT_COOKIE_SECURE=_cookie_secure,
    JWT_COOKIE_SAMESITE="Lax",
    JWT_COOKIE_HTTPONLY=True,
    JWT_COOKIE_CSRF_PROTECT=True,
    JWT_COOKIE_PATH="/",
    JWT_ACCESS_COOKIE_PATH="/",
    JWT_ACCESS_COOKIE_NAME="access_token",
    JWT_REFRESH_COOKIE_NAME="refresh_token",
    JWT_REFRESH_COOKIE_PATH="/api/refresh",
    JWT_ACCESS_CSRF_COOKIE_NAME="csrf_access_token",
    JWT_REFRESH_CSRF_COOKIE_NAME="csrf_refresh_token",
    JWT_ACCESS_CSRF_COOKIE_PATH="/",
    JWT_REFRESH_CSRF_COOKIE_PATH="/",
)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365 * 10)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = _cookie_secure
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

app.secret_key = os.getenv("SECRET_KEY", "landmark-super-secret-change-me")

# CORS — explicit origin allowlist (credentialed cookies). Override via ALLOWED_ORIGINS.
_boot("CORS")
_default_cors_origins = (
    "https://landmarkvts.in,"
    "https://www.landmarkvts.in,"
    "http://localhost:8000,"
    "http://localhost:10000,"
    "http://127.0.0.1:8000,"
    "http://127.0.0.1:10000"
)
_allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", _default_cors_origins).split(",")
    if origin.strip()
]
CORS(app, supports_credentials=True, origins=_allowed_origins)

# ==================== EXTENSIONS (ONLY ONCE) ====================
_boot("init_extensions (DummyLimiter + Razorpay client object only)")
limiter, razor_client = init_extensions(app)
_boot("init_extensions done")

# ==================== JWT ====================
_boot("JWTManager")
jwt = JWTManager(app)

def _wants_html_login_redirect():
    accept = request.headers.get("Accept") or ""
    return request.method == "GET" and "text/html" in accept

def _safe_relative_path(path):
    """Reject absolute/protocol-relative targets to avoid open redirects."""
    if not path or not path.startswith("/") or path.startswith("//") or path.startswith("/\\"):
        return "/"
    return path

def _current_path_with_query():
    path = request.path
    qs = request.query_string.decode("utf-8", "ignore")
    return f"{path}?{qs}" if qs else path

def _silent_refresh_redirect():
    """A page navigation hit a missing/expired/invalid access token.

    Cookies (incl. the refresh token) survive browser/app/device restarts,
    so before dropping the user to the login screen, bounce through a
    silent refresh that reuses the still-valid refresh cookie. Only a
    genuinely dead/revoked credential should ever force a fresh login.
    """
    next_url = urlquote(_safe_relative_path(_current_path_with_query()), safe="")
    return redirect(f"/api/refresh/silent?next={next_url}")

@jwt.unauthorized_loader
def _jwt_missing(reason):
    if _wants_html_login_redirect():
        return _silent_refresh_redirect()
    return jsonify({"success": False, "error": "Authentication required"}), 401

@jwt.expired_token_loader
def _jwt_expired(jwt_header, jwt_payload):
    if _wants_html_login_redirect():
        return _silent_refresh_redirect()
    return jsonify({"success": False, "error": "Session expired"}), 401

@jwt.invalid_token_loader
def _jwt_invalid(reason):
    if _wants_html_login_redirect():
        return _silent_refresh_redirect()
    return jsonify({"success": False, "error": "Invalid session"}), 401

from services.jwt_session import register_jwt_security, revoke_tokens_from_request
register_jwt_security(jwt)

# ==================== REGISTER ROUTES ====================
_boot("import routes / register_routes")
from routes import register_routes
register_routes(app)
_boot("register_routes done")

# ==================== DEBUG ROUTES ====================
@app.route('/ping')
def ping():
    return jsonify({
        "status": "ok",
        "service": "LANDMARK",
        "port": os.getenv("PORT"),
        "render": os.getenv("RENDER"),
        "render_service_type": os.getenv("RENDER_SERVICE_TYPE")
    })

# ==================== STATIC FOLDERS ====================
_boot("makedirs static folders")
os.makedirs("static/uploads", exist_ok=True)
os.makedirs("static/uploads/avatars", exist_ok=True)
os.makedirs("static/images/listings", exist_ok=True)
os.makedirs("static/qrcodes", exist_ok=True)

# ==================== TRANSLATIONS ====================
from functools import lru_cache

def __getattr__(name):
    if name == "redis_client":
        return get_redis_client()
    raise AttributeError(f"module {__name__} has no attribute {name}")

@lru_cache(maxsize=10)
def get_translations(lang):
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"])

@app.context_processor
def inject_language():
    lang = request.cookies.get("lang")
    if not lang:
        try:
            verify_jwt_in_request(optional=True)
            user_id = get_jwt_identity()
            if user_id:
                from database.init_db import get_db_connection
                with get_db_connection() as conn:
                    row = conn.execute(
                        text("SELECT language FROM users WHERE id = :uid"),
                        {"uid": user_id}
                    ).fetchone()
                    if row and row._mapping["language"]:
                        lang = row._mapping["language"]
        except Exception:
            pass
    if not lang:
        lang = "en"
    t = get_translations(lang)
    logger.debug(f"Language selected: {lang}")
    return dict(t=t, current_lang=lang, _=lambda key: t.get(key, key))

# ==================== HELPERS ====================
def execute_query(query, params=None, fetchone=False, fetchall=False, commit=False):
    """Disabled: raw SQL helper is unused and unsafe if wired to a request path."""
    logger.error("execute_query is disabled")
    raise RuntimeError("execute_query is disabled")

from services.subscription_access import legacy_add_business_gone

def _execute_payout():
    """Canonical Saturday/admin payout — wallet_balance.balance only."""
    from services.referral_commission import release_locked_referral_payouts
    return release_locked_referral_payouts()

@app.before_request
def before_request_actions():
    logger.info(f"{request.method} {request.path}")
    if 'lang' not in session and request.cookies.get('language'):
        lang_cookie = request.cookies.get('language')
        if lang_cookie in TRANSLATIONS:
            session['lang'] = lang_cookie

# ==================== WEB ROUTES ====================
@app.route("/")
def index():
    ref = (request.args.get("ref") or "").strip()
    if ref:
        from routes.auth_routes import cache_landing_referral_code, register_url_with_ref
        cache_landing_referral_code(ref)
        return redirect(register_url_with_ref(ref))
    lang = session.get("lang", "en")
    t = get_translations(lang)
    return render_template("public/index.html", t=t)

@app.route("/join")
def join():
    """Referral entry point: /join?ref=CODE. Same capture-and-redirect
    pattern as '/' and '/download-app' — canonical signup URL stays
    /register?ref=CODE, this is just a friendlier shareable alias."""
    ref = (request.args.get("ref") or "").strip()
    if ref:
        from routes.auth_routes import cache_landing_referral_code, register_url_with_ref
        cache_landing_referral_code(ref)
        return redirect(register_url_with_ref(ref))
    return redirect("/register")

@app.route("/dashboard")
def redirect_dashboard():
    return redirect("/api/user/dashboard")

@app.route('/download/android')
def download_apk():
    directory = os.path.join(app.root_path, 'static', 'downloads')
    return send_from_directory(directory, 'LANDMARK.apk', as_attachment=True)

@app.route("/map")
@app.route("/browse")
def browse():
    return render_template("map.html")

@app.route("/create-listing")
def redirect_create_listing():
    return redirect("/api/user/create-listing")

@app.route("/my-listings")
def redirect_my_listings():
    return redirect("/api/listing/my-listings")

@app.route("/profile")
def redirect_profile():
    return redirect("/api/user/profile")

@app.route("/invite")
def redirect_invite():
    return redirect("/api/user/invite")

@app.route("/wallet")
def wallet_page():
    return render_template("users/wallet.html")

@app.route("/pricing")
def pricing():
    page_type = (request.args.get("page_type") or "").strip().lower()
    if page_type not in ("service", "business"):
        page_type = ""
    return render_template("users/pricing.html", page_type=page_type)

@app.route("/logout")
def logout_page():
    revoke_tokens_from_request()
    response = make_response(render_template("logout.html"))
    unset_jwt_cookies(response)
    return response

@app.route('/set-language', methods=['POST'])
def set_language():
    raw_data = request.get_data(as_text=True)
    import json
    try:
        data = json.loads(raw_data)
    except:
        return jsonify({'error': 'Invalid JSON'}), 400
    lang = data.get('lang') if data else None
    if not lang or lang not in TRANSLATIONS:
        return jsonify({'error': f'Unsupported language: {lang}'}), 400
    session['lang'] = lang
    resp = jsonify({'status': 'ok'})
    resp.set_cookie('lang', lang, max_age=31536000, httponly=False, samesite='Lax')
    try:
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
        if user_id:
            from database.init_db import get_db_connection
            with get_db_connection() as conn:
                conn.execute(text("UPDATE users SET language = :lang WHERE id = :uid"), {"lang": lang, "uid": user_id})
                conn.commit()
    except Exception:
        pass
    return resp

# ==================== API ROUTES ====================
@app.route("/api/health")
def api_health():
    return {"status": "ok"}

@app.route('/api/readiness')
def readiness():
    try:
        from database.init_db import get_db_connection
        from sqlalchemy import text
        with get_db_connection() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}, 200
    except Exception:
        return {"status": "not ready"}, 503

def _mint_access_token_for_refresh():
    """Assumes a refresh JWT has already been verified on this request.

    Returns (access_token, access_expires) on success.
    Returns (None, None) if the identity no longer maps to an active,
    unblocked user — a genuine auth failure, caller should force login.
    Raises ValueError for a malformed identity (also a hard auth failure).
    Raises any other exception on a transient lookup failure (e.g. DB
    unreachable) — caller should treat that as "service unavailable", never
    as a reason to log the user out.
    """
    uid = int(get_jwt_identity())
    remember_me = bool(get_jwt().get("remember_me"))
    from database.init_db import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute(
            text("SELECT role, phone, is_blocked, is_active FROM users WHERE id = :uid"),
            {"uid": uid},
        ).fetchone()
    if not row:
        return None, None
    mapping = row._mapping
    if mapping.get("is_blocked") or mapping.get("is_active") == 0:
        return None, None
    role = mapping.get("role") or "free"
    phone = mapping.get("phone") or ""
    access_expires = timedelta(days=30) if remember_me else timedelta(hours=2)
    access_token = create_access_token(
        identity=str(uid),
        additional_claims={"role": role, "phone": phone, "remember_me": remember_me},
        expires_delta=access_expires,
    )
    return access_token, access_expires


def _wants_json_tokens():
    """POS/mobile clients opt in via this header to also receive the raw
    access token in the JSON body, alongside the normal Set-Cookie token
    used by browser sessions. Browser clients never send this header, so
    their response body is byte-for-byte unchanged."""
    return request.headers.get("X-Client-Type", "").strip().lower() == "pos"


@app.route("/api/refresh", methods=["POST"])
@limiter.limit("30 per minute")
@jwt_required(refresh=True)
def refresh():
    try:
        access_token, access_expires = _mint_access_token_for_refresh()
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid session"}), 401
    except Exception:
        logger.exception("refresh user lookup failed")
        return jsonify({"success": False, "error": "Service unavailable"}), 503
    if access_token is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401
    payload = {"success": True}
    if _wants_json_tokens():
        # POS/mobile only — browser clients rely on the cookie above.
        payload["access_token"] = access_token
    response = jsonify(payload)
    set_access_cookies(response, access_token, max_age=int(access_expires.total_seconds()))
    return response


@app.route("/api/refresh/silent", methods=["GET"])
@limiter.limit("60 per minute")
def refresh_silent():
    """GET counterpart to /api/refresh, for full-page navigations.

    Protected HTML pages land here (see _silent_refresh_redirect) instead of
    going straight to the login screen when their access-token cookie is
    missing/expired/invalid. As long as the refresh-token cookie (scoped to
    /api/refresh) is still valid, this transparently mints a fresh access
    token and bounces the user back to the page they wanted — keeping them
    signed in across app/browser/device restarts without a fresh OTP.
    Not decorated with @jwt_required: a failed verify here must fall through
    to the login redirect below, not re-trigger the loaders that sent us
    here (which would recurse back to this same route).
    """
    next_url = _safe_relative_path(request.args.get("next"))
    login_url = "/api/auth/public/login"

    try:
        verify_jwt_in_request(refresh=True)
    except Exception:
        resp = redirect(login_url)
        unset_jwt_cookies(resp)
        return resp

    try:
        access_token, access_expires = _mint_access_token_for_refresh()
    except (TypeError, ValueError):
        resp = redirect(login_url)
        unset_jwt_cookies(resp)
        return resp
    except Exception:
        logger.exception("silent refresh user lookup failed")
        return "Service temporarily unavailable. Please try again.", 503

    if access_token is None:
        resp = redirect(login_url)
        unset_jwt_cookies(resp)
        return resp

    resp = redirect(next_url)
    set_access_cookies(resp, access_token, max_age=int(access_expires.total_seconds()))
    return resp

@app.route('/download-app')
def download_app():
    ref = (request.args.get("ref") or "").strip()
    if ref:
        from routes.auth_routes import cache_landing_referral_code, register_url_with_ref
        cache_landing_referral_code(ref)
        return redirect(register_url_with_ref(ref))
    apk_path = os.path.join(app.root_path, 'static', 'app')
    return send_from_directory(apk_path, 'landmark.apk', as_attachment=True)

import qrcode
from qrcode.constants import ERROR_CORRECT_H
from io import BytesIO
from PIL import Image

LANDMARK_LOGO_PATH = os.path.join(app.root_path, 'static', 'images', 'landmark-logo.png')


def _overlay_logo_center(qr_img, logo_path):
    """Paste the real LANDMARK logo in the center of a QR image, on a small
    white backing square for contrast. High error correction (H, ~30%
    recoverable) plus a logo capped at ~20% of the QR width keeps the code
    reliably scannable. Any failure here must never break /qr/<code>."""
    try:
        with Image.open(logo_path) as logo_src:
            logo = logo_src.copy()
        target = max(int(qr_img.size[0] * 0.20), 1)
        logo.thumbnail((target, target), Image.LANCZOS)

        pad = max(int(target * 0.14), 6)
        backing_size = (logo.size[0] + pad * 2, logo.size[1] + pad * 2)
        backing = Image.new("RGB", backing_size, "white")
        backing_pos = (
            (qr_img.size[0] - backing_size[0]) // 2,
            (qr_img.size[1] - backing_size[1]) // 2,
        )
        qr_img.paste(backing, backing_pos)

        logo_pos = (
            (qr_img.size[0] - logo.size[0]) // 2,
            (qr_img.size[1] - logo.size[1]) // 2,
        )
        mask = logo.split()[3] if logo.mode == "RGBA" else None
        qr_img.paste(logo, logo_pos, mask)
    except Exception:
        logger.exception("QR logo overlay failed; serving plain QR")
    return qr_img


@app.route('/qr/<referral_code>')
def generate_qr(referral_code):
    from routes.auth_routes import register_url_with_ref
    signup_url = request.host_url.rstrip('/') + register_url_with_ref(referral_code)

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(signup_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    img = _overlay_logo_center(img, LANDMARK_LOGO_PATH)

    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    return send_file(img_io, mimetype='image/png')

@app.route("/api/add-business", methods=["POST"])
@jwt_required()
def api_add_business():
    # Legacy: wrote to `businesses`, inverted free-plan check, unused by current listing UI.
    # Disabled (410) so unpaid users cannot bypass canonical listing authorization.
    # Canonical path: POST /api/listing/create-listing
    body, code = legacy_add_business_gone()
    return jsonify(body), code

def _secrets_match(left, right):
    if not left or not right or len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _internal_job_authorized():
    """Shared bearer check for Saturday payout and commission retry crons."""
    expected = (os.getenv("SATURDAY_PAYOUT_SECRET") or "").strip()
    if not expected:
        return False
    jwt_secret = (os.getenv("JWT_SECRET_KEY") or "").strip()
    app_secret = (os.getenv("SECRET_KEY") or "").strip()
    if _secrets_match(expected, jwt_secret) or _secrets_match(expected, app_secret):
        logger.error("SATURDAY_PAYOUT_SECRET must be distinct from app/JWT secrets")
        return False
    auth = (request.headers.get("Authorization") or "").strip()
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False
    provided = auth[len(prefix):].strip()
    if not _secrets_match(provided, expected):
        return False
    return True


@app.route("/api/wallet/overview")
@jwt_required()
def wallet_overview():
    from database.init_db import get_db_connection
    from services.wallet_service import get_wallet_balance
    from services.referral_commission import next_saturday_6pm_ist
    user_id = get_jwt_identity()
    with get_db_connection() as conn:
        wallet = conn.execute(text("SELECT balance FROM wallet_balance WHERE user_id = :uid"), {"uid": user_id}).fetchone()
        available = wallet._mapping["balance"] if wallet else 0.0
        pending = conn.execute(text("SELECT COALESCE(SUM(amount),0) FROM wallet_transactions WHERE user_id = :uid AND status = 'locked' AND source IN ('activation_bonus','base_referral','referral_first_bonus','referral_recurring')"), {"uid": user_id}).scalar()
    next_payout = next_saturday_6pm_ist().strftime("%Y-%m-%d %H:%M IST") if next_saturday_6pm_ist else ""
    return jsonify({"available_balance": available, "pending_unlock": round(pending,2), "next_payout_ist": next_payout})

@app.route('/internal/saturday-payout', methods=['POST'])
def saturday_payout():
    if not _internal_job_authorized():
        return jsonify({"error": "Unauthorized"}), 403
    released = _execute_payout()
    sponsorship = {"cleared": 0}
    try:
        from services.sponsorship import cleanup_expired_sponsorships
        sponsorship = cleanup_expired_sponsorships()
    except Exception:
        logger.exception("sponsorship expiry cleanup failed; payout already completed")
    return jsonify({"released": released, "sponsorship_cleared": sponsorship.get("cleared", 0)}), 200


@app.route('/internal/referral-commission-retry', methods=['POST'])
def referral_commission_retry():
    """Drain pending referral_commission_jobs. Idempotent; SKIP LOCKED."""
    if not _internal_job_authorized():
        return jsonify({"error": "Unauthorized"}), 403
    from services.referral_commission import process_pending_referral_commission_jobs
    result = process_pending_referral_commission_jobs(razorpay_payment_id=None, limit=100)
    return jsonify(result or {"processed": [], "failed": []}), 200

@app.route('/internal/recompute-ranks', methods=['POST'])
def recompute_ranks_internal():
    """Nightly Rank system batch recompute + this period's Leader/Ranger
    monthly growth reward evaluation. Both idempotent; same functions the
    admin 'recompute now' button calls (services.rank_service)."""
    if not _internal_job_authorized():
        return jsonify({"error": "Unauthorized"}), 403
    from services.rank_service import recompute_all_ranks, evaluate_monthly_rewards
    rank_result = recompute_all_ranks()
    reward_result = evaluate_monthly_rewards()
    return jsonify({"rank": rank_result, "monthly_rewards": reward_result}), 200

@app.route('/api/payment/webhook', methods=['POST'])
def razorpay_webhook_dummy():
    """Unsigned path. Must not activate subscriptions or record payments."""
    return jsonify({
        "success": False,
        "error": "Use the signed webhook at /api/payment/razorpay/webhook",
    }), 403

@app.route('/favicon.ico')
def favicon():
    if os.path.exists("static/favicon.ico"):
        return send_from_directory('static', 'favicon.ico', mimetype='image/vnd.microsoft.icon')
    return '', 204

@app.route('/.well-known/appspecific/com.chrome.devtools.json')
def chrome_devtools():
    return '', 204

@app.route('/.well-known/<path:filename>')
def well_known_ignore(filename):
    return '', 204

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e.get_response()
    logger.error(traceback.format_exc())
    return jsonify({"error": "Something went wrong. Please try again."}), 500

from middleware.security_headers import add_security_headers
add_security_headers(app)

@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html")

@app.route("/terms")
def terms_of_service():
    return render_template("terms.html")

_boot("APP INIT COMPLETE — module finished; gunicorn worker can accept requests")

# ------------------------------
# Run the app (local / non-gunicorn only)
# ------------------------------
if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    port = int(os.getenv("PORT", 10000))
    _boot(f"__main__ app.run host=0.0.0.0 port={port}")
    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug_mode
    )