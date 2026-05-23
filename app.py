# app.py
import os
import traceback
from datetime import timedelta, datetime
from dotenv import load_dotenv
from flask import Flask, g, request, redirect, render_template, session, jsonify, send_from_directory, make_response
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity, unset_jwt_cookies, verify_jwt_in_request
from werkzeug.exceptions import HTTPException
from flask_cors import CORS
from sqlalchemy import text

# Load environment variables
load_dotenv()

# Database connection (PostgreSQL via SQLAlchemy)
from database.init_db import get_db

# Blueprint registration
from routes import register_routes

# Initialize Flask app
app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv("SECRET_KEY", "landmark-super-secret-change-me")

# ------------------------------
# Configuration (JWT & uploads)
# ------------------------------
app.config.update(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    UPLOAD_FOLDER="static/uploads",
    JWT_SECRET_KEY=os.getenv("JWT_SECRET_KEY", "your-secure-jwt-secret-key"),
    JWT_ACCESS_TOKEN_EXPIRES=timedelta(days=1),
    JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=30),
    JWT_TOKEN_LOCATION=["cookies", "headers"],
    JWT_COOKIE_SECURE=False,
    JWT_COOKIE_CSRF_PROTECT=False,
    JWT_ACCESS_COOKIE_PATH="/",
    JWT_ACCESS_COOKIE_NAME="access_token",
    JWT_REFRESH_COOKIE_NAME="refresh_token",
    JWT_REFRESH_COOKIE_PATH="/token/refresh",
)

# Initialize JWT manager
jwt = JWTManager(app)

# ------------------------------
# Ensure required folders exist
# ------------------------------
os.makedirs("static/uploads", exist_ok=True)
os.makedirs("static/images/listings", exist_ok=True)
os.makedirs("static/qrcodes", exist_ok=True)

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

# ------------------------------
# Translations (simple)
# ------------------------------
def get_translation(lang):
    translations = {
        "en": {
            "hero_title": "Discover Nearby Businesses",
            "hero_subtitle": "Earn money through finds & referrals",
            "get_started": "Get Started",
            "login": "Login",
        }
    }
    return translations.get(lang, translations["en"])

@app.before_request
def log_every_request():
    print(f"🔷 {request.method} {request.path}")

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
    t = get_translation(lang)
    return dict(t=t, current_lang=lang)

# ------------------------------
# Web Routes (public)
# ------------------------------
@app.route("/")
def index():
    lang = session.get("lang", "en")
    t = get_translation(lang)
    return render_template("public/index.html", t=t)

@app.route("/dashboard")
def redirect_dashboard():
    return redirect("/api/user/dashboard")

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

@app.route("/set-language", methods=["POST"])
def set_language():
    data = request.get_json()
    session["lang"] = data.get("lang", "en")
    return {"status": "ok"}

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

# ------------------------------
# Internal Saturday Payout (PostgreSQL)
# ------------------------------
@app.route('/internal/saturday-payout', methods=['POST'])
def saturday_payout():
    token = request.headers.get('Authorization')
    if token != 'Bearer YOUR_LONG_SECRET_KEY':
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_db()
    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    locked = conn.execute(text("""
        SELECT id, user_id, amount
        FROM wallet_transactions
        WHERE type = 'credit'
          AND source IN ('referral_first_bonus', 'referral_recurring')
          AND status = 'locked'
          AND unlock_at <= :now
    """), {"now": now_utc}).fetchall()

    released_count = 0
    for row in locked:
        uid = row._mapping["user_id"]
        amt = row._mapping["amount"]
        tid = row._mapping["id"]

        # Upsert wallet_balance: add amount
        conn.execute(text("""
            INSERT INTO wallet_balance (user_id, balance, updated_at)
            VALUES (:uid, :amt, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET balance = wallet_balance.balance + :amt2,
                updated_at = NOW()
        """), {"uid": uid, "amt": amt, "amt2": amt})

        # Update users.wallet_balance for quick display
        conn.execute(text("UPDATE users SET wallet_balance = wallet_balance + :amt WHERE id = :uid"),
                     {"amt": amt, "uid": uid})

        # Mark transaction as released
        conn.execute(text("UPDATE wallet_transactions SET status = 'released' WHERE id = :tid"),
                     {"tid": tid})

        released_count += 1

    conn.commit()
    return jsonify({"released": released_count}), 200

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
    traceback.print_exc()
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
    debug_mode = os.getenv("FLASK_DEBUG", "True").lower() == "true"
    app.run(host="0.0.0.0", port=8000, debug=debug_mode)