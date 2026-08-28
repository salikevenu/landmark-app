"""Stage 7 reviews, admin approval CAS, and legacy-path tests."""
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
from sqlalchemy.exc import IntegrityError

from services.admin_service import approve_listing_admin
from services.listing_service import (
    add_review_service,
    delete_review_service,
    disable_listing_service,
    get_reviews_service,
    update_review_service,
    verify_listing_service,
    delete_listing_service,
)
from services.wallet_service import process_referral, add_pending_referral_reward
from services.referral_service import process_referral_reward


def _ns(mapping):
    return SimpleNamespace(_mapping=mapping)


class FakeListingConn:
    def __init__(self, *, exists=True, status="pending", is_active=1):
        self.exists = exists
        self.status = status
        self.is_active = is_active
        self.approvals = 0
        self.commits = 0

    def execute(self, query, params=None):
        q = " ".join(str(getattr(query, "text", query)).lower().split())
        res = MagicMock()
        if q.startswith("update listings") and "status = 'pending'" in q:
            if self.exists and self.status == "pending" and self.is_active == 1:
                self.status = "approved"
                self.approvals += 1
                res.rowcount = 1
            else:
                res.rowcount = 0
            return res
        if "from listings" in q and "where id" in q:
            if not self.exists:
                res.fetchone.return_value = None
            else:
                res.fetchone.return_value = _ns({
                    "id": params.get("listing_id"),
                    "status": self.status,
                    "is_active": self.is_active,
                })
            return res
        if "insert into admin_audit_log" in q:
            res.rowcount = 1
            return res
        res.rowcount = 0
        res.fetchone.return_value = None
        return res

    def commit(self):
        self.commits += 1

    def rollback(self):
        return None

    def close(self):
        return None


class AdminApprovalCasTests(unittest.TestCase):
    def test_pending_active_approves_once(self):
        conn = FakeListingConn()
        with patch("services.admin_service.get_db_connection", return_value=conn):
            first = approve_listing_admin(3, 1, "999", "127.0.0.1")
            second = approve_listing_admin(3, 1, "999", "127.0.0.1")
        self.assertEqual(first["status"], "approved")
        self.assertEqual(second.get("_http"), 409)
        self.assertEqual(conn.approvals, 1)

    def test_already_approved_conflict(self):
        conn = FakeListingConn(status="approved")
        with patch("services.admin_service.get_db_connection", return_value=conn):
            out = approve_listing_admin(3, 1, "999", "1")
        self.assertEqual(out.get("_http"), 409)
        self.assertNotEqual(out.get("status"), "approved")

    def test_rejected_and_inactive_conflict(self):
        conn = FakeListingConn(status="rejected")
        with patch("services.admin_service.get_db_connection", return_value=conn):
            out = approve_listing_admin(3, 1, "999", "1")
        self.assertEqual(out.get("_http"), 409)
        conn2 = FakeListingConn(is_active=0)
        with patch("services.admin_service.get_db_connection", return_value=conn2):
            out2 = approve_listing_admin(3, 1, "999", "1")
        self.assertEqual(out2.get("_http"), 409)

    def test_nonexistent_not_found(self):
        conn = FakeListingConn(exists=False)
        with patch("services.admin_service.get_db_connection", return_value=conn):
            out = approve_listing_admin(99, 1, "999", "1")
        self.assertEqual(out.get("_http"), 404)

    def test_cas_sql_in_source(self):
        src = (ROOT / "services" / "admin_service.py").read_text(encoding="utf-8")
        self.assertIn("AND status = 'pending'", src)
        self.assertIn("AND is_active = 1", src)
        self.assertIn("rowcount != 1", src)
        route = (ROOT / "routes" / "admin_routes.py").read_text(encoding="utf-8")
        approve = route.split("def api_approve_listing")[1].split("def ")[0]
        self.assertIn("_http", approve)
        self.assertIn("db_user_is_admin", route)


