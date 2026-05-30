# app.py
import os, requests, secrets, redis
import traceback
from datetime import timedelta, datetime
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
    get_jwt_identity, 
    jwt_required, 
    verify_jwt_in_request,
    set_access_cookies,
    set_refresh_cookies,
    unset_jwt_cookies
)
from werkzeug.exceptions import HTTPException

from language.translations import TRANSLATIONS

import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# === PRODUCTION VALIDATION ===
import os

REQUIRED_ENV_VARS = ["SECRET_KEY", "JWT_SECRET_KEY", "DATABASE_URL"]
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_vars:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Redis is strongly recommended for production
if os.getenv("FLASK_ENV") == "production" and not os.getenv("REDIS_URL"):
    raise RuntimeError("REDIS_URL is required in production")

# Database connection (PostgreSQL via SQLAlchemy)
from database.init_db import get_db

# Blueprint registration
from routes import register_routes


# Initialize Flask app
app = Flask(__name__)

CORS(app, supports_credentials=True)
app.secret_key = os.getenv("SECRET_KEY", "landmark-super-secret-change-me")

# Rate Limiting – use the same Redis URL (but lazy load may not be available yet)
# For rate limiting, we need a URL at startup; provide a fallback for CI.
redis_url_for_limiter = os.getenv("REDIS_URL")
if not redis_url_for_limiter and os.getenv("CI") == "true":
    redis_url_for_limiter = "redis://localhost:6379/0"

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=redis_url_for_limiter,
    default_limits=["200 per day", "50 per hour"]
)

# ------------------------------
# Configuration (JWT & uploads)
# ------------------------------
app.config.update(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    UPLOAD_FOLDER="static/uploads",
    JWT_SECRET_KEY=os.getenv("JWT_SECRET_KEY", "your-secure-jwt-secret-key"),
    JWT_ACCESS_TOKEN_EXPIRES=timedelta(minutes=15),
    JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=7),
    JWT_TOKEN_LOCATION=["cookies", "headers"],
    JWT_COOKIE_SECURE=True,
    JWT_COOKIE_CSRF_PROTECT=True,
    JWT_ACCESS_COOKIE_PATH="/",
    JWT_ACCESS_COOKIE_NAME="access_token",
    JWT_REFRESH_COOKIE_NAME="refresh_token",
    JWT_REFRESH_COOKIE_PATH="/token/refresh",
)

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365 * 10)  # 10 years
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = True    # Ensure HTTPS in production
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Initialize JWT manager
jwt = JWTManager(app)

# ------------------------------
# Ensure required folders exist
# ------------------------------
os.makedirs("static/uploads", exist_ok=True)
os.makedirs("static/images/listings", exist_ok=True)
os.makedirs("static/qrcodes", exist_ok=True)

# app.py – after app initialization, before routes

from functools import lru_cache

from routes import register_routes

# Optional: provide a module-level proxy that throws when accessed
def __getattr__(name):
    if name == "redis_client":
        return get_redis_client()
    raise AttributeError(f"module {__name__} has no attribute {name}")
@lru_cache(maxsize=10)   # cache translations for each language
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
                conn = get_db()
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
    
    # Use the FULL translations (imported from language.translations)
    t = get_translations(lang)   # note the 's' – cached version using full TRANSLATIONS
    logger.debug(f"Language selected: {lang}")
    return dict(
        t=t,
        current_lang=lang,
        _=lambda key: t.get(key, key)
    )

# ------------------------------
# Database helper (wrapper using text())
# ------------------------------
def execute_query(query, params=None, fetchone=False, fetchall=False, commit=False):
    """Execute a SQLAlchemy text query and return results if requested."""
    conn = get_db()
    result = conn.execute(text(query), params or {})
    if commit:
        conn.commit()
    if fetchone:
        return result.fetchone()
    elif fetchall:
        return result.fetchall()
    return None

# ------------------------------
# Helper: subscription check
# ------------------------------
def is_subscription_active(user_dict):
    plan = user_dict.get("plan", "free")
    if plan == "free":
        return True   # free plan is always active
    expiry_str = user_dict.get("subscription_expiry")
    if not expiry_str:
        return False
    try:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d")
        return expiry_date >= datetime.utcnow()
    except:
        return False

