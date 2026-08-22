"""Stage 8 HIGH/CRITICAL authz, upload, and exposure tests."""
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

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from services.admin_service import change_user_role
from routes.user_routes import _as_user_id


class IdentityAndUploadTests(unittest.TestCase):
    def test_as_user_id_rejects_path_payloads(self):
        self.assertIsNone(_as_user_id("../etc/passwd"))
        self.assertIsNone(_as_user_id("1/../../x"))
        self.assertIsNone(_as_user_id("not-a-number"))
        self.assertEqual(_as_user_id("12"), 12)

    def test_avatar_requires_mime_and_safe_name(self):
        src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        fn = src.split("def upload_profile_avatar")[1].split("def logout")[0]
        self.assertIn("allowed_mimes", fn)
        self.assertNotIn("gif", fn)
        self.assertIn("user_{int(user_id)}", fn)
        self.assertIn("if not user_id", fn)


class AdminPrivilegeTests(unittest.TestCase):
    def test_cannot_assign_admin_role(self):
        out = change_user_role(2, "admin", 1, "999", "1")
        self.assertEqual(out.get("_http"), 400)
        self.assertIn("error", out)

    def test_cannot_change_existing_admin(self):
        class Conn:
            def execute(self, query, params=None):
                res = MagicMock()
                q = str(getattr(query, "text", query)).lower()
                if "select role" in q:
                    res.fetchone.return_value = SimpleNamespace(_mapping={"role": "admin"})
                else:
                    res.rowcount = 0
                return res

            def commit(self):
                return None

            def close(self):
                return None

        with patch("services.admin_service.get_db_connection", return_value=Conn()):
            out = change_user_role(9, "user", 1, "999", "1")
        self.assertEqual(out.get("_http"), 403)

    def test_impersonate_blocks_admin_targets(self):
        src = (ROOT / "routes" / "admin_routes.py").read_text(encoding="utf-8")
        fn = src.split("def impersonate_user")[1].split("def admin_chart_data")[0]
        self.assertIn("Cannot impersonate an admin", fn)

    def test_legacy_admin_helpers_check_db(self):
        for rel in (
            "middleware/auth_middleware.py",
            "middleware/error_handlers.py",
            "middleware/role_required.py",
            "utils/decorators.py",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("db_user_is_admin", text)


class ExposureTests(unittest.TestCase):
    def setUp(self):
        from routes.referral_routes import referral_bp
        from routes.admin_routes import admin_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(referral_bp)
        self.app.register_blueprint(admin_bp)
        self.client = self.app.test_client()

    def test_nearby_leads_disabled(self):
        res = self.client.get("/api/nearby-leads?lat=17&lng=78")
        self.assertEqual(res.status_code, 410)
        src = (ROOT / "routes" / "referral_routes.py").read_text(encoding="utf-8")
        leads = src.split("def nearby_leads")[1].split("def invite_business")[0]
        self.assertIn("This endpoint is disabled", leads)
        self.assertNotIn("FROM business_leads", leads)

    def test_readiness_does_not_leak_errors(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        ready = src.split("def readiness")[1].split("def ")[0]
        self.assertNotIn("str(e)", ready)
        self.assertIn('"not ready"', ready)

    def test_jwt_admin_claim_cannot_impersonate(self):
        with patch("routes.admin_routes.db_user_is_admin", return_value=False):
            with self.app.app_context():
                token = create_access_token(identity="2", additional_claims={"role": "admin"})
            res = self.client.post(
                "/api/admin/users/3/impersonate",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
