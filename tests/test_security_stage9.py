"""Stage 9 HIGH/CRITICAL API and operational abuse tests."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    create_refresh_token,
    jwt_required,
)

from services.jwt_blocklist import reset_memory_for_tests
from services.jwt_session import register_jwt_security, revoke_tokens_from_request


def _conn(row_mapping):
    class Conn:
        def execute(self, query, params=None):
            res = MagicMock()
            res.fetchone.return_value = (
                SimpleNamespace(_mapping=row_mapping) if row_mapping is not None else None
            )
            return res

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Conn()


class JwtBlocklistTests(unittest.TestCase):
    def setUp(self):
        reset_memory_for_tests()
        self.app = Flask(__name__)
        self.app.config.update(
            SECRET_KEY="test-secret",
            JWT_SECRET_KEY="test-jwt-secret",
            JWT_TOKEN_LOCATION=["headers"],
            JWT_COOKIE_CSRF_PROTECT=False,
        )
        jwt = JWTManager(self.app)
        register_jwt_security(jwt)

        @self.app.route("/protected")
        @jwt_required()
        def protected():
            return jsonify({"ok": True})

        @self.app.route("/logout", methods=["POST"])
        def logout():
            revoke_tokens_from_request()
            return jsonify({"ok": True})

        self.lookup = patch(
            "services.jwt_session.get_db_connection",
            side_effect=RuntimeError("no db"),
        )
        self.lookup.start()
        self.client = self.app.test_client()

    def tearDown(self):
        self.lookup.stop()
        reset_memory_for_tests()

    def test_logout_revokes_bearer_access_token(self):
        with self.app.app_context():
            token = create_access_token(identity="11")
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/protected", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/logout", headers=headers).status_code, 200)
        res = self.client.get("/protected", headers=headers)
        self.assertEqual(res.status_code, 401)
        self.assertIn("revoked", (res.get_json() or {}).get("error", "").lower())

    def test_logout_revokes_refresh_token(self):
        with self.app.app_context():
            refresh = create_refresh_token(identity="11")

        @self.app.route("/refresh-check", methods=["POST"])
        @jwt_required(refresh=True)
        def refresh_check():
            return jsonify({"ok": True})

        headers = {"Authorization": f"Bearer {refresh}"}
        self.assertEqual(self.client.post("/refresh-check", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/refresh-check", headers=headers).status_code, 401)


class BannedUserJwtTests(unittest.TestCase):
    def setUp(self):
        reset_memory_for_tests()
        self.app = Flask(__name__)
        self.app.config.update(
            JWT_SECRET_KEY="test-jwt-secret",
            JWT_TOKEN_LOCATION=["headers"],
            JWT_COOKIE_CSRF_PROTECT=False,
        )
        jwt = JWTManager(self.app)
        register_jwt_security(jwt)

        @self.app.route("/protected")
        @jwt_required()
        def protected():
            return jsonify({"ok": True})

        self.client = self.app.test_client()

    def tearDown(self):
        reset_memory_for_tests()

    def test_blocked_user_token_is_rejected(self):
        with patch(
            "services.jwt_session.get_db_connection",
            return_value=_conn({"id": 11, "is_blocked": 1, "is_active": 1}),
        ):
            with self.app.app_context():
                token = create_access_token(identity="11")
            res = self.client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 401)

    def test_inactive_user_token_is_rejected(self):
        with patch(
            "services.jwt_session.get_db_connection",
            return_value=_conn({"id": 11, "is_blocked": 0, "is_active": 0}),
        ):
            with self.app.app_context():
                token = create_access_token(identity="11")
            res = self.client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 401)


class RefreshAndInternalTests(unittest.TestCase):
    def setUp(self):
        from app import app as flask_app
        self.app = flask_app
        self.app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def test_refresh_missing_user_does_not_issue_token(self):
        with self.app.app_context():
            refresh = create_refresh_token(identity="99")
        with patch("services.jwt_session.get_db_connection", side_effect=RuntimeError("no db")), patch(
            "database.init_db.get_db_connection", return_value=_conn(None)
        ):
            res = self.client.post(
                "/api/refresh",
                headers={"Authorization": f"Bearer {refresh}"},
            )
        self.assertEqual(res.status_code, 401)
        self.assertFalse((res.get_json() or {}).get("success"))

    def test_refresh_db_error_does_not_issue_token(self):
        with self.app.app_context():
            refresh = create_refresh_token(identity="99")
        with patch("services.jwt_session.get_db_connection", side_effect=RuntimeError("no db")), patch(
            "database.init_db.get_db_connection", side_effect=RuntimeError("db down")
        ):
            res = self.client.post(
                "/api/refresh",
                headers={"Authorization": f"Bearer {refresh}"},
            )
        self.assertEqual(res.status_code, 503)

    def test_refresh_blocked_user_denied_by_lookup(self):
        with self.app.app_context():
            refresh = create_refresh_token(identity="99")
        with patch(
            "services.jwt_session.get_db_connection",
            return_value=_conn({"id": 99, "is_blocked": 1, "is_active": 1}),
        ):
            res = self.client.post(
                "/api/refresh",
                headers={"Authorization": f"Bearer {refresh}"},
            )
        self.assertEqual(res.status_code, 401)

    def test_payout_secret_cannot_reuse_jwt_secret(self):
        from app import _internal_job_authorized
        env = {
            "SATURDAY_PAYOUT_SECRET": "shared-secret-value",
            "JWT_SECRET_KEY": "shared-secret-value",
            "SECRET_KEY": "different-app-secret",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.app.test_request_context(
                "/internal/saturday-payout",
                headers={"Authorization": "Bearer shared-secret-value"},
            ):
                self.assertFalse(_internal_job_authorized())

    def test_payout_secret_compare_digest_accepts_distinct_secret(self):
        from app import _internal_job_authorized
        env = {
            "SATURDAY_PAYOUT_SECRET": "payout-secret-value",
            "JWT_SECRET_KEY": "jwt-secret-value-xx",
            "SECRET_KEY": "app-secret-value-xx",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.app.test_request_context(
                "/internal/saturday-payout",
                headers={"Authorization": "Bearer payout-secret-value"},
            ):
                self.assertTrue(_internal_job_authorized())
            with self.app.test_request_context(
                "/internal/saturday-payout",
                headers={"Authorization": "Bearer wrong-secret-value"},
            ):
                self.assertFalse(_internal_job_authorized())

    def test_empty_payout_secret_denied(self):
        from app import _internal_job_authorized
        with patch.dict(os.environ, {"SATURDAY_PAYOUT_SECRET": ""}, clear=False):
            with self.app.test_request_context(
                "/internal/saturday-payout",
                headers={"Authorization": "Bearer anything"},
            ):
                self.assertFalse(_internal_job_authorized())


class FailClosedAndDisclosureTests(unittest.TestCase):
    def test_execute_query_disabled(self):
        from app import execute_query
        with self.assertRaises(RuntimeError):
            execute_query("SELECT 1")

    def test_unmounted_geo_service_is_disabled(self):
        from services.geo_service import geo_bp, find_nearby_friends
        app = Flask(__name__)
        app.register_blueprint(geo_bp)
        res = app.test_client().get("/api/distance?lat1=1&lon1=1&lat2=2&lon2=2")
        self.assertEqual(res.status_code, 410)
        with self.assertRaises(RuntimeError):
            find_nearby_friends(17.3, 78.4, 5)

    def test_service_add_post_disabled(self):
        src = (ROOT / "routes" / "service_routes.py").read_text(encoding="utf-8")
        self.assertIn("This endpoint is disabled", src)
        self.assertNotIn("INSERT INTO services", src)

    def test_public_browse_does_not_leak_exceptions(self):
        src = (ROOT / "routes" / "listing_routes.py").read_text(encoding="utf-8")
        fn = src.split("def browse_api")[1].split("def add_review")[0]
        self.assertNotIn("str(e)", fn)
        self.assertIn("Something went wrong. Please try again.", fn)

    def test_authenticated_error_paths_do_not_leak_exceptions(self):
        for rel, marker in (
            ("routes/reviews_routes.py", 'return jsonify({"success": False, "error":'),
            ("routes/transaction_routes.py", 'return jsonify({"success": False, "error":'),
            ("routes/referral_routes.py", 'return jsonify({"error":'),
        ):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("str(e)", src)
            self.assertIn(marker, src)

    def test_limiter_prefers_redis_and_ignores_forwarded_for(self):
        src = (ROOT / "extensions.py").read_text(encoding="utf-8")
        self.assertIn("REDIS_URL", src)
        self.assertIn("get_remote_address", src)
        self.assertNotIn("X-Forwarded-For", src)

    def test_internal_auth_uses_compare_digest(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        fn = src.split("def _internal_job_authorized")[1].split("@app.route")[0]
        self.assertIn("hmac.compare_digest", src)
        self.assertIn("must be distinct", fn)
        self.assertNotIn('token == f"Bearer {expected}"', fn)

    def test_logout_routes_revoke_tokens(self):
        auth = (ROOT / "routes" / "auth_routes.py").read_text(encoding="utf-8")
        user = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("revoke_tokens_from_request()", auth.split("def logout")[1].split("def get_current_user")[0])
        self.assertIn("revoke_tokens_from_request()", user.split("def logout")[1].split("PLAN_DETAILS")[0])
        self.assertIn("revoke_tokens_from_request()", app_src.split("def logout_page")[1].split("def set_language")[0])


if __name__ == "__main__":
    unittest.main()
