"""POS billing subscription activation (Phase G-C): POST /api/pos/billing/activate.

Proves the full activation flow through the real Flask route + the real
services.pos_billing_activation.activate_paid_billing_order -- only
get_db_connection is faked, matching every other pos_* test file's
pattern. The fake models pos_billing_orders and pos_subscriptions as two
plain dicts and faithfully reproduces the two behaviors the implementation
actually depends on for correctness: `INSERT ... ON CONFLICT (owner_user_id)
DO NOTHING` (only succeeds if no row exists yet) and a conditional
`UPDATE ... WHERE owner_user_id = :uid` acting on the single existing row.

The fake has no handler for any query outside pos_billing_orders/
pos_subscriptions (in particular, nothing from Business Power/
organizations/users) -- if activation ever touched those, the fake would
raise AssertionError instead of silently succeeding.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from routes.pos_routes import pos_bp


class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class FakeResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class ActivationStore:
    def __init__(self):
        self.orders = {}
        self.subscriptions = {}

    def add_order(self, id, owner_user_id, pos_plan, amount_paise=39900,
                  currency="INR", status="paid", activated_at=None):
        self.orders[id] = {
            "id": id, "owner_user_id": owner_user_id, "pos_plan": pos_plan,
            "amount_paise": amount_paise, "currency": currency,
            "status": status, "activated_at": activated_at,
        }

    def add_subscription(self, owner_user_id, pos_plan, status="active", expires_at=None):
        self.subscriptions[owner_user_id] = {
            "owner_user_id": owner_user_id, "pos_plan": pos_plan,
            "status": status, "expires_at": expires_at,
        }

    def find_order(self, id, owner_user_id):
        row = self.orders.get(id)
        if row and row["owner_user_id"] == owner_user_id:
            return dict(row)
        return None

    def find_subscription(self, owner_user_id):
        row = self.subscriptions.get(owner_user_id)
        return dict(row) if row else None

    def try_insert_subscription(self, owner_user_id, plan, status, expires_at):
        if owner_user_id in self.subscriptions:
            return None
        row = {"owner_user_id": owner_user_id, "pos_plan": plan, "status": status, "expires_at": expires_at}
        self.subscriptions[owner_user_id] = row
        return dict(row)

    def update_subscription(self, owner_user_id, plan, status, expires_at):
        self.subscriptions[owner_user_id] = {
            "owner_user_id": owner_user_id, "pos_plan": plan, "status": status, "expires_at": expires_at,
        }

    def mark_order_activated(self, id):
        if id in self.orders:
            self.orders[id]["activated_at"] = datetime(2026, 9, 8)


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.commit_count = 0

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if q.startswith(
            "select id, owner_user_id, pos_plan, amount_paise, currency, status, "
            "activated_at from pos_billing_orders"
        ):
            row = self.store.find_order(params["id"], params["uid"])
            return FakeResult(row=FakeRow(row) if row else None)

        if q.startswith("insert into pos_subscriptions"):
            row = self.store.try_insert_subscription(
                params["uid"], params["plan"], params["status"], params["expires_at"]
            )
            return FakeResult(row=FakeRow(row) if row else None)

        if q.startswith("select pos_plan, status, expires_at from pos_subscriptions"):
            row = self.store.find_subscription(params["uid"])
            return FakeResult(row=FakeRow(row) if row else None)

        if q.startswith("update pos_subscriptions set pos_plan"):
            self.store.update_subscription(
                params["uid"], params["plan"], params["status"], params["expires_at"]
            )
            return FakeResult(row=None)

        if q.startswith("update pos_billing_orders set activated_at"):
            self.store.mark_order_activated(params["id"])
            return FakeResult(row=None)

        if q.startswith(
            "select owner_user_id, pos_plan, status, expires_at from pos_subscriptions"
        ):
            row = self.store.find_subscription(params["uid"])
            return FakeResult(row=FakeRow(row) if row else None)

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        pass

    def close(self):
        return None


def _make_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
        JWT_TOKEN_LOCATION=["headers"],
        TESTING=True,
    )
    JWTManager(app)
    app.register_blueprint(pos_bp, url_prefix="/api/pos")
    return app


class PosBillingActivationTests(unittest.TestCase):
    def setUp(self):
        self.store = ActivationStore()
        self.conn = FakeConn(self.store)
        conn_patcher = patch("routes.pos_routes.get_db_connection", lambda: self.conn)
        conn_patcher.start()
        self.addCleanup(conn_patcher.stop)

        self.app = _make_app()
        self.client = self.app.test_client()

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _activate(self, billing_order_id=1, uid=1):
        return self.client.post(
            "/api/pos/billing/activate",
            json={"billing_order_id": billing_order_id},
            headers=self._auth_headers(uid),
        )

    # ---- Authentication ----

    def test_unauthenticated_activation_rejected(self):
        res = self.client.post("/api/pos/billing/activate", json={"billing_order_id": 1})
        self.assertEqual(res.status_code, 401)

    def test_another_owner_cannot_activate_someone_elses_billing_order(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        res = self._activate(billing_order_id=1, uid=2)
        self.assertEqual(res.status_code, 404)
        self.assertIsNone(self.store.find_subscription(2))
        self.assertIsNone(self.store.find_subscription(1))

    def test_nonexistent_billing_order_behaves_safely(self):
        res = self._activate(billing_order_id=999, uid=1)
        self.assertEqual(res.status_code, 404)

    def test_another_owner_and_nonexistent_return_identical_response_shape(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        other_owner = self._activate(billing_order_id=1, uid=2)
        nonexistent = self._activate(billing_order_id=999, uid=2)
        self.assertEqual(other_owner.get_json(), nonexistent.get_json())

    # ---- Prerequisites ----

    def test_created_order_cannot_activate(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="created")
        res = self._activate()
        self.assertEqual(res.status_code, 409)
        self.assertIsNone(self.store.find_subscription(1))

    def test_failed_order_cannot_activate(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="failed")
        res = self._activate()
        self.assertEqual(res.status_code, 409)
        self.assertIsNone(self.store.find_subscription(1))

    def test_paid_order_can_activate(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        res = self._activate()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.find_subscription(1)["status"], "active")

    def test_malformed_request_missing_billing_order_id_rejected(self):
        res = self.client.post(
            "/api/pos/billing/activate", json={}, headers=self._auth_headers()
        )
        self.assertEqual(res.status_code, 400)

    # ---- New subscription ----

    def test_paid_starter_creates_active_starter_subscription(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        res = self._activate()
        body = res.get_json()
        self.assertEqual(body["plan"], "starter")
        self.assertEqual(body["subscription_status"], "active")
        sub = self.store.find_subscription(1)
        self.assertEqual(sub["pos_plan"], "starter")
        self.assertEqual(sub["status"], "active")

    def test_paid_growth_creates_active_growth_subscription(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="growth", status="paid")
        res = self._activate()
        self.assertEqual(res.get_json()["plan"], "growth")
        self.assertEqual(self.store.find_subscription(1)["pos_plan"], "growth")

    def test_correct_owner_used_from_jwt(self):
        self.store.add_order(id=1, owner_user_id=7, pos_plan="starter", status="paid")
        res = self._activate(billing_order_id=1, uid=7)
        self.assertEqual(res.status_code, 200)
        self.assertIsNotNone(self.store.find_subscription(7))
        self.assertIsNone(self.store.find_subscription(1))

    def test_no_client_controlled_plan_amount_or_limit_accepted(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        res = self.client.post(
            "/api/pos/billing/activate",
            json={
                "billing_order_id": 1, "plan": "growth", "amount": 1,
                "business_limit": 999, "owner_user_id": 999,
            },
            headers=self._auth_headers(uid=1),
        )
        self.assertEqual(res.status_code, 200)
        # The order's own stored plan (starter) wins, not the client's claim.
        self.assertEqual(self.store.find_subscription(1)["pos_plan"], "starter")

    # ---- Renewal ----

    def test_starter_renewal_extends_existing_expiry(self):
        self.store.add_subscription(1, "starter", "active", datetime(2026, 10, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        self._activate()
        self.assertEqual(self.store.find_subscription(1)["expires_at"], datetime(2026, 11, 1))

    def test_growth_renewal_extends_existing_expiry(self):
        self.store.add_subscription(1, "growth", "active", datetime(2026, 10, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="growth", status="paid")
        self._activate()
        self.assertEqual(self.store.find_subscription(1)["expires_at"], datetime(2026, 11, 1))

    def test_renewal_does_not_shorten_the_subscription(self):
        far_future = datetime(2027, 6, 1)
        self.store.add_subscription(1, "starter", "active", far_future)
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        self._activate()
        self.assertGreater(self.store.find_subscription(1)["expires_at"], far_future)

    # ---- Expiry / restart ----

    def test_expired_starter_plus_new_starter_payment_restarts_from_new_activation_time(self):
        # A safely-in-the-past date regardless of wall-clock time -- only
        # the mocked "activation now" below controls the resulting expiry.
        self.store.add_subscription(1, "starter", "active", datetime(2020, 1, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        with patch("services.pos_billing_activation.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 9, 8)
            self._activate()
        self.assertEqual(self.store.find_subscription(1)["expires_at"], datetime(2026, 10, 8))

    def test_expired_growth_plus_new_growth_payment_restarts_from_new_activation_time(self):
        self.store.add_subscription(1, "growth", "active", datetime(2020, 1, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="growth", status="paid")
        with patch("services.pos_billing_activation.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 9, 8)
            self._activate()
        sub = self.store.find_subscription(1)
        self.assertEqual(sub["pos_plan"], "growth")
        self.assertEqual(sub["expires_at"], datetime(2026, 10, 8))

    # ---- Upgrade ----

    def test_active_starter_plus_paid_growth_becomes_growth(self):
        self.store.add_subscription(1, "starter", "active", datetime(2026, 10, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="growth", status="paid")
        with patch("services.pos_billing_activation.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 9, 15)
            self._activate()
        sub = self.store.find_subscription(1)
        self.assertEqual(sub["pos_plan"], "growth")
        # Fresh period from the new activation time -- no proration/credit
        # from the old Starter expiry.
        self.assertEqual(sub["expires_at"], datetime(2026, 10, 15))

    # ---- Downgrade protection ----

    def test_active_growth_plus_paid_starter_does_not_downgrade(self):
        self.store.add_subscription(1, "growth", "active", datetime(2026, 10, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        self._activate()
        sub = self.store.find_subscription(1)
        self.assertEqual(sub["pos_plan"], "growth")
        self.assertEqual(sub["expires_at"], datetime(2026, 10, 1))

    def test_starter_payment_while_growth_active_still_marks_order_activated(self):
        self.store.add_subscription(1, "growth", "active", datetime(2026, 10, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        res = self._activate()
        self.assertEqual(res.status_code, 200)
        self.assertIsNotNone(self.store.orders[1]["activated_at"])
        self.assertFalse(res.get_json()["subscription_changed"])

    # ---- Suspended ----

    def test_suspended_subscription_reactivates_on_legitimate_payment(self):
        self.store.add_subscription(1, "starter", "suspended", datetime(2026, 12, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        res = self._activate()
        self.assertEqual(res.status_code, 200)
        sub = self.store.find_subscription(1)
        self.assertEqual(sub["status"], "active")

    # ---- Idempotency ----

    def test_same_activation_request_twice_is_safe(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        first = self._activate()
        second = self._activate()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json()["expires_at"], second.get_json()["expires_at"])

    def test_same_paid_billing_order_activated_twice_does_not_extend_expiry_again(self):
        self.store.add_subscription(1, "starter", "active", datetime(2026, 10, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        self._activate()
        first_expiry = self.store.find_subscription(1)["expires_at"]
        self._activate()
        second_expiry = self.store.find_subscription(1)["expires_at"]
        self.assertEqual(first_expiry, second_expiry)

    def test_already_activated_order_does_not_change_plan_again(self):
        self.store.add_subscription(1, "starter", "active", datetime(2026, 10, 1))
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        self._activate()

        # A second, different paid Growth order for the same owner must
        # NOT retroactively change what the first (already-activated)
        # Starter order did -- but activating IT is a legitimate new
        # event and is expected to apply its own (upgrade) policy.
        self.store.add_order(id=2, owner_user_id=1, pos_plan="growth", status="paid")
        self._activate(billing_order_id=2)
        self.assertEqual(self.store.find_subscription(1)["pos_plan"], "growth")

        # Re-activating order 1 again (already activated) must be a
        # pure no-op -- it must not revert the plan back to starter.
        res = self._activate(billing_order_id=1)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.find_subscription(1)["pos_plan"], "growth")

    def test_activation_marker_and_subscription_update_are_atomic(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        self._activate()
        order = self.store.orders[1]
        sub = self.store.find_subscription(1)
        # Both committed together -- one is never present without the other.
        self.assertIsNotNone(order["activated_at"])
        self.assertIsNotNone(sub)

    # ---- Business Power ----

    def test_business_power_owner_can_still_activate_a_paid_pos_order(self):
        # G-C never reads users/organizations at all -- the fake would
        # raise AssertionError if it tried. This proves activation
        # proceeds purely from pos_billing_orders/pos_subscriptions,
        # completely independent of Business Power status.
        self.store.add_order(id=1, owner_user_id=1, pos_plan="growth", status="paid")
        res = self._activate()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.find_subscription(1)["pos_plan"], "growth")

    # ---- Security ----

    def test_client_cannot_activate_an_unpaid_order_via_status_field(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="created")
        res = self.client.post(
            "/api/pos/billing/activate",
            json={"billing_order_id": 1, "status": "paid"},
            headers=self._auth_headers(),
        )
        self.assertEqual(res.status_code, 409)
        self.assertIsNone(self.store.find_subscription(1))

    # ---- Subscription boundary ----

    def test_activation_touches_only_pos_subscriptions_and_billing_order(self):
        self.store.add_order(id=1, owner_user_id=1, pos_plan="starter", status="paid")
        res = self._activate()
        self.assertEqual(res.status_code, 200)
        # FakeConn has no handler for users/organizations/payments -- if
        # activation touched any of them, this test would already have
        # raised AssertionError instead of reaching this line.


if __name__ == "__main__":
    unittest.main()
