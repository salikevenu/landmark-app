"""Stage 6 listing / catalog / review integrity tests."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
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

from services.subscription_access import is_subscription_active
from services.listing_service import add_review_service, create_listing, find_nearby


def _row(mapping):
    return SimpleNamespace(_mapping=mapping, __getitem__=lambda self, i: list(mapping.values())[i])


class SubscriptionGatingTests(unittest.TestCase):
    def test_jwt_role_does_not_unlock_without_db_plan(self):
        self.assertFalse(is_subscription_active({
            "plan": "free",
            "role": "business_premium",
            "subscription_expiry": (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d"),
        }))

    def test_create_locks_user_and_forces_pending(self):
        src = (ROOT / "routes" / "listing_routes.py").read_text(encoding="utf-8")
        self.assertIn("FOR UPDATE", src)
        self.assertIn("'pending'", src)
        self.assertIn("is_premium, is_featured, is_sponsored, is_verified", src)
        self.assertIn("0, 0, 0, 0", src)
        self.assertNotIn('request.form.get("status")', src)
        self.assertIn("_paid_listing_user", src)
        update = src.split("def update_listing")[1].split("def delete_listing")[0]
        self.assertNotIn("SET status", update)
        self.assertNotIn("user_id = :new", update)


class ListingIdorHttpTests(unittest.TestCase):
    def setUp(self):
        from routes.listing_routes import listing_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        self.app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp()
        JWTManager(self.app)
        self.app.register_blueprint(listing_bp, url_prefix="/api/listing")
        self.client = self.app.test_client()
        self.owners = {1: 99}

    def _token(self, uid, role="business_premium"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role, "phone": "9000000000"})

    def _conn(self, *, paid=True, owner=99):
        expiry = (datetime.utcnow() + timedelta(days=20)).strftime("%Y-%m-%d")
        user = {
            "id": owner, "role": "business_premium" if paid else "free",
            "plan": "business_premium" if paid else "free",
            "subscription_expiry": expiry if paid else None,
            "is_active": 1, "extra_businesses_purchased": 0,
        }

        class Conn:
            def execute(self, query, params=None):
                q = " ".join(str(getattr(query, "text", query)).lower().split())
                params = params or {}
                res = MagicMock()
                if "from users" in q:
                    res.fetchone.return_value = SimpleNamespace(_mapping=user)
                    return res
                if "from listings" in q and "user_id" in q:
                    lid = int(params.get("lid") or 0)
                    uid = params.get("uid")
                    if self_owners.get(lid) == int(uid):
                        res.fetchone.return_value = SimpleNamespace(_mapping={"id": lid})
                    else:
                        res.fetchone.return_value = None
                    return res
                if "update listings" in q:
                    res.rowcount = 0
                    return res
                res.fetchone.return_value = None
                return res

            def commit(self):
                return None

            def close(self):
                return None

        self_owners = self.owners
        return Conn()

    def test_user_cannot_update_other_listing(self):
        with patch("routes.listing_routes.get_db_connection", return_value=self._conn()):
            res = self.client.put(
                "/api/listing/update-listing/1",
                json={"business_name": "Hijack", "status": "approved", "user_id": 99},
                headers={"Authorization": f"Bearer {self._token(7)}"},
            )
        self.assertEqual(res.status_code, 404)

    def test_expired_jwt_role_cannot_update(self):
        conn = self._conn(paid=True)
        expiry_past = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

        def execute(query, params=None):
            q = str(getattr(query, "text", query))
            res = MagicMock()
            if "FROM users" in q:
                res.fetchone.return_value = SimpleNamespace(_mapping={
                    "id": 99, "role": "business_premium", "plan": "business_premium",
                    "subscription_expiry": expiry_past, "is_active": 1,
                    "extra_businesses_purchased": 0,
                })
                return res
            res.fetchone.return_value = SimpleNamespace(_mapping={"id": 1})
            return res

        conn.execute = execute
        with patch("routes.listing_routes.get_db_connection", return_value=conn):
            res = self.client.put(
                "/api/listing/update-listing/1",
                json={"business_name": "Nope"},
                headers={"Authorization": f"Bearer {self._token(99)}"},
            )
        self.assertEqual(res.status_code, 403)

    def test_unauthenticated_rate_blocked(self):
        res = self.client.post("/api/listing/rate", json={"listing_id": 1, "rating": 5})
        self.assertEqual(res.status_code, 401)

    def test_pending_listing_not_in_public_detail(self):
        class Conn:
            def execute(self, query, params=None):
                res = MagicMock()
                res.fetchone.return_value = None
                return res
        with patch("routes.listing_routes.get_db_connection", return_value=Conn()):
            res = self.client.get("/api/listing/api/listing/3")
        self.assertEqual(res.status_code, 404)


class ReviewAttributionTests(unittest.TestCase):
    def test_review_uses_jwt_identity_not_client_phone(self):
        src = (ROOT / "routes" / "listing_routes.py").read_text(encoding="utf-8")
        review = src.split("def add_review")[1].split("def get_reviews")[0]
        self.assertIn("get_jwt_identity()", review)
        self.assertNotIn("get_jwt().get(\"phone\")", review)

    def test_add_review_rejects_bad_rating(self):
        out = add_review_service({"listing_id": 1, "rating": 9}, 5)
        self.assertIn("error", out)

    def test_public_reviews_omit_phone(self):
        src = (ROOT / "services" / "listing_service.py").read_text(encoding="utf-8")
        getter = src.split("def get_reviews_service")[1].split("def ")[0]
        self.assertNotIn("AS user_phone", getter)
        self.assertIn("status = 'approved'", getter)
        self.assertIn("user_name", getter)


class LegacyAndPromotionTests(unittest.TestCase):
    def test_legacy_listing_mutators_disabled(self):
        self.assertIsNone(create_listing({"user_id": 1, "business_name": "X", "latitude": 1, "longitude": 1}))
        self.assertEqual(find_nearby(1, 2), [])
        promo = (ROOT / "routes" / "promotions_routes.py").read_text(encoding="utf-8")
        self.assertIn("This endpoint is disabled", promo)
        self.assertNotIn("INSERT INTO sponsored_ads", promo)
        nearby = (ROOT / "routes" / "nearby_routes.py").read_text(encoding="utf-8")
        self.assertIn("This endpoint is disabled", nearby)
        self.assertIn("status = 'approved'", nearby)

    def test_public_browse_requires_approved(self):
        browse = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        chunk = browse.split("def api_browse")[1].split("def ")[0]
        self.assertIn("status = 'approved'", chunk)
        self.assertNotIn("status IS NULL", chunk)


if __name__ == "__main__":
    unittest.main()
