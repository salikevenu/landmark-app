"""Stage 7.1 admin-granted sponsorship vs unpaid self-serve ads."""
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

from services.admin_service import sponsor_listing_admin
from services.listing_service import sponsor_listing_service


class SponsorStore:
    def __init__(self, listings):
        self.listings = listings
        self.ads = []
        self.wallet_writes = 0
        self.payment_writes = 0
        self.commission_writes = 0

    def execute(self, query, params=None):
        q = " ".join(str(getattr(query, "text", query)).lower().split())
        params = params or {}
        res = MagicMock()
        res.rowcount = 0
        res.fetchone.return_value = None
        if q.startswith("update listings") and "is_sponsored = 1" in q:
            lid = int(params.get("listing_id"))
            row = self.listings.get(lid)
            if row and row["status"] == "approved" and row["is_active"] == 1:
                row["is_sponsored"] = 1
                res.fetchone.return_value = SimpleNamespace(_mapping={
                    "id": lid, "user_id": row["user_id"]
                })
                res.rowcount = 1
            return res
        if "from listings" in q and "where id" in q:
            lid = int(params.get("listing_id"))
            row = self.listings.get(lid)
            if row:
                res.fetchone.return_value = SimpleNamespace(_mapping=row)
            return res
        if q.startswith("insert into sponsored_ads"):
            if "admin_grant" not in q:
                raise AssertionError("sponsorship plan must be admin_grant")
            if "'admin_grant', 0," not in q:
                raise AssertionError("sponsorship must record amount 0")
            self.ads.append({"amount": 0, **dict(params)})
            res.rowcount = 1
            return res
        if "wallet" in q or "balance +" in q:
            self.wallet_writes += 1
        if "insert into payments" in q:
            self.payment_writes += 1
        if "referral_commission" in q:
            self.commission_writes += 1
        if "insert into admin_audit_log" in q:
            res.rowcount = 1
        return res

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class AdminSponsorCasTests(unittest.TestCase):
    def test_approved_active_can_be_sponsored_unpaid(self):
        store = SponsorStore({1: {"id": 1, "user_id": 9, "status": "approved", "is_active": 1}})
        with patch("services.admin_service.get_db_connection", return_value=store):
            out = sponsor_listing_admin(1, 1, "999", "127.0.0.1")
        self.assertEqual(out["status"], "sponsored")
        self.assertFalse(out["paid"])
        self.assertEqual(store.ads[0]["amount"], 0)
        self.assertEqual(store.wallet_writes, 0)
        self.assertEqual(store.payment_writes, 0)
        self.assertEqual(store.commission_writes, 0)

    def test_pending_inactive_missing(self):
        pending = SponsorStore({1: {"id": 1, "user_id": 9, "status": "pending", "is_active": 1}})
        with patch("services.admin_service.get_db_connection", return_value=pending):
            self.assertEqual(sponsor_listing_admin(1, 1, "9", "1").get("_http"), 409)
        inactive = SponsorStore({1: {"id": 1, "user_id": 9, "status": "approved", "is_active": 0}})
        with patch("services.admin_service.get_db_connection", return_value=inactive):
            self.assertEqual(sponsor_listing_admin(1, 1, "9", "1").get("_http"), 409)
        missing = SponsorStore({})
        with patch("services.admin_service.get_db_connection", return_value=missing):
            self.assertEqual(sponsor_listing_admin(99, 1, "9", "1").get("_http"), 404)

    def test_legacy_service_fail_closed(self):
        self.assertIn("error", sponsor_listing_service(1))
        src = (ROOT / "services" / "listing_service.py").read_text(encoding="utf-8")
        fn = src.split("def sponsor_listing_service")[1].split("def ")[0]
        self.assertNotIn("is_sponsored = 1", fn)
        self.assertNotIn("INSERT INTO sponsored_ads", fn)


class SponsorHttpAuthTests(unittest.TestCase):
    def setUp(self):
        from routes.admin_routes import admin_bp
        from routes.listing_routes import listing_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(admin_bp)
        self.app.register_blueprint(listing_bp, url_prefix="/api/listing")
        self.client = self.app.test_client()

    def _token(self, uid, role):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role, "phone": "9000000000"})

    def test_normal_user_cannot_sponsor(self):
        res = self.client.post(
            "/api/admin/listings/1/sponsor",
            headers={"Authorization": f"Bearer {self._token(2, 'business_premium')}"},
        )
        self.assertEqual(res.status_code, 403)

    def test_jwt_admin_without_db_role_cannot_sponsor(self):
        with patch("routes.admin_routes.db_user_is_admin", return_value=False):
            res = self.client.post(
                "/api/admin/listings/1/sponsor",
                headers={"Authorization": f"Bearer {self._token(2, 'admin')}"},
            )
        self.assertEqual(res.status_code, 403)

    def test_create_and_update_ignore_sponsored_featured(self):
        src = (ROOT / "routes" / "listing_routes.py").read_text(encoding="utf-8")
        create = src.split("def api_create_listing")[1].split("def my_listings")[0]
        self.assertIn("0, 0, 0, 0", create)
        self.assertNotIn('request.form.get("is_sponsored")', create)
        self.assertNotIn('request.form.get("is_featured")', create)
        update = src.split("def update_listing")[1].split("def delete_listing")[0]
        self.assertNotIn("is_sponsored", update)
        self.assertNotIn("is_featured", update)
        self.assertNotIn("SET status", update)


class SelfServePromotionTests(unittest.TestCase):
    def test_onboard_disabled_and_page_not_paid(self):
        promo = (ROOT / "routes" / "promotions_routes.py").read_text(encoding="utf-8")
        self.assertIn("This endpoint is disabled", promo)
        self.assertNotIn("INSERT INTO sponsored_ads", promo)
        html = (ROOT / "templates" / "promotions" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Pay with Razorpay", html)
        self.assertNotIn("handlePayment", html)
        self.assertNotIn("₹99", html)
        self.assertIn("Self-serve advertising is not available", html)

    def test_no_unauthorized_public_sponsor_route(self):
        listing = (ROOT / "routes" / "listing_routes.py").read_text(encoding="utf-8")
        self.assertNotIn("/sponsor", listing)
        self.assertNotIn("sponsor_listing_service", listing)

    def test_setup_agents_cannot_regenerate_ad_payment(self):
        src = (ROOT / "setup_agents.py").read_text(encoding="utf-8")
        self.assertNotIn("sponsored_ads", src)
        self.assertNotIn("is_sponsored = 1", src)
        self.assertNotIn("wallet_balance.balance +", src)
        ads = (ROOT / "agents" / "ads_agent.py").read_text(encoding="utf-8")
        self.assertNotIn("sponsored_ads", ads)
        self.assertNotIn("razorpay", ads.lower())

    def test_admin_sponsor_does_not_touch_money_tables(self):
        src = (ROOT / "services" / "admin_service.py").read_text(encoding="utf-8")
        fn = src.split("def sponsor_listing_admin")[1].split("def get_admin_payments")[0]
        self.assertIn("admin_grant", fn)
        self.assertIn('"paid": False', fn)
        self.assertNotIn("wallet_balance", fn)
        self.assertNotIn("process_referral_commission", fn)
        self.assertNotIn("INSERT INTO payments", fn)


if __name__ == "__main__":
    unittest.main()