def _execute_payout():
    conn = get_db()
    # Use NOW() in the query – no parameter needed
    locked = conn.execute(text("""
        SELECT id, user_id, amount
        FROM wallet_transactions
        WHERE type = 'credit'
          AND source IN ('referral_first_bonus', 'referral_recurring')
          AND status = 'locked'
          AND unlock_at <= NOW()
    """)).fetchall()

    released_count = 0
    for row in locked:
        uid = row._mapping["user_id"]
        amt = row._mapping["amount"]
        tid = row._mapping["id"]

        conn.execute(text("""
            INSERT INTO wallet_balance (user_id, balance, updated_at)
            VALUES (:uid, :amt, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET balance = wallet_balance.balance + :amt2,
                updated_at = NOW()
        """), {"uid": uid, "amt": amt, "amt2": amt})

        conn.execute(text("UPDATE users SET wallet_balance = wallet_balance + :amt WHERE id = :uid"),
                     {"amt": amt, "uid": uid})

        conn.execute(text("UPDATE wallet_transactions SET status = 'released' WHERE id = :tid"),
                     {"tid": tid})

        released_count += 1

    conn.commit()
    return released_count

@app.before_request
def before_request_actions():
    # 1. Logging (your existing code)
    logger.info(f"{request.method} {request.path}")
    
    # 2. Load language from cookie into session (if not already set)
    if 'lang' not in session and request.cookies.get('language'):
        lang_cookie = request.cookies.get('language')
        if lang_cookie in TRANSLATIONS:   # use your TRANSLATIONS dict keys
            session['lang'] = lang_cookie

# ------------------------------
# Web Routes (public)
# ------------------------------
@app.route("/")
def index():
    lang = session.get("lang", "en")
    t = get_translations(lang)
    return render_template("public/index.html", t=t)

@app.route("/dashboard")
def redirect_dashboard():
    return redirect("/api/user/dashboard")

from flask import send_from_directory

@app.route('/download/android')
def download_apk():
    directory = os.path.join(app.root_path, 'static', 'downloads')
    return send_from_directory(directory, 'LANDMARK.apk', as_attachment=True)

@app.route("/browse")
def browse():
    return redirect("/api/user/browse")

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
    return render_template("users/pricing.html")

@app.route("/logout")
def logout_page():
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
    
    # ✅ Update session (so dropdown selected works)
    session['lang'] = lang
    
    # Set cookie for persistence across browser sessions
    resp = jsonify({'status': 'ok'})
    resp.set_cookie('lang', lang, max_age=31536000, httponly=False, samesite='Lax')
    
    # Update DB if user logged in
    try:
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
        if user_id:
            conn = get_db()
            conn.execute(
                text("UPDATE users SET language = :lang WHERE id = :uid"),
                {"lang": lang, "uid": user_id}
            )
            conn.commit()
    except Exception:
        pass
    
    return resp

# ------------------------------
# API Routes (JWT‑protected)
# ------------------------------
@app.route("/api/health")
def api_health():
    return {"status": "ok"}

