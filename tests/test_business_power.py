"""Business Power V1: enterprise plan (unlimited businesses, single owner,
manual/contact-sales activation, zero referral commission)."""
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
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "rzp_test_secret")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from config.payment_config import (
    BUSINESS_POWER_PLAN,
    BUSINESS_POWER_ROLE,
    PLANS,
    get_plan_spec,
    is_extra_business_plan,
)
from services.subscription_access import (
    PAID_PLANS,
    is_subscription_active,
    get_business_limit_for_user,
)


def _future(days=30):
    return (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")


def _past(days=1):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Plan identity / access recognition
# ---------------------------------------------------------------------------
class PlanIdentityTests(unittest.TestCase):
    def test_business_power_is_a_recognized_paid_plan(self):
        self.assertIn(BUSINESS_POWER_PLAN, PAID_PLANS)

    def test_active_business_power_grants_premium_level_access(self):
        self.assertTrue(is_subscription_active({
            "plan": BUSINESS_POWER_PLAN,
            "subscription_expiry": _future(),
        }))

    def test_inactive_business_power_denied(self):
        self.assertFalse(is_subscription_active({
            "plan": BUSINESS_POWER_PLAN,
            "subscription_expiry": _past(),
        }))
        self.assertFalse(is_subscription_active({
            "plan": BUSINESS_POWER_PLAN,
            "subscription_expiry": None,
        }))

    def test_existing_three_plans_still_recognized(self):
        for plan in ("service_provider", "business_basic", "business_premium"):
            self.assertIn(plan, PAID_PLANS)
            self.assertTrue(is_subscription_active({"plan": plan, "subscription_expiry": _future()}))

    def test_business_power_never_added_to_checkout_plans(self):
        """Must never resolve via get_plan_spec — that would let it flow
        through the fixed-price Razorpay checkout (create_order)."""
        display, spec = get_plan_spec(BUSINESS_POWER_PLAN)
        self.assertIsNone(display)
        self.assertIsNone(spec)
        self.assertNotIn(BUSINESS_POWER_PLAN, PLANS)
        self.assertNotIn("Business Power", PLANS)

    def test_business_power_is_not_confused_with_extra_business(self):
        self.assertFalse(is_extra_business_plan(BUSINESS_POWER_PLAN))


# ---------------------------------------------------------------------------
# Centralized business-limit helper
# ---------------------------------------------------------------------------
class BusinessLimitHelperTests(unittest.TestCase):
    def test_business_power_is_unlimited(self):
        self.assertIsNone(get_business_limit_for_user({
            "plan": BUSINESS_POWER_PLAN, "business_limit": 0, "extra_businesses_purchased": 0,
        }))

    def test_existing_plans_keep_their_real_limits(self):
        for display, expected_limit in (
            ("Service Provider", 10),
            ("Business Basic", 1),
            ("Business Premium", 3),
        ):
            _, spec = get_plan_spec(display)
            self.assertEqual(spec["business_limit"], expected_limit)
            limit = get_business_limit_for_user({
                "plan": spec["plan"], "business_limit": spec["business_limit"],
                "extra_businesses_purchased": 0,
            })
            self.assertEqual(limit, expected_limit)

    def test_extra_purchased_slots_still_add_for_existing_plans(self):
        self.assertEqual(get_business_limit_for_user({
            "plan": "business_premium", "business_limit": 3, "extra_businesses_purchased": 2,
        }), 5)

    def test_missing_user_row_is_zero_not_unlimited(self):
        self.assertEqual(get_business_limit_for_user(None), 0)
        self.assertEqual(get_business_limit_for_user({}), 0)


# ---------------------------------------------------------------------------
# Unlimited business creation (POST /api/listing/create-listing)
# ---------------------------------------------------------------------------
class _CreateListingConn:
    def __init__(self, user_row, existing_count):
        self.user_row = user_row
        self.existing_count = existing_count
        self.inserted = False

    def execute(self, query, params=None):
        q = " ".join(str(getattr(query, "text", query)).lower().split())
        res = MagicMock()
        if "from users" in q:
            res.fetchone.return_value = SimpleNamespace(_mapping=dict(self.user_row))
            return res
        if "count(*) as cnt" in q and "from listings" in q:
            res.fetchone.return_value = SimpleNamespace(_mapping={"cnt": self.existing_count})
            return res
        if "insert into listings" in q:
            self.inserted = True
            fetch_res = MagicMock()
            fetch_res.fetchone.return_value = (999,)
            return fetch_res
        res.fetchone.return_value = None
        return res

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class UnlimitedBusinessCreationTests(unittest.TestCase):
    def setUp(self):
        from routes.listing_routes import listing_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        self.app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp()
        JWTManager(self.app)
        self.app.register_blueprint(listing_bp, url_prefix="/api/listing")
        self.client = self.app.test_client()

    def _token(self, uid=1):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"phone": "9000000000"})

    def _post(self, conn):
        with patch("routes.listing_routes.get_db_connection", return_value=conn):
            return self.client.post(
                "/api/listing/create-listing",
                data={
                    "business_name": "Test Biz",
                    "category": "retail",
                    "latitude": "12.9",
                    "longitude": "77.5",
                },
                headers={"Authorization": f"Bearer {self._token()}"},
            )

    def test_business_power_can_create_far_more_than_three_listings(self):
        user_row = {
            "id": 1, "role": BUSINESS_POWER_ROLE, "plan": BUSINESS_POWER_PLAN,
            "subscription_expiry": _future(), "is_active": 1,
            "extra_businesses_purchased": 0, "business_limit": 0,
        }
        conn = _CreateListingConn(user_row, existing_count=500)
        res = self._post(conn)
        self.assertEqual(res.status_code, 201)
        self.assertTrue(conn.inserted)

    def test_business_power_needs_no_extra_business_purchase(self):
        user_row = {
            "id": 1, "role": BUSINESS_POWER_ROLE, "plan": BUSINESS_POWER_PLAN,
            "subscription_expiry": _future(), "is_active": 1,
            "extra_businesses_purchased": 0, "business_limit": 0,
        }
        conn = _CreateListingConn(user_row, existing_count=4)
        res = self._post(conn)
        self.assertEqual(res.status_code, 201)

    def test_business_basic_still_capped_at_one(self):
        _, spec = get_plan_spec("Business Basic")
        user_row = {
            "id": 1, "role": "business_basic", "plan": "business_basic",
            "subscription_expiry": _future(), "is_active": 1,
            "extra_businesses_purchased": 0, "business_limit": spec["business_limit"],
        }
        conn = _CreateListingConn(user_row, existing_count=spec["business_limit"])
        res = self._post(conn)
        self.assertEqual(res.status_code, 403)
        self.assertFalse(conn.inserted)

    def test_business_premium_still_capped_at_three(self):
        _, spec = get_plan_spec("Business Premium")
        user_row = {
            "id": 1, "role": "business_premium", "plan": "business_premium",
            "subscription_expiry": _future(), "is_active": 1,
            "extra_businesses_purchased": 0, "business_limit": spec["business_limit"],
        }
        conn = _CreateListingConn(user_row, existing_count=spec["business_limit"] - 1)
        res = self._post(conn)
        self.assertEqual(res.status_code, 201)

        conn2 = _CreateListingConn(user_row, existing_count=spec["business_limit"])
        res2 = self._post(conn2)
        self.assertEqual(res2.status_code, 403)
        self.assertFalse(conn2.inserted)

    def test_service_provider_still_capped_at_ten(self):
        _, spec = get_plan_spec("Service Provider")
        user_row = {
            "id": 1, "role": "service_provider", "plan": "service_provider",
            "subscription_expiry": _future(), "is_active": 1,
            "extra_businesses_purchased": 0, "business_limit": spec["business_limit"],
        }
        conn = _CreateListingConn(user_row, existing_count=spec["business_limit"])
        res = self._post(conn)
        self.assertEqual(res.status_code, 403)


