"""Authentication/session consistency: cookie JWT, no localStorage tokens."""
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import importlib.util

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    jwt_required,
    get_jwt_identity,
    get_jwt,
    set_access_cookies,
    unset_jwt_cookies,
)


def _csrf_from_client(client):
    return client.get_cookie("csrf_access_token")


def make_session_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
        JWT_TOKEN_LOCATION=["cookies", "headers"],
        JWT_COOKIE_SECURE=False,
        JWT_COOKIE_SAMESITE="Lax",
        JWT_COOKIE_HTTPONLY=True,
        JWT_COOKIE_CSRF_PROTECT=True,
        JWT_ACCESS_COOKIE_NAME="access_token",
        JWT_COOKIE_CSRF_HEADER_NAME="X-CSRF-TOKEN",
    )
    jwt = JWTManager(app)

    @jwt.unauthorized_loader
    def missing(_reason):
        return jsonify({"success": False, "error": "Authentication required"}), 401

    @jwt.expired_token_loader
    def expired(_h, _p):
        return jsonify({"success": False, "error": "Session expired"}), 401

    @jwt.invalid_token_loader
    def invalid(_reason):
        return jsonify({"success": False, "error": "Invalid session"}), 401

    @app.route("/api/auth/verify-otp", methods=["POST"])
    def verify_otp():
        data = request.get_json(silent=True) or {}
        if data.get("otp") != "123456":
            return jsonify({"success": False, "message": "Incorrect OTP"}), 401
        token = create_access_token(
            identity="42",
            additional_claims={"role": "free", "phone": "9876543210"},
        )
        resp = jsonify({
            "success": True,
            "message": "Login successful",
            "data": {"status": "existing", "user": {"id": 42, "role": "free", "phone": "9876543210"}},
        })
        set_access_cookies(resp, token)
        return resp, 200

    @app.route("/api/me")
    @jwt_required()
    def me():
        return jsonify({"success": True, "user_id": get_jwt_identity()})

    @app.route("/api/listing/create-listing", methods=["POST"])
    @jwt_required()
    def create_listing():
        claims = get_jwt()
        if claims.get("role") not in ("service_provider", "business_basic", "business_premium"):
            return jsonify({"success": False, "error": "Active subscription required"}), 403
        return jsonify({"success": True, "listing_id": 1}), 201

    @app.route("/logout", methods=["POST"])
    def logout():
        resp = jsonify({"success": True})
        unset_jwt_cookies(resp)
        return resp

    return app


class CookieSessionTests(unittest.TestCase):
    def setUp(self):
        self.app = make_session_app()
        self.client = self.app.test_client()

    def test_otp_verification_sets_httponly_cookie(self):
        res = self.client.post("/api/auth/verify-otp", json={"phone": "9876543210", "otp": "123456"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["success"])
        self.assertNotIn("access_token", body)
        cookie = self.client.get_cookie("access_token")
        self.assertIsNotNone(cookie)
        set_cookie = "".join(res.headers.getlist("Set-Cookie"))
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("access_token=", set_cookie)

    def test_cookie_authenticated_request_reaches_protected_endpoint(self):
        self.client.post("/api/auth/verify-otp", json={"phone": "9876543210", "otp": "123456"})
        res = self.client.get("/api/me")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["user_id"], "42")

    def test_authenticated_request_works_without_localstorage_or_bearer(self):
        self.client.post("/api/auth/verify-otp", json={"phone": "9876543210", "otp": "123456"})
        res = self.client.get("/api/me")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["user_id"], "42")

    def test_unauthenticated_protected_request_is_401(self):
        res = self.client.get("/api/me")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["error"], "Authentication required")

    def test_invalid_jwt_rejected(self):
        self.client.set_cookie("access_token", "not-a-jwt")
        res = self.client.get("/api/me")
        self.assertEqual(res.status_code, 401)
        self.assertIn("session", res.get_json()["error"].lower())

    def test_expired_jwt_rejected(self):
        with self.app.app_context():
            token = create_access_token(identity="42", expires_delta=timedelta(seconds=-5))
        self.client.set_cookie("access_token", token)
        res = self.client.get("/api/me")
        self.assertEqual(res.status_code, 401)

    def test_logout_rejects_subsequent_protected_request(self):
        self.client.post("/api/auth/verify-otp", json={"phone": "9876543210", "otp": "123456"})
        csrf = _csrf_from_client(self.client)
        headers = {}
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf.value
        res = self.client.post("/logout", headers=headers)
        self.assertEqual(res.status_code, 200)
        follow = self.client.get("/api/me")
        self.assertEqual(follow.status_code, 401)

    def test_authenticated_but_unauthorized_is_403(self):
        self.client.post("/api/auth/verify-otp", json={"phone": "9876543210", "otp": "123456"})
        csrf = _csrf_from_client(self.client)
        headers = {"X-CSRF-TOKEN": csrf.value} if csrf else {}
        res = self.client.post("/api/listing/create-listing", headers=headers)
        self.assertEqual(res.status_code, 403)


class FrontendAuthGateTests(unittest.TestCase):
    def test_create_listing_does_not_stop_on_empty_localstorage(self):
        html = (ROOT / "templates" / "users" / "create_listing.html").read_text(encoding="utf-8")
        self.assertNotIn("localStorage.getItem", html)
        self.assertIn("LandmarkSession.authFetch", html)
        self.assertIn("/api/listing/create-listing", html)

    def test_login_does_not_store_tokens_in_localstorage(self):
        login = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        self.assertNotIn("localStorage.setItem", login)
        session_js = (ROOT / "static" / "js" / "session.js").read_text(encoding="utf-8")
        self.assertNotIn("localStorage.setItem", session_js)
        self.assertIn("credentials: \"include\"", session_js)

    def test_otp_login_uses_credentials_include(self):
        login = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        self.assertIn("credentials: 'include'", login)
        self.assertIn("/api/auth/verify-otp", login)


class AppJwtConfigTests(unittest.TestCase):
    def test_refresh_cookie_path_matches_refresh_route(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('JWT_REFRESH_COOKIE_PATH="/api/refresh"', src)
        self.assertIn("JWT_COOKIE_HTTPONLY=True", src)
        self.assertIn('JWT_ACCESS_COOKIE_PATH="/"', src)
        self.assertIn('JWT_COOKIE_PATH="/"', src)
        self.assertIn('JWT_COOKIE_SAMESITE="Lax"', src)
        self.assertIn("JWT_COOKIE_CSRF_PROTECT=True", src)
        self.assertIn("JWT_ACCESS_CSRF_COOKIE_NAME", src)
        self.assertIn("JWT_REFRESH_CSRF_COOKIE_PATH", src)
        self.assertIn('_cookie_secure = os.getenv("RENDER") == "true"', src)
        self.assertIn("JWT_COOKIE_SECURE=_cookie_secure", src)
        self.assertNotIn('JWT_REFRESH_COOKIE_PATH="/token/refresh"', src)

    def test_session_js_refresh_does_not_recurse(self):
        js = (ROOT / "static" / "js" / "session.js").read_text(encoding="utf-8")
        self.assertIn("refreshInFlight", js)
        self.assertIn("/api/refresh", js)
        self.assertIn('credentials: "include"', js)
        self.assertIn("csrf_access_token", js)
        self.assertIn("csrf_refresh_token", js)
        self.assertIn("isRefreshCall", js)


if __name__ == "__main__":
    unittest.main()
