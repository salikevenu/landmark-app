"""POS subscription information (Phase E-A): GET /api/pos/subscription.

Exercises the actual route through the Flask test client, proving the
endpoint's response shape and its use of the existing canonical
_resolve_pos_entitlement() / resolve_pos_entitlement() -- not a second
entitlement implementation. No real database connection --
pos_routes.get_db_connection is patched with an in-memory fake, matching
every other pos_* test file's pattern.
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


class FakeConn:
    """`state["user_row"]`/`state["subscription_row"]` are the default
    single-user fixture most tests use; `state["users_by_id"]`/
    `state["subscriptions_by_owner"]` (both dicts, default {}) let the
    isolation test give two different uids two different rows, proving
    the query is genuinely scoped by the JWT-derived uid, not a shared
    fixture."""

    def __init__(self, state):
        self.state = state

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}
        uid = params.get("uid")

        if q.startswith("select plan, subscription_expiry from users"):
            by_id = self.state.get("users_by_id")
            user_row = by_id[uid] if by_id and uid in by_id else self.state["user_row"]
            return FakeResult(row=FakeRow(dict(user_row)) if user_row else None)

        if q.startswith("select pos_plan, status, expires_at from pos_subscriptions"):
            by_owner = self.state.get("subscriptions_by_owner")
            sub_row = (
                by_owner[uid] if by_owner and uid in by_owner else self.state["subscription_row"]
            )
            return FakeResult(row=FakeRow(dict(sub_row)) if sub_row else None)

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


class PosSubscriptionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()
        self.state = {"user_row": dict(NOT_BUSINESS_POWER), "subscription_row": None}
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(self.state),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _set(self, user_row=None, subscription_row=None):
        self.state["user_row"] = user_row if user_row is not None else dict(NOT_BUSINESS_POWER)
        self.state["subscription_row"] = subscription_row

    def _get(self, uid=1):
        return self.client.get("/api/pos/subscription", headers=self._auth_headers(uid))

    # ---- Unauthenticated ----

    def test_unauthenticated_request_rejected(self):
        res = self.client.get("/api/pos/subscription")
        self.assertEqual(res.status_code, 401)

    # ---- Active Starter ----

    def test_active_starter(self):
        self._set(subscription_row=_starter(expires_at=datetime(2027, 1, 1)))

        res = self._get()

        self.assertEqual(res.status_code, 200)
        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], True)
        self.assertEqual(sub["source"], "pos_subscription")
        self.assertEqual(sub["plan"], "starter")
        self.assertEqual(sub["business_limit"], 1)
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["expires_at"], "2027-01-01T00:00:00")

    # ---- Active Growth ----

    def test_active_growth(self):
        self._set(subscription_row=_growth(expires_at=datetime(2027, 6, 1)))

        res = self._get()

        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], True)
        self.assertEqual(sub["source"], "pos_subscription")
        self.assertEqual(sub["plan"], "growth")
        self.assertEqual(sub["business_limit"], 3)
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["expires_at"], "2027-06-01T00:00:00")

    def test_growth_null_expires_at_remains_active(self):
        self._set(subscription_row=_growth(expires_at=None))

        res = self._get()

        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], True)
        self.assertEqual(sub["status"], "active")
        self.assertIsNone(sub["expires_at"])

    # ---- Active Business Power ----

    def test_active_business_power(self):
        self._set(user_row=BUSINESS_POWER_USER, subscription_row=None)

        res = self._get()

        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], True)
        self.assertEqual(sub["source"], "business_power")
        # Growth-level POS entitlement -- not a third plan.
        self.assertEqual(sub["plan"], "growth")
        self.assertIsNone(sub["business_limit"])  # unlimited
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["expires_at"], "2099-01-01")

    def test_business_power_requires_no_pos_subscription_row(self):
        self._set(user_row=BUSINESS_POWER_USER, subscription_row=None)

        res = self._get()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["subscription"]["has_access"], True)

    # ---- Expired POS subscription ----

    def test_expired_pos_subscription(self):
        past = datetime.utcnow() - timedelta(days=1)
        self._set(subscription_row=_starter(expires_at=past))

        res = self._get()

        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], False)
        self.assertEqual(sub["source"], "none")
        self.assertIsNone(sub["plan"])
        self.assertEqual(sub["business_limit"], 0)
        self.assertEqual(sub["status"], "inactive")
        self.assertIsNone(sub["expires_at"])

    # ---- Suspended POS subscription ----

    def test_suspended_pos_subscription(self):
        self._set(subscription_row=_growth(status="suspended"))

        res = self._get()

        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], False)
        self.assertEqual(sub["source"], "none")
        self.assertEqual(sub["business_limit"], 0)
        self.assertEqual(sub["status"], "inactive")

    # ---- No POS subscription ----

    def test_no_pos_subscription(self):
        self._set(subscription_row=None)

        res = self._get()

        self.assertEqual(res.status_code, 200)
        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], False)
        self.assertEqual(sub["source"], "none")
        self.assertIsNone(sub["plan"])
        self.assertEqual(sub["business_limit"], 0)
        self.assertEqual(sub["status"], "inactive")
        self.assertIsNone(sub["expires_at"])

    # ---- Expired Business Power falls back ----

    def test_expired_business_power_with_no_pos_subscription(self):
        self._set(user_row=EXPIRED_BUSINESS_POWER_USER, subscription_row=None)

        res = self._get()

        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], False)
        self.assertEqual(sub["source"], "none")

    def test_expired_business_power_falls_back_to_real_pos_subscription(self):
        self._set(user_row=EXPIRED_BUSINESS_POWER_USER, subscription_row=_starter())

        res = self._get()

        sub = res.get_json()["subscription"]
        self.assertEqual(sub["has_access"], True)
        self.assertEqual(sub["source"], "pos_subscription")
        self.assertEqual(sub["plan"], "starter")

    # ---- business_limit correctness ----

    def test_starter_business_limit_is_one(self):
        self._set(subscription_row=_starter())
        self.assertEqual(self._get().get_json()["subscription"]["business_limit"], 1)

    def test_growth_business_limit_is_three(self):
        self._set(subscription_row=_growth())
        self.assertEqual(self._get().get_json()["subscription"]["business_limit"], 3)

    def test_business_power_business_limit_is_null_not_zero(self):
        self._set(user_row=BUSINESS_POWER_USER, subscription_row=None)
        self.assertIsNone(self._get().get_json()["subscription"]["business_limit"])

    def test_no_access_business_limit_is_zero_not_null(self):
        self._set(subscription_row=None)
        self.assertEqual(self._get().get_json()["subscription"]["business_limit"], 0)

    # ---- source correctness ----

    def test_source_values_are_exactly_the_three_documented(self):
        self._set(subscription_row=_starter())
        self.assertEqual(self._get().get_json()["subscription"]["source"], "pos_subscription")

        self._set(user_row=BUSINESS_POWER_USER, subscription_row=None)
        self.assertEqual(self._get().get_json()["subscription"]["source"], "business_power")

        self._set(subscription_row=None)
        self.assertEqual(self._get().get_json()["subscription"]["source"], "none")

    # ---- Authenticated user isolation ----

    def test_response_reflects_only_the_authenticated_users_own_state(self):
        # uid=1 is an active Growth POS subscriber; uid=2 has no POS
        # subscription at all. Each request must be scoped by the uid the
        # route derives from *that request's own* JWT, never a shared or
        # leaked value.
        self.state["users_by_id"] = {1: dict(NOT_BUSINESS_POWER), 2: dict(NOT_BUSINESS_POWER)}
        self.state["subscriptions_by_owner"] = {1: _growth(), 2: None}

        sub1 = self._get(uid=1).get_json()["subscription"]
        sub2 = self._get(uid=2).get_json()["subscription"]

        self.assertEqual(sub1["has_access"], True)
        self.assertEqual(sub1["plan"], "growth")
        self.assertEqual(sub2["has_access"], False)
        self.assertIsNone(sub2["plan"])

    def test_client_supplied_user_id_is_ignored(self):
        self._set(subscription_row=_starter())

        res = self.client.get(
            "/api/pos/subscription?user_id=999",
            headers=self._auth_headers(uid=1),
        )

        # No error, no different behavior -- the query string is never
        # consulted for identity.
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["subscription"]["plan"], "starter")

    def test_client_cannot_influence_plan_or_status_via_request_body(self):
        self._set(subscription_row=_starter())

        res = self.client.get(
            "/api/pos/subscription",
            json={"plan": "growth", "status": "active", "business_limit": 999},
            headers=self._auth_headers(),
        )

        sub = res.get_json()["subscription"]
        self.assertEqual(sub["plan"], "starter")
        self.assertEqual(sub["business_limit"], 1)


if __name__ == "__main__":
    unittest.main()
