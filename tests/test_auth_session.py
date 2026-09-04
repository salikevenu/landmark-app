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


def _authz_conn(role):
    """Fakes services.authz.get_db_connection's single fetchone() query,
    same minimal shape as tests/test_ranger_security.py's _conn helper."""
    class Conn:
        def execute(self, query, params=None):
            res = MagicMock()
            res.fetchone.return_value = SimpleNamespace(_mapping={"role": role}) if role is not None else None
            return res

        def close(self):
            return None

    return Conn()


class AdminLoginSessionRedirectTests(unittest.TestCase):
    """/admin/login must not show a fresh OTP form to an already-authenticated
    admin — that's what made Android Chrome's back button (and revisiting a
    bookmarked/autocompleted /admin/login URL) look like a lost session even
    when the cookies were still perfectly valid."""

    def setUp(self):
        from routes.admin_routes import admin_bp
        self.app = Flask(__name__, template_folder=str(ROOT / "templates"))
        self.app.config.update(
            SECRET_KEY="test-secret",
            JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
            JWT_TOKEN_LOCATION=["cookies", "headers"],
            JWT_COOKIE_SECURE=False,
            JWT_COOKIE_CSRF_PROTECT=False,
            JWT_ACCESS_COOKIE_NAME="access_token",
        )
        JWTManager(self.app)
        self.app.register_blueprint(admin_bp)
        self.client = self.app.test_client()

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    def test_unauthenticated_admin_login_still_renders_otp_form(self):
        res = self.client.get("/admin/login")
        self.assertEqual(res.status_code, 200)

    def test_authenticated_admin_is_redirected_to_dashboard(self):
        self.client.set_cookie("access_token", self._token(1, role="admin"))
        with patch("services.authz.get_db_connection", return_value=_authz_conn("admin")):
            res = self.client.get("/admin/login", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/admin/dashboard", res.headers.get("Location", ""))

    def test_authenticated_non_admin_cannot_reach_admin_dashboard_this_way(self):
        """A normal authenticated user's valid session must never be treated
        as an admin session, regardless of what the JWT role claim says."""
        self.client.set_cookie("access_token", self._token(2, role="free"))
        with patch("services.authz.get_db_connection", return_value=_authz_conn("free")):
            res = self.client.get("/admin/login", follow_redirects=False)
        self.assertEqual(res.status_code, 200)

    def test_forged_admin_role_claim_without_db_confirmation_still_sees_login_form(self):
        """Same double-check admin_required already relies on: the JWT role
        claim alone is not enough, db_user_is_admin must also agree."""
        self.client.set_cookie("access_token", self._token(3, role="admin"))
        with patch("services.authz.get_db_connection", return_value=_authz_conn("free")):
            res = self.client.get("/admin/login", follow_redirects=False)
        self.assertEqual(res.status_code, 200)

    def test_expired_admin_token_does_not_crash_admin_login(self):
        """Must not weaken or bypass the existing expired-token handling —
        just must not blow up with an unhandled error either."""
        with self.app.app_context():
            token = create_access_token(
                identity="1", additional_claims={"role": "admin"}, expires_delta=timedelta(seconds=-5)
            )
        self.client.set_cookie("access_token", token)
        res = self.client.get("/admin/login")
        self.assertIn(res.status_code, (200, 302, 401, 422))

    def test_current_request_is_admin_does_not_swallow_expired_or_invalid_tokens(self):
        """Only a genuinely MISSING token may be treated as 'not logged in'
        here. An expired/invalid token must keep propagating to the app's
        existing global JWT error handlers (the silent-refresh path every
        other protected admin page already relies on) instead of being
        caught and misreported as 'no session' by this helper."""
        src = (ROOT / "routes" / "admin_routes.py").read_text(encoding="utf-8")
        fn_src = src.split("def _current_request_is_admin")[1].split("\ndef ")[0]
        self.assertIn("verify_jwt_in_request(optional=True)", fn_src)
        self.assertNotIn("except ", fn_src)
        self.assertNotIn("except:", fn_src)

    def test_admin_login_success_uses_location_replace_not_href(self):
        """location.replace (not .href =) drops /admin/login from browser
        history, so Android Chrome's back button from the dashboard can no
        longer land back on a stale login form."""
        html = (ROOT / "templates" / "admin" / "admin_login.html").read_text(encoding="utf-8")
        self.assertIn('window.location.replace("/admin/dashboard")', html)
        self.assertNotIn('window.location.href = "/admin/dashboard"', html)


class UserLoginSessionRedirectTests(unittest.TestCase):
    """/api/auth/public/login must not show a fresh OTP form to an
    already-authenticated user — the regular-user counterpart to the
    deployed AdminLoginSessionRedirectTests fix above, addressing the
    reported "asks for OTP again" symptom (Chrome back button, a
    bookmark, or autocomplete landing directly on the login URL even
    though the JWT cookies are still perfectly valid)."""

    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.app = flask_app
        self.client = flask_app.test_client()

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    # 1. Unauthenticated -> OTP login page, HTTP 200
    def test_unauthenticated_public_login_still_renders_otp_form(self):
        res = self.client.get("/api/auth/public/login")
        self.assertEqual(res.status_code, 200)

    # 2. Authenticated regular user -> HTTP 302 to /dashboard
    def test_authenticated_regular_user_is_redirected_to_dashboard(self):
        self.client.set_cookie("access_token", self._token(1, role="free"))
        res = self.client.get("/api/auth/public/login", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/dashboard", res.headers.get("Location", ""))

    # 3. Authenticated non-admin still treated as a regular user -> /dashboard
    def test_authenticated_non_admin_redirected_to_dashboard_not_admin(self):
        self.client.set_cookie("access_token", self._token(2, role="business_basic"))
        res = self.client.get("/api/auth/public/login", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/dashboard", res.headers.get("Location", ""))
        self.assertNotIn("/admin/dashboard", res.headers.get("Location", ""))

    # 4. Expired/invalid JWT: existing global handling remains intact, no crash
    def test_expired_token_does_not_crash_public_login(self):
        with self.app.app_context():
            token = create_access_token(identity="1", expires_delta=timedelta(seconds=-5))
        self.client.set_cookie("access_token", token)
        res = self.client.get("/api/auth/public/login")
        self.assertIn(res.status_code, (200, 302, 401, 422))

    def test_invalid_token_does_not_crash_public_login(self):
        self.client.set_cookie("access_token", "not-a-jwt")
        res = self.client.get("/api/auth/public/login")
        self.assertIn(res.status_code, (200, 302, 401, 422))

    # 5. Source-level: successful public login navigation uses .replace, not .href
    def test_login_success_navigation_uses_location_replace(self):
        html = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        self.assertIn(
            "window.location.replace((role === 'admin') ? '/admin/dashboard' : '/dashboard');",
            html,
        )
        self.assertNotIn("window.location.href = (role === 'admin')", html)

    # 6. Source-level: LandmarkSession.redirectToLogin uses .replace, not .href
    def test_session_js_redirect_to_login_uses_location_replace(self):
        js = (ROOT / "static" / "js" / "session.js").read_text(encoding="utf-8")
        self.assertIn("window.location.replace(LOGIN_URL);", js)
        self.assertNotIn("window.location.href = LOGIN_URL", js)

    # 7. Direct regression for the reported symptom
    def test_dashboard_then_back_to_login_with_valid_cookies_redirects_not_form(self):
        token = self._token(3, role="free")
        self.client.set_cookie("access_token", token)
        dash = self.client.get("/dashboard", follow_redirects=True)
        self.assertEqual(dash.status_code, 200)
        res = self.client.get("/api/auth/public/login", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/dashboard", res.headers.get("Location", ""))

    def test_current_request_is_authenticated_user_does_not_swallow_expired_or_invalid_tokens(self):
        """Only a genuinely MISSING token may be treated as 'not logged in'
        here — an expired/invalid token must keep propagating to the app's
        existing global JWT error handlers (the silent-refresh path)."""
        src = (ROOT / "routes" / "auth_routes.py").read_text(encoding="utf-8")
        fn_src = src.split("def _current_request_is_authenticated_user")[1].split("\ndef ")[0]
        self.assertIn("verify_jwt_in_request(optional=True)", fn_src)
        self.assertNotIn("except ", fn_src)
        self.assertNotIn("except:", fn_src)


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

    def test_admin_payments_uses_authfetch_not_hardcoded_redirect(self):
        """Regression: admin_payments.html used to fetch() /api/admin/payments raw
        and jump straight to /admin/login on a 401 with no refresh attempt —
        logging an admin out just because the 2h access token had expired."""
        html = (ROOT / "templates" / "admin" / "admin_payments.html").read_text(encoding="utf-8")
        self.assertIn("LandmarkSession.authFetch", html)
        self.assertIn('"/api/admin/payments"', html)
        self.assertNotIn('window.location.href = "/admin/login"', html)

    def test_no_admin_page_bypasses_authfetch_for_admin_api_calls(self):
        """Every admin page that calls /api/admin/* must route it through
        LandmarkSession.authFetch (silent refresh + retry) somewhere in the
        file, and must not hardcode a straight-to-login redirect off a bare
        fetch()'s 401 — that combination is what let an expired access token
        log an admin out instead of silently refreshing."""
        import re
        admin_dir = ROOT / "templates" / "admin"
        hardcoded_redirect_on_401 = re.compile(
            r'status\s*===\s*401[^}]*window\.location\.href\s*=\s*[\'"]/admin/login[\'"]',
            re.DOTALL,
        )
        offenders = []
        for path in sorted(admin_dir.glob("*.html")):
            content = path.read_text(encoding="utf-8")
            if "/api/admin" not in content:
                continue
            uses_authfetch = "LandmarkSession.authFetch" in content
            hardcoded_redirect = hardcoded_redirect_on_401.search(content)
            if not uses_authfetch or hardcoded_redirect:
                offenders.append(path.name)
        self.assertEqual(offenders, [])


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
