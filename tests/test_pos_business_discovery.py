"""POS business discovery (Phase C): GET /api/pos/businesses exposes
`pos_access` per business, computed server-side from a single entitlement
resolution -- never per-business, never client-influenced, and the
endpoint itself must always succeed (200) regardless of entitlement state.

Business logic for products/inventory/sales/customers themselves is
covered elsewhere; these tests only prove the `pos_access` computation
and the discovery endpoint's own "always succeeds" contract.
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

    def create(self, owner_user_id, name, created_at=None):
        row = {
            "id": self.next_id,
            "owner_user_id": owner_user_id,
            "name": name,
            "created_at": created_at or datetime(2026, 9, 4),
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def for_owner(self, owner_user_id):
        return sorted(
            (r for r in self.rows if r["owner_user_id"] == owner_user_id),
            key=lambda r: r["id"],
        )


class FakeConn:
    """`state` is a mutable {"user_row": ..., "subscription_row": ...}
    dict the test case can reassign between requests. Tracks how many
    times the users/pos_subscriptions queries ran on *this* connection
    instance -- one instance == one request, since the route calls
    get_db_connection() exactly once per request -- to prove entitlement
    is resolved once, not per business."""

    def __init__(self, businesses, state):
        self.businesses = businesses
        self.state = state
        self.users_query_count = 0
        self.subscription_query_count = 0

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if q.startswith("select plan, subscription_expiry from users"):
            self.users_query_count += 1
            user_row = self.state["user_row"]
            return FakeResult(row=FakeRow(dict(user_row)) if user_row else None)

        if q.startswith("select pos_plan, status, expires_at from pos_subscriptions"):
            self.subscription_query_count += 1
            sub_row = self.state["subscription_row"]
            return FakeResult(row=FakeRow(dict(sub_row)) if sub_row else None)

        if q.startswith("select id, name, created_at") and "from pos_businesses" in q:
            rows = self.businesses.for_owner(params.get("uid"))
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

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
BUSINESS_POWER_USER = {"plan": "business_power", "subscription_expiry": "2099-01-01"}
EXPIRED_BUSINESS_POWER_USER = {"plan": "business_power", "subscription_expiry": "2000-01-01"}


def _starter(status="active", expires_at=None):
    return {"pos_plan": "starter", "status": status, "expires_at": expires_at}


def _growth(status="active", expires_at=None):
    return {"pos_plan": "growth", "status": status, "expires_at": expires_at}


class PosBusinessDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        self.state = {"user_row": dict(NOT_BUSINESS_POWER), "subscription_row": None}
        self.last_conn = None

        def _factory():
            conn = FakeConn(self.businesses, self.state)
            self.last_conn = conn
            return conn

        patcher = patch("routes.pos_routes.get_db_connection", _factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _set_entitlement(self, user_row=None, subscription_row=None):
        self.state["user_row"] = user_row if user_row is not None else dict(NOT_BUSINESS_POWER)
        self.state["subscription_row"] = subscription_row

    def _get(self, uid=1):
        return self.client.get("/api/pos/businesses", headers=self._auth_headers(uid))

    # ---- A. Active Starter, one business ----

    def test_active_starter_one_business_has_access(self):
        self._set_entitlement(subscription_row=_starter())
        self.businesses.create(1, "Shop A")

        res = self._get()

        self.assertEqual(res.status_code, 200)
        businesses = res.get_json()["businesses"]
        self.assertEqual(businesses[0]["pos_access"], True)

    # ---- B. Active Starter, two businesses ----

    def test_active_starter_two_businesses_oldest_true_newer_false(self):
        self._set_entitlement(subscription_row=_starter())
        oldest = self.businesses.create(1, "Shop A", created_at=datetime(2026, 1, 1))
        newer = self.businesses.create(1, "Shop B", created_at=datetime(2026, 2, 1))

        res = self._get()

        by_id = {b["id"]: b["pos_access"] for b in res.get_json()["businesses"]}
        self.assertEqual(by_id[oldest["id"]], True)
        self.assertEqual(by_id[newer["id"]], False)

    # ---- C. Active Growth, three businesses ----

    def test_active_growth_three_businesses_all_true(self):
        self._set_entitlement(subscription_row=_growth())
        for i in range(3):
            self.businesses.create(1, f"Shop {i}", created_at=datetime(2026, 1, i + 1))

        res = self._get()

        businesses = res.get_json()["businesses"]
        self.assertEqual(len(businesses), 3)
        self.assertTrue(all(b["pos_access"] for b in businesses))

    # ---- D. Active Growth, four businesses ----

    def test_active_growth_four_businesses_oldest_three_true_fourth_false(self):
        self._set_entitlement(subscription_row=_growth())
        created = [
            self.businesses.create(1, f"Shop {i}", created_at=datetime(2026, 1, i + 1))
            for i in range(4)
        ]

        res = self._get()

        by_id = {b["id"]: b["pos_access"] for b in res.get_json()["businesses"]}
        self.assertTrue(all(by_id[created[i]["id"]] for i in range(3)))
        self.assertFalse(by_id[created[3]["id"]])

    # ---- E. No POS subscription ----

    def test_no_subscription_list_succeeds_all_false(self):
        self.businesses.create(1, "Shop A")
        self.businesses.create(1, "Shop B")

        res = self._get()

        self.assertEqual(res.status_code, 200)
        businesses = res.get_json()["businesses"]
        self.assertEqual(len(businesses), 2)
        self.assertTrue(all(b["pos_access"] is False for b in businesses))

    # ---- F. Expired subscription ----

    def test_expired_subscription_list_succeeds_pos_access_false(self):
        past = datetime.utcnow() - timedelta(days=1)
        self._set_entitlement(subscription_row=_starter(expires_at=past))
        self.businesses.create(1, "Shop A")

        res = self._get()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["businesses"][0]["pos_access"], False)

    # ---- G. Suspended subscription ----

    def test_suspended_subscription_list_succeeds_pos_access_false(self):
        self._set_entitlement(subscription_row=_growth(status="suspended"))
        self.businesses.create(1, "Shop A")

        res = self._get()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["businesses"][0]["pos_access"], False)

    # ---- H. NULL expires_at ----

    def test_null_expires_at_grants_access(self):
        self._set_entitlement(subscription_row=_starter(expires_at=None))
        self.businesses.create(1, "Shop A")

        res = self._get()

        self.assertEqual(res.get_json()["businesses"][0]["pos_access"], True)

    # ---- I. Business Power ----

    def test_business_power_all_businesses_true_regardless_of_count(self):
        self._set_entitlement(user_row=BUSINESS_POWER_USER, subscription_row=None)
        for i in range(5):
            self.businesses.create(1, f"Shop {i}", created_at=datetime(2026, 1, i + 1))

        res = self._get()

        businesses = res.get_json()["businesses"]
        self.assertEqual(len(businesses), 5)
        self.assertTrue(all(b["pos_access"] for b in businesses))

    # ---- J. Expired Business Power ----

    def test_expired_business_power_falls_back_no_unlimited_grant(self):
        self._set_entitlement(user_row=EXPIRED_BUSINESS_POWER_USER, subscription_row=None)
        self.businesses.create(1, "Shop A")

        res = self._get()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["businesses"][0]["pos_access"], False)

    def test_expired_business_power_falls_back_to_real_pos_subscription(self):
        self._set_entitlement(user_row=EXPIRED_BUSINESS_POWER_USER, subscription_row=_starter())
        self.businesses.create(1, "Shop A")

        res = self._get()

        self.assertEqual(res.get_json()["businesses"][0]["pos_access"], True)

    # ---- K. Other user's businesses never appear ----

    def test_other_users_businesses_never_appear(self):
        self._set_entitlement(subscription_row=_growth())
        self.businesses.create(2, "Other Owner Shop")

        res = self._get(uid=1)

        self.assertEqual(res.get_json(), {"businesses": []})

    # ---- L. Empty business list ----

    def test_empty_business_list_succeeds(self):
        self._set_entitlement(subscription_row=_growth())

        res = self._get()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"businesses": []})

    # ---- M. Deterministic ordering on created_at tie ----

    def test_tied_created_at_uses_id_as_deterministic_tiebreaker(self):
        self._set_entitlement(subscription_row=_starter())
        same_instant = datetime(2026, 1, 1, 12, 0, 0)
        first = self.businesses.create(1, "Shop A", created_at=same_instant)
        second = self.businesses.create(1, "Shop B", created_at=same_instant)

        res = self._get()

        by_id = {b["id"]: b["pos_access"] for b in res.get_json()["businesses"]}
        self.assertEqual(by_id[first["id"]], True)
        self.assertEqual(by_id[second["id"]], False)

    # ---- N. No N+1 subscription lookup ----

    def test_entitlement_resolved_once_not_per_business(self):
        self._set_entitlement(subscription_row=_growth())
        for i in range(6):
            self.businesses.create(1, f"Shop {i}", created_at=datetime(2026, 1, i + 1))

        res = self._get()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.get_json()["businesses"]), 6)
        self.assertEqual(self.last_conn.users_query_count, 1)
        self.assertEqual(self.last_conn.subscription_query_count, 1)

    def test_business_power_skips_subscription_query_entirely(self):
        self._set_entitlement(user_row=BUSINESS_POWER_USER, subscription_row=None)
        for i in range(4):
            self.businesses.create(1, f"Shop {i}")

        res = self._get()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.last_conn.subscription_query_count, 0)

    # ---- O. Existing response fields unchanged ----

    def test_existing_fields_unchanged(self):
        self._set_entitlement(subscription_row=_growth())
        self.businesses.create(1, "Shop A")

        res = self._get()

        business = res.get_json()["businesses"][0]
        self.assertIn("id", business)
        self.assertIn("name", business)
        self.assertIn("created_at", business)
        self.assertEqual(business["name"], "Shop A")

    def test_only_pos_access_added_no_other_new_fields(self):
        self._set_entitlement(subscription_row=_growth())
        self.businesses.create(1, "Shop A")

        res = self._get()

        business = res.get_json()["businesses"][0]
        self.assertEqual(set(business.keys()), {"id", "name", "created_at", "pos_access"})

    # ---- Security: client cannot influence pos_access ----

    def test_client_supplied_pos_access_in_query_is_ignored(self):
        self._set_entitlement(subscription_row=None)  # not entitled
        self.businesses.create(1, "Shop A")

        res = self.client.get(
            "/api/pos/businesses?pos_access=true", headers=self._auth_headers()
        )

        self.assertEqual(res.get_json()["businesses"][0]["pos_access"], False)


if __name__ == "__main__":
    unittest.main()
