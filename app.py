# app.py
import os
import traceback
from datetime import timedelta, datetime
from dotenv import load_dotenv
from flask import Flask, g, request, redirect, render_template, session, jsonify, send_from_directory
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from werkzeug.exceptions import HTTPException
from flask_jwt_extended import unset_jwt_cookies
from flask_cors import CORS
import sqlite3
from flask import make_response

# Load environment variables
load_dotenv()

# Database path
from database.init_db import get_db, close_db, DB_PATH

# Blueprint registration
from routes import register_routes

# Initialize Flask app
app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv("SECRET_KEY", "landmark-super-secret-change-me")

# Close database connection at the end of each request
app.teardown_appcontext(close_db)

# ------------------------------
# Configuration (JWT only)
# ------------------------------
app.config.update(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    UPLOAD_FOLDER="static/uploads",
    # JWT configuration
    JWT_SECRET_KEY=os.getenv("JWT_SECRET_KEY", "your-secure-jwt-secret-key"),
    JWT_ACCESS_TOKEN_EXPIRES=timedelta(days=1),
    JWT_REFRESH_TOKEN_EXPIRES=timedelta(days=30),
    JWT_TOKEN_LOCATION=["headers"],
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
# Database helper
# ------------------------------

def execute_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(query, params)
    if commit:
        conn.commit()
    if fetchone:
        result = cur.fetchone()
    elif fetchall:
        result = cur.fetchall()
    else:
        result = None
    return result
# ------------------------------
# Helper: subscription check
# ------------------------------
def is_subscription_active(user_dict):
    plan = user_dict.get("plan", "free")
    if plan == "free":
        return True   # ✅ Free plan is always active
    # Paid plans must have a valid expiry date
    expiry_str = user_dict.get("subscription_expiry")
    if not expiry_str:
        return False  # Paid plan but no expiry date? Treat as expired.
    try:
        expiry_date = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
        return expiry_date >= datetime.now()
    except Exception:
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

# ------------------------------
# Web Routes (public or language-only session)
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
    # Public listing page – client‑side JWT for any user actions
    return redirect("/api/user/browse")

from flask import redirect

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
    return render_template("users/wallet.html")   # create a minimal template

@app.route("/transactions")
def transactions_page():
    return render_template("users/transactions.html")

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
    # Language preference is the only session data we keep
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
    user = execute_query("SELECT * FROM users WHERE id=?", (user_id,), fetchone=True)
    if not user:
        return jsonify({"error": "User not found"}), 404

    user_dict = dict(user)

    # First: check if the user's plan is active (paid plans only)
    if user_dict["plan"] == "free":
        return jsonify({"error": "This feature requires a paid plan. Please upgrade."}), 403

    if not is_subscription_active(user_dict):
        return jsonify({"error": "Your plan has expired. Please upgrade."}), 403

    if user_dict["role"] != "business_owner":
        return jsonify({"error": "Only business users can add businesses."}), 403

    count = execute_query("SELECT COUNT(*) as cnt FROM businesses WHERE user_id=?", (user_id,), fetchone=True)["cnt"]
    if count >= user_dict.get("business_limit", 0):
        return jsonify({"error": "Business limit reached. Upgrade your plan."}), 403

    name = request.json.get("name") if request.is_json else request.form.get("name")
    if not name:
        return jsonify({"error": "Business name required"}), 400

    execute_query(
        "INSERT INTO businesses (user_id, name) VALUES (?, ?)",
        (user_id, name), commit=True
    )
    return jsonify({"message": "Business added successfully"}), 201

# ------------------------------
# Static / Favicon / Well‑known (ignore)
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
    return render_template("terms.html")  # you'll create this too if needed

# ------------------------------
# Run the app
# -
# -----------------------------
if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "True").lower() == "true"
    app.run(host="0.0.0.0", port=8000, debug=debug_mode)