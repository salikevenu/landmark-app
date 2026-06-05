import os
import secrets
import logging
import random
import re
import string
import json
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, render_template, current_app
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
    unset_jwt_cookies
)

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Initialize Firebase Admin SDK (once)
# ------------------------------------------------------------
def init_firebase():
    """Initialize Firebase Admin SDK using service account file or environment variable."""
    if firebase_admin._apps:
        return  # already initialized
    # Try to load from file (local development)
    if os.path.exists("firebase-service-account.json"):
        cred = credentials.Certificate("firebase-service-account.json")
        firebase_admin.initialize_app(cred)
        current_app.logger.info("Firebase Admin initialized from file")
    else:
        # Try from environment variable (production on Render)
        firebase_creds = os.getenv("FIREBASE_SERVICE_ACCOUNT")
        if firebase_creds:
            try:
                cred_dict = json.loads(firebase_creds)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                current_app.logger.info("Firebase Admin initialized from env var")
            except Exception as e:
                current_app.logger.error(f"Failed to parse Firebase credentials from env: {e}")
        else:
            current_app.logger.warning("Firebase credentials not found. Phone auth will not work.")

# We'll call init_firebase inside the first request or at app startup.
# To ensure it runs, we can call it when the blueprint is registered.
# Alternatively, call it inside the /firebase-login endpoint (lazy init).
# Here we will call it inside the endpoint to avoid issues with app context.

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
        logger.exception("Registration error")
        return jsonify({"error": "Internal server error"}), 500

# =================================
# FIREBASE PHONE AUTH LOGIN
# =================================
@auth_bp.route("/firebase-login", methods=["POST"])
def firebase_login():
    """Verify Firebase ID token and issue JWT for your app."""
    # Initialize Firebase (if not already)
    init_firebase()

    data = request.get_json() or {}
    id_token = data.get("idToken")
    phone = data.get("phone")
    remember_me = data.get("remember_me", False)

    if not id_token or not phone:
        return jsonify({"error": "Missing idToken or phone"}), 400

    # Verify the Firebase ID token
    try:
        decoded = firebase_auth.verify_id_token(id_token)
        firebase_phone = decoded.get("phone_number")
        # Ensure the phone number matches (Firebase uses E.164 format: +91XXXXXXXXXX)
        if firebase_phone != "+91" + phone:
            current_app.logger.warning(f"Phone mismatch: {firebase_phone} vs +91{phone}")
            return jsonify({"error": "Phone number mismatch"}), 400
    except Exception as e:
        current_app.logger.error(f"Firebase token verification failed: {e}")
        return jsonify({"error": "Invalid verification"}), 401

    # Find or create user in your database
    conn = get_db()
    user = conn.execute(
        text("SELECT id, name, role, referral_code FROM users WHERE phone = :phone"),
        {"phone": phone}
    ).fetchone()

    if user:
        user_id = user._mapping["id"]
        role = user._mapping["role"]
        referral_code = user._mapping["referral_code"]
    else:
        # Create new user with the provided phone number (name can be added later)
        referral_code = generate_referral_code()
        result = conn.execute(text("""
            INSERT INTO users (phone, name, role, referral_code, created_at)
            VALUES (:phone, :name, 'free', :code, CURRENT_TIMESTAMP)
            RETURNING id
        """), {
            "phone": phone,
            "name": "",   # empty name, user can update later
            "code": referral_code
        })
        user_id = result.fetchone()[0]
        conn.commit()
        role = "free"

    # JWT expiry based on remember_me
    if remember_me:
        access_expires = timedelta(days=30)
        refresh_expires = timedelta(days=365)
    else:
        access_expires = timedelta(minutes=15)
        refresh_expires = timedelta(days=7)

    access_token = create_access_token(
        identity=str(user_id),
        additional_claims={"role": role, "phone": phone},
        expires_delta=access_expires
    )
    refresh_token = create_refresh_token(
        identity=str(user_id),
        expires_delta=refresh_expires
    )

    response = jsonify({
        "status": "success",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {
            "id": user_id,
            "phone": phone,
            "role": role,
            "referral_code": referral_code
        }
    })
    set_access_cookies(response, access_token, max_age=access_expires)
    set_refresh_cookies(response, refresh_token, max_age=refresh_expires)
    return response, 200

# =================================
# LOGOUT (stateless)
# =================================
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