class ReviewConn:
    def __init__(self, *, listing=True, user=True, existing=False, update_ok=False, owned_id=None):
        self.listing = listing
        self.user = user
        self.existing = existing
        self.update_ok = update_ok
        self.owned_id = owned_id
        self.inserts = []
        self.updates = []
        self.deletes = []

    def execute(self, query, params=None):
        q = " ".join(str(getattr(query, "text", query)).lower().split())
        params = params or {}
        res = MagicMock()
        res.rowcount = 0
        res.fetchone.return_value = None
        res.fetchall.return_value = []
        if "from listings" in q:
            res.fetchone.return_value = _ns({"id": 1}) if self.listing else None
            return res
        if "from users" in q:
            res.fetchone.return_value = _ns({"id": 5, "phone": "9000000005"}) if self.user else None
            return res
        if q.startswith("select id from reviews") and "user_id" in q:
            res.fetchone.return_value = _ns({"id": 8}) if self.existing else None
            return res
        if q.startswith("insert into reviews"):
            self.inserts.append(dict(params))
            res.rowcount = 1
            return res
        if q.startswith("update reviews"):
            self.updates.append(dict(params))
            res.rowcount = 1 if self.update_ok else 0
            return res
        if q.startswith("select listing_id from reviews"):
            if self.update_ok or (self.owned_id and params.get("rid") == self.owned_id and params.get("uid") == 5):
                res.fetchone.return_value = _ns({"listing_id": 1})
                res.rowcount = 1
            else:
                res.fetchone.return_value = None
            return res
        if q.startswith("delete from reviews"):
            self.deletes.append(dict(params))
            ok = self.owned_id and params.get("rid") == self.owned_id and int(params.get("uid")) == 5
            res.rowcount = 1 if ok else 0
            return res
        if q.startswith("update listings"):
            res.rowcount = 1
            return res
        if "from reviews r" in q:
            res.fetchall.return_value = [
                _ns({"user_name": "Ann", "rating": 5, "review": "ok", "owner_reply": None, "created_at": None})
            ]
            return res
        return res

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class ReviewIdentityTests(unittest.TestCase):
    def test_inserts_jwt_user_id_not_client_identity(self):
        conn = ReviewConn()
        with patch("services.listing_service.get_db_connection", return_value=conn):
            out = add_review_service({
                "listing_id": 1,
                "rating": 4,
                "review": "nice",
                "user_id": 99,
                "user_phone": "8111111111",
                "phone": "8111111111",
            }, 5)
        self.assertEqual(out.get("status"), "review_added")
        self.assertEqual(conn.inserts[0]["user_id"], 5)
        self.assertEqual(conn.inserts[0]["user_phone"], "9000000005")
        self.assertNotEqual(conn.inserts[0]["user_phone"], "8111111111")

    def test_unapproved_listing_rejected(self):
        conn = ReviewConn(listing=False)
        with patch("services.listing_service.get_db_connection", return_value=conn):
            out = add_review_service({"listing_id": 1, "rating": 5}, 5)
        self.assertEqual(out.get("_http"), 404)
        self.assertEqual(conn.inserts, [])

    def test_duplicate_conflict(self):
        conn = ReviewConn(existing=True)
        with patch("services.listing_service.get_db_connection", return_value=conn):
            out = add_review_service({"listing_id": 1, "rating": 5}, 5)
        self.assertEqual(out.get("_http"), 409)

    def test_integrity_error_conflict(self):
        conn = ReviewConn()

        def execute(query, params=None):
            q = " ".join(str(getattr(query, "text", query)).lower().split())
            if q.startswith("insert into reviews"):
                raise IntegrityError("INSERT", {}, Exception("duplicate"))
            return ReviewConn.execute(conn, query, params)

        conn.execute = execute
        with patch("services.listing_service.get_db_connection", return_value=conn):
            out = add_review_service({"listing_id": 1, "rating": 5}, 5)
        self.assertEqual(out.get("_http"), 409)

    def test_idor_update_and_delete(self):
        conn = ReviewConn(update_ok=False, owned_id=11)
        with patch("services.listing_service.get_db_connection", return_value=conn):
            upd = update_review_service(11, {"rating": 2, "user_id": 5}, 7)
            dele = delete_review_service(11, 7)
        self.assertEqual(upd.get("_http"), 404)
        self.assertEqual(dele.get("_http"), 404)

    def test_owner_can_update_own_review(self):
        conn = ReviewConn(update_ok=True, owned_id=11)
        with patch("services.listing_service.get_db_connection", return_value=conn):
            out = update_review_service(11, {"rating": 3, "listing_id": 99, "user_id": 1}, 5)
        self.assertEqual(out.get("status"), "review_updated")
        self.assertNotIn("listing_id", conn.updates[0])

    def test_public_reviews_omit_phone(self):
        conn = ReviewConn()
        with patch("services.listing_service.get_db_connection", return_value=conn):
            out = get_reviews_service(1)
        self.assertIn("reviews", out)
        self.assertNotIn("user_phone", out["reviews"][0])
        src = (ROOT / "services" / "listing_service.py").read_text(encoding="utf-8")
        getter = src.split("def get_reviews_service")[1]
        self.assertNotIn("AS user_phone", getter)
        self.assertIn("u.id = r.user_id", getter)


