"""Stage 5 authorization, admin, OTP, listing, and debug-endpoint tests."""
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token


class DisabledDangerousEndpointsTests(unittest.TestCase):
    def setUp(self):
        from routes.admin_routes import admin_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(admin_bp)
        self.client = self.app.test_client()

    def test_make_me_admin_gone(self):
        self.assertEqual(self.client.get("/api/make-me-admin").status_code, 410)

    def test_sms_tools_disabled_even_with_jwt(self):
        with self.app.app_context():
            token = create_access_token(identity="7", additional_claims={"role": "user"})
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.post("/api/send-sms", json={"phone": "9876543210", "message": "hi"}, headers=headers).status_code, 410)
        self.assertEqual(self.client.post("/api/send-otp", json={"phone": "9876543210"}, headers=headers).status_code, 410)
        self.assertEqual(self.client.get("/api/test-sms-ui", headers=headers).status_code, 410)

    def test_migration_route_still_gone(self):
        src = (ROOT / "routes" / "admin_routes.py").read_text(encoding="utf-8")
        self.assertNotIn("ADD COLUMN IF NOT EXISTS had_first_withdrawal", src)
        self.assertNotIn("9959543954", src)


class ListingIdorTests(unittest.TestCase):
    def setUp(self):
        from routes.listing_routes import listing_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        self.app.config["UPLOAD_FOLDER"] = "/tmp/landmark-test-uploads"
        JWTManager(self.app)
        self.app.register_blueprint(listing_bp, url_prefix="/api/listing")
        self.client = self.app.test_client()
        self.owned = {}

    def _conn(self):
        class Conn:
            owned = self.owned

            def execute(self, query, params=None):
                q = " ".join(str(getattr(query, "text", query)).lower().split())
                params = params or {}
                class R:
                    def __init__(self, row=None):
                        self._row = row
                    def fetchone(self):
                        return self._row
                    def fetchall(self):
                        return []
                class Row:
                    def __init__(self, mapping):
                        self._mapping = mapping
                    def __getitem__(self, i):
                        return list(self._mapping.values())[i]
                if "from listings" in q and "user_id" in q:
                    lid = int(params.get("lid") or 0)
                    uid = str(params.get("uid"))
                    if Conn.owned.get(lid) == uid:
                        return R(Row({"id": lid}))
                    return R(None)
                if "from listings" in q and "status = 'approved'" in q:
                    return R(None)
                return R(None)

            def commit(self):
                return None

            def close(self):
                return None

        return Conn()

    def test_user_cannot_upload_image_to_other_listing(self):
        self.owned[99] = "2"
        with self.app.app_context():
            token = create_access_token(
                identity="1",
                additional_claims={"role": "business_premium", "phone": "9000000001"},
            )
        with patch("routes.listing_routes.get_db_connection", side_effect=self._conn):
            res = self.client.post(
                "/api/listing/upload-listing-image",
                data={"listing_id": "99", "image": (BytesIO(b"\x89PNG\r\n\x1a\n"), "x.png")},
                content_type="multipart/form-data",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(res.status_code, 404)

    def test_pending_listing_not_public(self):
        with patch("routes.listing_routes.get_db_connection", side_effect=self._conn):
            res = self.client.get("/api/listing/api/listing/1")
        self.assertEqual(res.status_code, 404)

    def test_delete_uses_user_id(self):
        src = (ROOT / "routes" / "listing_routes.py").read_text(encoding="utf-8")
        self.assertIn("WHERE id = :lid AND user_id = :uid", src)
        self.assertNotIn("AND user_phone = :phone", src)


class ReferralInfoAuthTests(unittest.TestCase):
    def test_referral_info_ignores_client_user_id(self):
        src = (ROOT / "routes" / "referral_routes.py").read_text(encoding="utf-8")
        self.assertIn("@jwt_required()", src)
        self.assertIn("get_jwt_identity()", src)
        self.assertNotIn("request.args.get(\"user_id\")", src)


class OtpDebugLockTests(unittest.TestCase):
    def test_debug_sms_disabled_on_render(self):
        with patch.dict(os.environ, {"DEBUG_SMS": "true", "RENDER": "true",
                                     "MESSAGE_CENTRAL_CUSTOMER_ID": "cid",
                                     "MESSAGE_CENTRAL_AUTH_TOKEN": "tok"}, clear=False):
            import importlib
            import services.sms_service as sms
            importlib.reload(sms)
            svc = sms.MessageCentralSMS()
            self.assertFalse(svc.debug_mode)
        import importlib
        import services.sms_service as sms
        importlib.reload(sms)

    def test_legacy_otp_service_is_noop(self):
        from auth.otp_service import send_otp, verify_otp
        self.assertFalse(send_otp("9876543210"))
        self.assertFalse(verify_otp("9876543210", "123456"))


class SetupAgentsMoneySafetyTests(unittest.TestCase):
    def test_generator_cannot_credit_wallet(self):
        src = (ROOT / "setup_agents.py").read_text(encoding="utf-8")
        self.assertNotIn("wallet_balance.balance +", src)
        self.assertIn("Use POST /api/payment/verify-payment", src)


class WalletIsolationTests(unittest.TestCase):
    def test_wallet_overview_uses_jwt_identity(self):
        src = (ROOT / "routes" / "wallet_routes.py").read_text(encoding="utf-8")
        self.assertIn("user_id = get_jwt_identity()", src)
        self.assertNotIn("data.get(\"user_id\")", src)
        withdraw = (ROOT / "routes" / "withdraw_routes.py").read_text(encoding="utf-8")
        self.assertIn("db_user_is_admin", withdraw)


class MiddlewareAdminDbCheckTests(unittest.TestCase):
    def test_shared_admin_required_checks_database(self):
        src = (ROOT / "middleware" / "admin_required.py").read_text(encoding="utf-8")
        self.assertIn("db_user_is_admin", src)
        self.assertIn("services.authz", src)


class OrchestrationDisabledTests(unittest.TestCase):
    def test_subscription_and_maintenance_410(self):
        src = (ROOT / "routes" / "orchestration_routes.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("This endpoint is disabled"), 3)


class ReviewsNoLiveAlterTests(unittest.TestCase):
    def test_reviews_do_not_alter_on_request(self):
        src = (ROOT / "routes" / "reviews_routes.py").read_text(encoding="utf-8")
        self.assertNotIn("ALTER TABLE reviews", src)
        user = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        self.assertNotIn("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url", user)


if __name__ == "__main__":
    unittest.main()
