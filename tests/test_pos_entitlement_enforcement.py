"""POS entitlement enforcement (Phase B): the backend is authoritative for
POS access -- every business-scoped route enforces active POS entitlement
in addition to the existing ownership check, and POST /businesses enforces
the caller's plan business-count limit before INSERT.

Exercises the actual routes through the Flask test client (not just the
pure resolve_pos_entitlement() function from Phase A, already covered by
tests/test_pos_subscriptions.py) -- proving the gate is genuinely wired
into every route, not merely implemented as an unused helper. Business
logic correctness for products/inventory/sales/customers themselves is
already covered by their own test files and is deliberately not
re-tested here; these fakes return the minimum data needed to prove
200-vs-403, nothing more.
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
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class PosBusinessStore:
    def __init__(self):
        self.rows = []
        self.next_id = 1

    def create(self, owner_user_id, name):
        row = {
            "id": self.next_id,
            "owner_user_id": owner_user_id,
            "name": name,
            "created_at": datetime(2026, 9, 4) + timedelta(seconds=self.next_id),
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def owned_by(self, business_id, owner_user_id):
        return next(
            (r for r in self.rows if r["id"] == business_id and r["owner_user_id"] == owner_user_id),
            None,
        )

    def for_owner(self, owner_user_id):
        return [r for r in self.rows if r["owner_user_id"] == owner_user_id]

    def count_for_owner(self, owner_user_id):
        return len(self.for_owner(owner_user_id))


class FakeConn:
    """`state` is a mutable {"user_row": ..., "subscription_row": ...}
    dict shared with (and freely reassignable by) the test case -- each
    request constructs a fresh FakeConn (matching real per-request
    connect() behavior) but all of them read the *current* state at query
    time, so a test can change entitlement between requests without
    re-patching. Every other business-scoped query returns the minimum
    shape needed to prove 200-vs-403 (empty lists), since those
    endpoints' own content correctness is covered elsewhere."""

    def __init__(self, businesses, state):
        self.businesses = businesses
        self.state = state

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if q.startswith("select plan, subscription_expiry from users"):
            user_row = self.state["user_row"]
            return FakeResult(row=FakeRow(dict(user_row)) if user_row else None)

        if q.startswith("select pos_plan, status, expires_at from pos_subscriptions"):
            sub_row = self.state["subscription_row"]
            return FakeResult(row=FakeRow(dict(sub_row)) if sub_row else None)

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("select count(*) as c from pos_businesses"):
            return FakeResult(row=FakeRow({"c": self.businesses.count_for_owner(params.get("uid"))}))

        if q.startswith("select id, name, created_at") and "from pos_businesses" in q:
            rows = self.businesses.for_owner(params.get("uid"))
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

        if q.startswith("insert into pos_businesses"):
            row = self.businesses.create(params["uid"], params["name"])
            return FakeResult(row=FakeRow(dict(row)))

        # Past the entitlement gate: business-scoped reads return empty,
        # writes are never reached by these tests (entitlement denial
        # always fires before body parsing).
        if "from pos_products" in q:
            return FakeResult(rows=[])
        if q.startswith("select id, total_amount, created_at from pos_sales"):
            return FakeResult(rows=[])
        if q.startswith("select id, name, phone, created_at from pos_customers"):
            return FakeResult(rows=[])

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        return None

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


NOT_BUSINESS_POWER = {"plan": "free", "subscription_expiry": None}


def _starter(status="active", expires_at=None):
    return {"pos_plan": "starter", "status": status, "expires_at": expires_at}


def _growth(status="active", expires_at=None):
    return {"pos_plan": "growth", "status": status, "expires_at": expires_at}


BUSINESS_POWER_USER = {"plan": "business_power", "subscription_expiry": "2099-01-01"}


class PosEntitlementEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        self.state = {"user_row": dict(NOT_BUSINESS_POWER), "subscription_row": None}
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(self.businesses, self.state),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _set_entitlement(self, user_row=None, subscription_row=None):
        self.state["user_row"] = user_row if user_row is not None else dict(NOT_BUSINESS_POWER)
        self.state["subscription_row"] = subscription_row

    # ---- A. No POS subscription ----

    def test_no_subscription_get_products_denied(self):
        business = self.businesses.create(1, "Shop A")
        res = self.client.get(f"/api/pos/businesses/{business['id']}/products", headers=self._auth_headers())
        self.assertEqual(res.status_code, 403)

    def test_no_subscription_post_sales_denied(self):
        business = self.businesses.create(1, "Shop A")
        res = self.client.post(
            f"/api/pos/businesses/{business['id']}/sales",
            json={"items": []},
            headers=self._auth_headers(),
        )
        self.assertEqual(res.status_code, 403)

    def test_no_subscription_post_customers_denied(self):
        business = self.businesses.create(1, "Shop A")
        res = self.client.post(
            f"/api/pos/businesses/{business['id']}/customers",
            json={"name": "Alice", "phone": "9876543210"},
            headers=self._auth_headers(),
        )
        self.assertEqual(res.status_code, 403)

    def test_no_subscription_business_creation_denied(self):
        res = self.client.post(
            "/api/pos/businesses", json={"name": "New Shop"}, headers=self._auth_headers()
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.businesses.count_for_owner(1), 0)

    # ---- B. Active Starter ----

    def test_starter_existing_business_accessible(self):
        self._set_entitlement(subscription_row=_starter())
        business = self.businesses.create(1, "Shop A")

        res = self.client.get(f"/api/pos/businesses/{business['id']}/products", headers=self._auth_headers())

        self.assertEqual(res.status_code, 200)

    def test_starter_first_business_can_be_created(self):
        self._set_entitlement(subscription_row=_starter())

        res = self.client.post("/api/pos/businesses", json={"name": "Shop A"}, headers=self._auth_headers())

        self.assertEqual(res.status_code, 201)

    def test_starter_second_business_rejected(self):
        self._set_entitlement(subscription_row=_starter())
        self.businesses.create(1, "Shop A")

        res = self.client.post("/api/pos/businesses", json={"name": "Shop B"}, headers=self._auth_headers())

        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.businesses.count_for_owner(1), 1)

    # ---- C. Active Growth ----

    def test_growth_existing_businesses_accessible(self):
        self._set_entitlement(subscription_row=_growth())
        b1 = self.businesses.create(1, "Shop A")
        b2 = self.businesses.create(1, "Shop B")

        res1 = self.client.get(f"/api/pos/businesses/{b1['id']}/products", headers=self._auth_headers())
        res2 = self.client.get(f"/api/pos/businesses/{b2['id']}/products", headers=self._auth_headers())

        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)

    def test_growth_creation_allowed_through_third_business(self):
        self._set_entitlement(subscription_row=_growth())
        self.businesses.create(1, "Shop A")
        self.businesses.create(1, "Shop B")

        res = self.client.post("/api/pos/businesses", json={"name": "Shop C"}, headers=self._auth_headers())

        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.businesses.count_for_owner(1), 3)

    def test_growth_fourth_business_rejected(self):
        self._set_entitlement(subscription_row=_growth())
        self.businesses.create(1, "Shop A")
        self.businesses.create(1, "Shop B")
        self.businesses.create(1, "Shop C")

        res = self.client.post("/api/pos/businesses", json={"name": "Shop D"}, headers=self._auth_headers())

        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.businesses.count_for_owner(1), 3)

    # ---- D. Expired subscription ----

    def test_expired_subscription_business_access_denied(self):
        past = datetime.utcnow() - timedelta(days=1)
        self._set_entitlement(subscription_row=_starter(expires_at=past))
        business = self.businesses.create(1, "Shop A")

        res = self.client.get(f"/api/pos/businesses/{business['id']}/products", headers=self._auth_headers())

        self.assertEqual(res.status_code, 403)

    def test_expired_subscription_creation_denied(self):
        past = datetime.utcnow() - timedelta(days=1)
        self._set_entitlement(subscription_row=_starter(expires_at=past))

        res = self.client.post("/api/pos/businesses", json={"name": "Shop A"}, headers=self._auth_headers())

        self.assertEqual(res.status_code, 403)

    # ---- E. Suspended subscription ----

    def test_suspended_subscription_business_access_denied(self):
        self._set_entitlement(subscription_row=_growth(status="suspended"))
        business = self.businesses.create(1, "Shop A")

        res = self.client.get(f"/api/pos/businesses/{business['id']}/products", headers=self._auth_headers())

        self.assertEqual(res.status_code, 403)

    def test_suspended_subscription_creation_denied(self):
        self._set_entitlement(subscription_row=_growth(status="suspended"))

        res = self.client.post("/api/pos/businesses", json={"name": "Shop A"}, headers=self._auth_headers())

        self.assertEqual(res.status_code, 403)

    # ---- F. NULL expires_at ----

    def test_null_expires_at_remains_usable(self):
        self._set_entitlement(subscription_row=_starter(expires_at=None))
        business = self.businesses.create(1, "Shop A")

        res = self.client.get(f"/api/pos/businesses/{business['id']}/products", headers=self._auth_headers())

        self.assertEqual(res.status_code, 200)

    # ---- G. Business Power ----

    def test_business_power_business_access_allowed(self):
        self._set_entitlement(user_row=BUSINESS_POWER_USER, subscription_row=None)
        business = self.businesses.create(1, "Shop A")

        res = self.client.get(f"/api/pos/businesses/{business['id']}/products", headers=self._auth_headers())

        self.assertEqual(res.status_code, 200)

    def test_business_power_more_than_three_businesses_can_be_created(self):
        self._set_entitlement(user_row=BUSINESS_POWER_USER, subscription_row=None)
        for name in ("Shop A", "Shop B", "Shop C", "Shop D"):
            res = self.client.post("/api/pos/businesses", json={"name": name}, headers=self._auth_headers())
            self.assertEqual(res.status_code, 201)

        self.assertEqual(self.businesses.count_for_owner(1), 4)

    def test_business_power_requires_no_pos_subscription_row(self):
        self._set_entitlement(user_row=BUSINESS_POWER_USER, subscription_row=None)

        res = self.client.post("/api/pos/businesses", json={"name": "Shop A"}, headers=self._auth_headers())

        self.assertEqual(res.status_code, 201)

    # ---- H. Wrong owner ----

    def test_wrong_owner_still_returns_404_not_403(self):
        self._set_entitlement(subscription_row=_growth())
        business = self.businesses.create(1, "Shop A")

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/products", headers=self._auth_headers(uid=2)
        )

        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json(), {"success": False, "error": "Business not found"})

    def test_wrong_owner_does_not_leak_business_existence(self):
        self._set_entitlement(subscription_row=_growth())
        business = self.businesses.create(1, "Shop A")

        res_owned = self.client.get(
            f"/api/pos/businesses/{business['id']}/products", headers=self._auth_headers(uid=2)
        )
        res_nonexistent = self.client.get(
            "/api/pos/businesses/999999/products", headers=self._auth_headers(uid=2)
        )

        self.assertEqual(res_owned.get_json(), res_nonexistent.get_json())
        self.assertEqual(res_owned.status_code, res_nonexistent.status_code)

    # ---- I. Boundary: count scoped to owner ----

    def test_business_count_is_scoped_to_owner_not_global(self):
        # Another user (uid=2) already has 3 businesses -- must not affect
        # uid=1's own Starter (limit 1) creation.
        self.businesses.create(2, "Other Owner Shop A")
        self.businesses.create(2, "Other Owner Shop B")
        self.businesses.create(2, "Other Owner Shop C")
        self._set_entitlement(subscription_row=_starter())

        res = self.client.post("/api/pos/businesses", json={"name": "My Shop"}, headers=self._auth_headers(uid=1))

        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.businesses.count_for_owner(1), 1)

    # ---- J. Atomic creation ----

    def test_failed_limit_check_creates_no_row(self):
        self._set_entitlement(subscription_row=_starter())
        self.businesses.create(1, "Shop A")
        before = self.businesses.count_for_owner(1)

        res = self.client.post("/api/pos/businesses", json={"name": "Shop B"}, headers=self._auth_headers())

        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.businesses.count_for_owner(1), before)

    # ---- K. Client manipulation ----

    def test_client_cannot_override_pos_plan_or_status_via_business_creation(self):
        self._set_entitlement(subscription_row=_starter())

        res = self.client.post(
            "/api/pos/businesses",
            json={"name": "Shop A", "pos_plan": "growth", "status": "active", "business_limit": 999},
            headers=self._auth_headers(),
        )
        self.assertEqual(res.status_code, 201)

        # A second business must still be rejected -- the client-supplied
        # pos_plan/business_limit fields above were silently ignored, the
        # real Starter (limit 1) subscription is what was enforced.
        res2 = self.client.post("/api/pos/businesses", json={"name": "Shop B"}, headers=self._auth_headers())
        self.assertEqual(res2.status_code, 403)

    def test_client_cannot_select_another_owner_via_business_id(self):
        self._set_entitlement(subscription_row=_growth())
        other_users_business = self.businesses.create(2, "Other Owner Shop")

        res = self.client.get(
            f"/api/pos/businesses/{other_users_business['id']}/products", headers=self._auth_headers(uid=1)
        )

        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