class ReviewHttpIdorTests(unittest.TestCase):
    def setUp(self):
        from routes.listing_routes import listing_bp
        from routes.reviews_routes import reviews_api_bp
        from routes.admin_routes import admin_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(listing_bp, url_prefix="/api/listing")
        self.app.register_blueprint(reviews_api_bp)
        self.app.register_blueprint(admin_bp)
        self.client = self.app.test_client()

    def _token(self, uid, role="user"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role, "phone": "9000000000"})

    def test_unauthenticated_create_blocked(self):
        res = self.client.post("/api/listing/review", json={"listing_id": 1, "rating": 5})
        self.assertEqual(res.status_code, 401)

    def test_user_cannot_delete_other_review_http(self):
        with patch("routes.listing_routes.delete_review_service", return_value={"error": "Review not found", "_http": 404}):
            res = self.client.delete(
                "/api/listing/review/4",
                headers={"Authorization": f"Bearer {self._token(2)}"},
            )
        self.assertEqual(res.status_code, 404)

    def test_non_admin_cannot_approve(self):
        with patch("routes.admin_routes.db_user_is_admin", return_value=False):
            res = self.client.post(
                "/api/admin/listings/1/approve",
                headers={"Authorization": f"Bearer {self._token(2, role='user')}"},
            )
        self.assertEqual(res.status_code, 403)

    def test_owner_reply_requires_listing_owner(self):
        class Conn:
            def execute(self, query, params=None):
                res = MagicMock()
                res.rowcount = 0
                return res

            def commit(self):
                return None

            def close(self):
                return None

        with patch("routes.reviews_routes.get_db_connection", return_value=Conn()):
            res = self.client.post(
                "/api/reviews/reply",
                json={"review_id": 1, "reply": "thanks", "user_id": 1},
                headers={"Authorization": f"Bearer {self._token(9)}"},
            )
        self.assertEqual(res.status_code, 404)


class LegacyAndMigrationTests(unittest.TestCase):
    def test_unrouted_listing_mutators_disabled(self):
        self.assertIn("error", disable_listing_service(1))
        self.assertIn("error", verify_listing_service(1))
        self.assertIn("error", delete_listing_service(1))

    def test_legacy_wallet_paths_do_not_credit(self):
        self.assertIsNone(process_referral(1, 1000))
        self.assertFalse(add_pending_referral_reward(1, 1))
        self.assertIsNone(process_referral_reward(1, "premium", "pay_x"))
        import importlib.util

        def _load(name, rel):
            spec = importlib.util.spec_from_file_location(name, ROOT / rel)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        referral = _load("referral_agent_s7", "agents/referral_agent.py")
        payment = _load("payment_agent_s7", "agents/payment_agent.py")
        self.assertFalse(referral.ReferralAgent().process_referral_reward(1, "premium").get("success"))
        self.assertFalse(payment.PaymentAgent().verify_payment("pay", 1).get("success"))

    def test_setup_agents_cannot_recreate_wallet_credit(self):
        src = (ROOT / "setup_agents.py").read_text(encoding="utf-8")
        self.assertNotIn("wallet_balance.balance +", src)
        self.assertIn("legacy_referral_agent_disabled", src)
        self.assertIn("PaymentAgent.verify_payment must not credit wallets", src)

    def test_additive_migration_not_auto_applied(self):
        mig = ROOT / "migrations" / "add_reviews_user_id_unique.py"
        src = mig.read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS user_id", src)
        self.assertIn("uq_reviews_listing_user", src)
        self.assertIn("Do not apply this migration to production", src)
        self.assertNotIn("DROP COLUMN", src)
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("add_reviews_user_id_unique", app_src)
        init_src = (ROOT / "database" / "init_db.py").read_text(encoding="utf-8")
        self.assertNotIn("uq_reviews_listing_user", init_src)

    def test_no_new_catalog_tables(self):
        for path in (
            ROOT / "database" / "init_db.py",
            ROOT / "migrations" / "add_reviews_user_id_unique.py",
        ):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("CREATE TABLE IF NOT EXISTS products", src)
            self.assertNotIn("CREATE TABLE IF NOT EXISTS services", src)
            self.assertNotIn("CREATE TABLE IF NOT EXISTS promotions", src)


if __name__ == "__main__":
    unittest.main()