# ---------------------------------------------------------------------------
# Ownership / isolation — same code path, exercised with business_power role
# ---------------------------------------------------------------------------
class BusinessPowerOwnershipTests(unittest.TestCase):
    def setUp(self):
        from routes.listing_routes import listing_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(listing_bp, url_prefix="/api/listing")
        self.client = self.app.test_client()

    def _token(self, uid):
        with self.app.app_context():
            return create_access_token(
                identity=str(uid), additional_claims={"role": BUSINESS_POWER_ROLE, "phone": "9000000000"},
            )

    def _conn(self, owner_id):
        user = {
            "id": owner_id, "role": BUSINESS_POWER_ROLE, "plan": BUSINESS_POWER_PLAN,
            "subscription_expiry": _future(), "is_active": 1,
            "extra_businesses_purchased": 0, "business_limit": 0,
        }

        class Conn:
            def execute(self, query, params=None):
                q = " ".join(str(getattr(query, "text", query)).lower().split())
                res = MagicMock()
                if "from users" in q:
                    res.fetchone.return_value = SimpleNamespace(_mapping=user)
                    return res
                if "from listings" in q:
                    # Listing 1 belongs to owner_id only.
                    lid = int((params or {}).get("lid") or 0)
                    uid = (params or {}).get("uid")
                    if lid == 1 and str(uid) == str(owner_id):
                        res.fetchone.return_value = SimpleNamespace(_mapping={"id": 1})
                    else:
                        res.fetchone.return_value = None
                    return res
                res.fetchone.return_value = None
                return res

            def commit(self):
                return None

            def close(self):
                return None

        return Conn()

    def test_owner_can_reach_own_listing(self):
        with patch("routes.listing_routes.get_db_connection", return_value=self._conn(owner_id=42)):
            res = self.client.get(
                "/api/listing/listing/1",
                headers={"Authorization": f"Bearer {self._token(42)}"},
            )
        self.assertEqual(res.status_code, 200)

    def test_business_power_user_cannot_view_another_users_listing(self):
        with patch("routes.listing_routes.get_db_connection", return_value=self._conn(owner_id=42)):
            res = self.client.get(
                "/api/listing/listing/1",
                headers={"Authorization": f"Bearer {self._token(7)}"},
            )
        self.assertEqual(res.status_code, 404)

    def test_business_power_user_cannot_update_another_users_listing(self):
        with patch("routes.listing_routes.get_db_connection", return_value=self._conn(owner_id=42)):
            res = self.client.put(
                "/api/listing/update-listing/1",
                json={"business_name": "Hijack"},
                headers={"Authorization": f"Bearer {self._token(7)}"},
            )
        self.assertEqual(res.status_code, 404)

    def test_business_power_user_cannot_delete_another_users_listing(self):
        with patch("routes.listing_routes.get_db_connection", return_value=self._conn(owner_id=42)):
            res = self.client.delete(
                "/api/listing/delete-listing/1",
                headers={"Authorization": f"Bearer {self._token(7)}"},
            )
        self.assertEqual(res.status_code, 404)


