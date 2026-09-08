"""POS billing order foundation (Phase G-A): POST /api/pos/billing/orders.

Creates a Razorpay order and a pos_billing_orders row for the
authenticated owner's intended POS plan. Does NOT verify payment and does
NOT touch pos_subscriptions/users -- that is proven directly here by
never giving the fake connection a handler for any users/pos_subscriptions
query, so an unexpected read there raises immediately.

No real database connection -- routes.pos_routes.get_db_connection is
patched with an in-memory fake, matching every other pos_* test file's
pattern. No real Razorpay call -- routes.pos_routes.get_razorpay_client
is patched with a MagicMock, matching tests/test_payment_lifecycle.py's
convention.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from sqlalchemy.exc import IntegrityError

from routes.pos_routes import pos_bp


class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class FakeResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class BillingOrderStore:
    def __init__(self):
        self.rows = []
        self.next_id = 1
        self.duplicate_razorpay_order_id = None

    def insert(self, params):
        if params["rzp_order_id"] == self.duplicate_razorpay_order_id:
            raise IntegrityError("insert", params, Exception("duplicate key"))
        row = {
            "id": self.next_id,
            "owner_user_id": params["uid"],
            "pos_plan": params["plan"],
            "amount_paise": params["amount"],
            "currency": "INR",
            "razorpay_order_id": params["rzp_order_id"],
            "status": "created",
            "created_at": "2026-09-08T00:00:00",
        }
        self.rows.append(row)
        self.next_id += 1
        return row


class FakeConn:
    """Deliberately has no handler for any users/pos_subscriptions query --
    an unexpected read there is a bug (this endpoint must never resolve
    entitlement), and would raise AssertionError, failing the test loudly.
    """

    def __init__(self, store):
        self.store = store
        self.committed = False
        self.rolled_back = False

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}
        if q.startswith("insert into pos_billing_orders"):
            row = self.store.insert(params)
            return FakeResult(row=FakeRow(dict(row)))
        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

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


class PosBillingOrdersTests(unittest.TestCase):
    def setUp(self):
        self.store = BillingOrderStore()
        self.conn = FakeConn(self.store)
        conn_patcher = patch("routes.pos_routes.get_db_connection", lambda: self.conn)
        conn_patcher.start()
        self.addCleanup(conn_patcher.stop)

        self.rzp = MagicMock()
        self.rzp.order.create.return_value = {"id": "order_rzp_1"}
        rzp_patcher = patch("routes.pos_routes.get_razorpay_client", return_value=self.rzp)
        rzp_patcher.start()
        self.addCleanup(rzp_patcher.stop)

        key_patcher = patch(
            "routes.pos_routes.get_razorpay_key_pair",
            return_value=("rzp_test_publickey", "supersecretvalue"),
        )
        key_patcher.start()
        self.addCleanup(key_patcher.stop)

        self.app = _make_app()
        self.client = self.app.test_client()

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _post(self, body=None, uid=1, headers=None):
        return self.client.post(
            "/api/pos/billing/orders",
            json=body if body is not None else {"plan": "starter"},
            headers=headers if headers is not None else self._auth_headers(uid),
        )

    # ---- Authentication ----

    def test_unauthenticated_request_rejected(self):
        res = self.client.post("/api/pos/billing/orders", json={"plan": "starter"})
        self.assertEqual(res.status_code, 401)

    def test_authenticated_owner_can_create_an_order(self):
        res = self._post({"plan": "starter"})
        self.assertEqual(res.status_code, 201)

    # ---- Plan validation ----

    def test_starter_accepted(self):
        res = self._post({"plan": "starter"})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["plan"], "starter")

    def test_growth_accepted(self):
        res = self._post({"plan": "growth"})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["plan"], "growth")

    def test_invalid_plan_rejected(self):
        res = self._post({"plan": "enterprise"})
        self.assertEqual(res.status_code, 400)
        self.rzp.order.create.assert_not_called()

    def test_business_power_is_not_a_purchasable_plan(self):
        res = self._post({"plan": "business_power"})
        self.assertEqual(res.status_code, 400)
        self.rzp.order.create.assert_not_called()

    def test_missing_plan_rejected(self):
        res = self._post({})
        self.assertEqual(res.status_code, 400)

    def test_client_supplied_amount_is_ignored(self):
        res = self._post({"plan": "starter", "amount": 1})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["amount"], 39900)
        self.assertEqual(self.rzp.order.create.call_args[0][0]["amount"], 39900)

    def test_client_supplied_user_id_is_ignored(self):
        res = self._post({"plan": "starter", "user_id": 999}, uid=7)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.store.rows[0]["owner_user_id"], 7)

    def test_client_supplied_business_limit_is_ignored(self):
        res = self._post({"plan": "starter", "business_limit": 999})
        self.assertEqual(res.status_code, 201)
        # The response never echoes a business_limit at all -- this
        # endpoint deals only in plan/amount/currency/order identity.
        self.assertNotIn("business_limit", res.get_json())

    # ---- Pricing ----

    def test_starter_always_resolves_to_39900_paise(self):
        res = self._post({"plan": "starter", "amount": 1})
        self.assertEqual(res.get_json()["amount"], 39900)

    def test_growth_always_resolves_to_79900_paise(self):
        res = self._post({"plan": "growth", "amount": 1})
        self.assertEqual(res.get_json()["amount"], 79900)

    # ---- Database ----

    def test_billing_order_record_created(self):
        self._post({"plan": "starter"})
        self.assertEqual(len(self.store.rows), 1)

    def test_owner_user_id_correct(self):
        self._post({"plan": "starter"}, uid=42)
        self.assertEqual(self.store.rows[0]["owner_user_id"], 42)

    def test_intended_plan_correct(self):
        self._post({"plan": "growth"})
        self.assertEqual(self.store.rows[0]["pos_plan"], "growth")

    def test_amount_correct(self):
        self._post({"plan": "growth"})
        self.assertEqual(self.store.rows[0]["amount_paise"], 79900)

    def test_razorpay_order_id_stored(self):
        self.rzp.order.create.return_value = {"id": "order_xyz_999"}
        self._post({"plan": "starter"})
        self.assertEqual(self.store.rows[0]["razorpay_order_id"], "order_xyz_999")

    def test_unique_razorpay_order_id_behavior(self):
        self.store.duplicate_razorpay_order_id = "order_rzp_1"
        res = self._post({"plan": "starter"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(len(self.store.rows), 0)
        self.assertTrue(self.conn.rolled_back)

    # ---- Razorpay ----

    def test_successful_razorpay_order_creation(self):
        res = self._post({"plan": "starter"})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["razorpay_order_id"], "order_rzp_1")
        self.rzp.order.create.assert_called_once()

    def test_razorpay_failure_handled_safely(self):
        self.rzp.order.create.side_effect = Exception("razorpay is down")

        res = self._post({"plan": "starter"})

        self.assertEqual(res.status_code, 502)
        # No misleading local record when Razorpay itself failed.
        self.assertEqual(len(self.store.rows), 0)

    def test_razorpay_client_unavailable_handled_safely(self):
        with patch("routes.pos_routes.get_razorpay_client", return_value=None):
            res = self._post({"plan": "starter"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(len(self.store.rows), 0)

    def test_secret_credentials_never_returned(self):
        res = self._post({"plan": "starter"})
        body = res.get_json()
        self.assertEqual(body.get("razorpay_key_id"), "rzp_test_publickey")
        serialized = str(body)
        self.assertNotIn("supersecretvalue", serialized)
        for key in body:
            self.assertNotIn("secret", key.lower())

    # ---- Entitlement invariants ----
    # (No handler exists in FakeConn for any users/pos_subscriptions query
    # -- if this route ever read or wrote either, these tests would raise
    # AssertionError from FakeConn.execute instead of passing.)

    def test_creating_an_order_does_not_touch_pos_subscriptions_or_users(self):
        res = self._post({"plan": "growth"})
        self.assertEqual(res.status_code, 201)

    def test_allowed_uniformly_regardless_of_declared_client_state(self):
        # Nothing in the request body about "current subscription" is ever
        # consulted -- there is no server-side lookup to consult it with.
        res = self._post({"plan": "starter", "current_status": "active", "current_plan": "growth"})
        self.assertEqual(res.status_code, 201)

    # ---- Isolation ----

    def test_one_owner_cannot_create_an_order_for_another_owner(self):
        self._post({"plan": "starter", "owner_user_id": 999}, uid=5)
        self.assertEqual(self.store.rows[0]["owner_user_id"], 5)

    def test_no_client_controlled_identity_is_trusted(self):
        headers = self._auth_headers(uid=3)
        res = self.client.post(
            "/api/pos/billing/orders?user_id=999",
            json={"plan": "starter"},
            headers=headers,
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.store.rows[0]["owner_user_id"], 3)


if __name__ == "__main__":
    unittest.main()