@app.route("/api/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh():
    current_user_id = get_jwt_identity()
    new_access_token = create_access_token(identity=current_user_id)
    return jsonify(access_token=new_access_token)

from flask import send_from_directory

@app.route('/download-app')
def download_app():
    ref = request.args.get('ref')
    if ref:
        # Optional: log download to a new table 'referral_downloads' for analytics
        # We'll skip logging for now, but you can add later.
        pass
    apk_path = os.path.join(app.root_path, 'static', 'app')
    return send_from_directory(apk_path, 'landmark.apk', as_attachment=True)

import qrcode
from io import BytesIO

@app.route('/qr/<referral_code>')
def generate_qr(referral_code):
    download_url = request.host_url.rstrip('/') + f'/download-app?ref={referral_code}'
    qr = qrcode.make(download_url)
    img_io = BytesIO()
    qr.save(img_io, 'PNG')
    img_io.seek(0)
    return send_file(img_io, mimetype='image/png')

@app.route("/api/add-business", methods=["POST"])
@jwt_required()
def api_add_business():
    user_id = get_jwt_identity()
    user = execute_query(
        "SELECT * FROM users WHERE id = :uid",
        {"uid": user_id},
        fetchone=True
    )
    if not user:
        return jsonify({"error": "User not found"}), 404

    user_dict = dict(user._mapping)

    if user_dict["plan"] == "free":
        return jsonify({"error": "This feature requires a paid plan. Please upgrade."}), 403

    if not is_subscription_active(user_dict):
        return jsonify({"error": "Your plan has expired. Please upgrade."}), 403

    if user_dict["role"] != "business_owner":
        return jsonify({"error": "Only business users can add businesses."}), 403

    count_result = execute_query(
        "SELECT COUNT(*) as cnt FROM businesses WHERE user_id = :uid",
        {"uid": user_id},
        fetchone=True
    )
    count = count_result._mapping["cnt"]

    if count >= user_dict.get("business_limit", 0):
        return jsonify({"error": "Business limit reached. Upgrade your plan."}), 403

    name = request.json.get("name") if request.is_json else request.form.get("name")
    if not name:
        return jsonify({"error": "Business name required"}), 400

    execute_query(
        "INSERT INTO businesses (user_id, name) VALUES (:uid, :name)",
        {"uid": user_id, "name": name},
        commit=True
    )
    return jsonify({"message": "Business added successfully"}), 201

@app.route("/api/wallet/overview")
@jwt_required()
def wallet_overview():
    from services.wallet_service import get_wallet_balance
    from services.referral_commission import next_saturday_6pm_ist
    user_id = get_jwt_identity()
    conn = get_db()
    wallet = conn.execute(text("SELECT balance FROM wallet_balance WHERE user_id = :uid"), {"uid": user_id}).fetchone()
    available = wallet._mapping["balance"] if wallet else 0.0
    pending = conn.execute(text("SELECT COALESCE(SUM(amount),0) FROM wallet_transactions WHERE user_id = :uid AND status = 'locked' AND source IN ('activation_bonus','base_referral','referral_first_bonus','referral_recurring')"), {"uid": user_id}).scalar()
    next_payout = next_saturday_6pm_ist().strftime("%Y-%m-%d %H:%M IST") if next_saturday_6pm_ist else ""
    return jsonify({"available_balance": available, "pending_unlock": round(pending,2), "next_payout_ist": next_payout})
# ------------------------------
# Internal Saturday Payout (PostgreSQL)
# ------------------------------
@app.route('/internal/saturday-payout', methods=['POST'])
def saturday_payout():
    token = request.headers.get('Authorization')
    if token != f"Bearer {os.getenv('SATURDAY_PAYOUT_SECRET')}":
        return jsonify({"error": "Unauthorized"}), 403

    released = _execute_payout()
    return jsonify({"released": released}), 200

# ------------------------------
# Static / Favicon / Well‑known
# ------------------------------
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

# ------------------------------
# Error handlers
# ------------------------------
@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e.get_response()
    logger.error(traceback.format_exc())
    return jsonify({"error": str(e)}), 500

# ------------------------------
# Security middleware
# ------------------------------
from middleware.security_headers import add_security_headers
add_security_headers(app)

# ------------------------------
# Register blueprints (must be after app creation)
# ------------------------------
from extensions import init_extensions
limiter, razor_client = init_extensions(app)
register_routes(app)

@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html")

@app.route("/terms")
def terms_of_service():
    return render_template("terms.html")

@app.route('/temp-token')
def temp_token():
    if os.getenv("FLASK_ENV") != "development":
        return {"error": "Not allowed"}, 404
    token = create_access_token(
        identity='9959543954',
        additional_claims={"role": "admin"},
        expires_delta=timedelta(days=30)
    )
    return {"token": token}

# ------------------------------
# Run the app
# ------------------------------
if __name__ == "__main__":
    # Only used for local development
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"   # Default to False
    port = int(os.getenv("PORT", 8000))   # Use PORT env var with fallback 8000 for local
    app.run(host="0.0.0.0", port=port, debug=debug_mode)