class BusinessPowerAnalyticsIsolationTests(unittest.TestCase):
    def setUp(self):
        from routes.analytics_routes import analytics_api_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(analytics_api_bp, url_prefix="/api/analytics")
        self.client = self.app.test_client()

    def _token(self, uid):
        with self.app.app_context():
            return create_access_token(identity=str(uid))

    def test_every_analytics_query_is_scoped_to_the_authenticated_user(self):
        seen_uids = []

        class Conn:
            def execute(self, query, params=None):
                seen_uids.append((params or {}).get("uid"))
                res = MagicMock()
                res.fetchone.return_value = SimpleNamespace(
                    _mapping={"total_views": 0, "total_clicks": 0, "total_whatsapp": 0, "views": 0, "clicks": 0}
                )
                res.fetchall.return_value = []
                return res

        with patch("routes.analytics_routes.get_db_connection", return_value=Conn()):
            res = self.client.get(
                "/api/analytics/data",
                headers={"Authorization": f"Bearer {self._token(55)}"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(seen_uids)
        self.assertTrue(all(str(uid) == "55" for uid in seen_uids))


# ---------------------------------------------------------------------------
# Manual (admin) activation
# ---------------------------------------------------------------------------
class _UserExistsConn:
    def __init__(self, role="free", exists=True):
        self.role = role
        self.exists = exists

    def execute(self, query, params=None):
        res = MagicMock()
        if self.exists:
            res.fetchone.return_value = SimpleNamespace(_mapping={"id": params.get("user_id"), "role": self.role})
        else:
            res.fetchone.return_value = None
        return res

    def commit(self):
        # Same connection instance also serves log_admin_action's audit INSERT.
        return None

    def close(self):
        return None


class _ActivateWriteConn:
    def __init__(self, matched=True):
        self.matched = matched
        self.updates = []

    def execute(self, query, params=None):
        self.updates.append(params)
        res = MagicMock()
        res.rowcount = 1 if self.matched else 0
        return res

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class ManualActivationServiceTests(unittest.TestCase):
    def test_authorized_activation_succeeds(self):
        from services.admin_service import activate_business_power
        with patch("services.admin_service.get_db_connection", return_value=_UserExistsConn(role="free")), \
             patch("services.payment_service.get_db_connection", return_value=_ActivateWriteConn()):
            result = activate_business_power(5, 1, "9999999999", "127.0.0.1")
        self.assertEqual(result["status"], "business_power_activated")
        self.assertEqual(result["plan"], BUSINESS_POWER_PLAN)
        self.assertIn("expiry", result)

    def test_activation_writes_role_plan_expiry_atomically(self):
        from services.admin_service import activate_business_power
        write_conn = _ActivateWriteConn()
        with patch("services.admin_service.get_db_connection", return_value=_UserExistsConn(role="business_basic")), \
             patch("services.payment_service.get_db_connection", return_value=write_conn):
            activate_business_power(5, 1, "9999999999", "127.0.0.1")
        update_params = write_conn.updates[0]
        self.assertEqual(update_params["role"], BUSINESS_POWER_ROLE)
        self.assertEqual(update_params["plan"], BUSINESS_POWER_PLAN)
        self.assertIsNotNone(update_params["expiry_date"])

    def test_activation_rejects_unknown_user(self):
        from services.admin_service import activate_business_power
        with patch("services.admin_service.get_db_connection", return_value=_UserExistsConn(exists=False)):
            result = activate_business_power(999, 1, "9999999999", "127.0.0.1")
        self.assertEqual(result.get("_http"), 404)

    def test_activation_refuses_to_change_an_admin(self):
        from services.admin_service import activate_business_power
        with patch("services.admin_service.get_db_connection", return_value=_UserExistsConn(role="admin")):
            result = activate_business_power(5, 1, "9999999999", "127.0.0.1")
        self.assertEqual(result.get("_http"), 403)

    def test_activation_is_idempotent(self):
        from services.admin_service import activate_business_power
        write_conn = _ActivateWriteConn()
        with patch("services.admin_service.get_db_connection", return_value=_UserExistsConn(role=BUSINESS_POWER_ROLE)), \
             patch("services.payment_service.get_db_connection", return_value=write_conn):
            first = activate_business_power(5, 1, "9999999999", "127.0.0.1")
            second = activate_business_power(5, 1, "9999999999", "127.0.0.1")
        self.assertEqual(first["status"], "business_power_activated")
        self.assertEqual(second["status"], "business_power_activated")
        self.assertEqual(len(write_conn.updates), 2)

    def test_duration_days_clamped_to_a_safe_default(self):
        from services import admin_service
        with patch("services.admin_service.get_db_connection", return_value=_UserExistsConn(role="free")), \
             patch.object(admin_service, "activate_business_power_for_user", return_value="2027-01-01") as act:
            admin_service.activate_business_power(5, 1, "9999999999", "127.0.0.1", duration_days=999999)
        act.assert_called_once_with(5, duration_days=365)

    def test_activation_never_touches_payments_wallet_or_commission(self):
        src = (ROOT / "services" / "payment_service.py").read_text(encoding="utf-8")
        fn = src.split("def activate_business_power_for_user")[1].split("\ndef ")[0]
        self.assertNotIn("wallet_transactions", fn)
        self.assertNotIn("enqueue_referral_commission_job(", fn)
        self.assertNotIn("INSERT INTO payments", fn)
        self.assertIn("business_limit = 0", fn)


class ManualActivationRouteTests(unittest.TestCase):
    def setUp(self):
        from routes.admin_routes import admin_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(admin_bp)
        self.client = self.app.test_client()

    def _token(self, uid, role):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    def test_non_admin_cannot_activate_business_power(self):
        token = self._token(2, "business_premium")
        res = self.client.post(
            "/api/admin/users/5/activate-business-power",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(res.status_code, 403)

    def test_forged_admin_jwt_claim_without_db_role_is_rejected(self):
        """Never rely solely on the JWT claim — db_user_is_admin is the
        real, fresh-from-the-database authority."""
        with patch("routes.admin_routes.db_user_is_admin", return_value=False):
            token = self._token(2, "admin")
            res = self.client.post(
                "/api/admin/users/5/activate-business-power",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(res.status_code, 403)

    def test_unauthenticated_request_rejected(self):
        res = self.client.post("/api/admin/users/5/activate-business-power")
        self.assertEqual(res.status_code, 401)

    def test_authorized_admin_can_activate_via_route(self):
        token = self._token(1, "admin")

        class _AdminInfoConn:
            def execute(self, query, params=None):
                res = MagicMock()
                res.fetchone.return_value = SimpleNamespace(
                    _mapping={"id": 1, "phone": "9999999999", "role": "admin"}
                )
                return res

            def close(self):
                return None

        with patch("routes.admin_routes.db_user_is_admin", return_value=True), \
             patch("routes.admin_routes.get_db_connection", return_value=_AdminInfoConn()), \
             patch(
                 "routes.admin_routes.activate_business_power",
                 return_value={"status": "business_power_activated", "plan": BUSINESS_POWER_PLAN, "expiry": "2027-01-01"},
             ) as act:
            res = self.client.post(
                "/api/admin/users/5/activate-business-power",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "business_power_activated")
        act.assert_called_once()


# ---------------------------------------------------------------------------
# Referral / wallet safety
# ---------------------------------------------------------------------------
class _FakeReferralConn:
    """Minimal fake covering exactly the queries process_referral_commission
    issues, to prove it fails closed for an (hypothetical) business_power
    payment rather than guessing a commission."""

    def __init__(self, plan, amount=699000):
        self.plan = plan
        self.amount = amount
        self.wrote_anything = False

    def execute(self, query, params=None):
        q = " ".join(str(getattr(query, "text", query)).lower().split())
        res = MagicMock()
        if "from users" in q and "for update" in q:
            res.fetchone.return_value = SimpleNamespace(_mapping={
                "referred_by": 1, "first_sub_commission_paid": 0,
            })
            return res
        if "select 1 from wallet_transactions" in q:
            res.fetchone.return_value = None
            return res
        if "from payments" in q:
            res.fetchone.return_value = SimpleNamespace(_mapping={
                "plan": self.plan, "status": "activated", "user_id": 2, "amount": self.amount,
            })
            return res
        if "insert into wallet_transactions" in q or "update users set first_sub_commission_paid" in q:
            self.wrote_anything = True
            return res
        res.fetchone.return_value = None
        return res

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class ReferralCommissionSafetyTests(unittest.TestCase):
    def test_existing_three_plan_bonuses_unchanged(self):
        from services.referral_commission import FIRST_BONUS_BY_PLAN
        self.assertEqual(FIRST_BONUS_BY_PLAN, {
            "service_provider": 50.0,
            "business_basic": 100.0,
            "business_premium": 150.0,
        })

    def test_business_power_absent_from_first_bonus_map(self):
        from services.referral_commission import FIRST_BONUS_BY_PLAN
        self.assertNotIn(BUSINESS_POWER_PLAN, FIRST_BONUS_BY_PLAN)

    def test_process_referral_commission_fails_closed_for_business_power_payment(self):
        """Defense in depth: even if a payments row somehow existed with
        plan='business_power', the commission engine must refuse to guess —
        no wallet write, no first-sale flag burn."""
        from services.referral_commission import process_referral_commission, CommissionPlanLookupError
        conn = _FakeReferralConn(plan=BUSINESS_POWER_PLAN)
        with self.assertRaises(CommissionPlanLookupError):
            process_referral_commission(2, 6990.0, "pay_bp_1", conn=conn)
        self.assertFalse(conn.wrote_anything)

    def test_manual_activation_never_enqueues_a_commission_job(self):
        from services import payment_service as ps
        write_conn = _ActivateWriteConn()
        with patch("services.payment_service.get_db_connection", return_value=write_conn), \
             patch("services.payment_service.enqueue_referral_commission_job") as enqueue:
            ps.activate_business_power_for_user(5, duration_days=365)
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
