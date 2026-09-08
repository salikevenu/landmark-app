"""POS subscriptions foundation (Phase A): schema/migration DDL and the
pure entitlement-resolution helpers in services/subscription_access.py.

Phase A adds no routes yet, so there is nothing to exercise through a
Flask test client. These tests instead:
  - patch get_db_connection with an in-memory fake (matching every other
    pos_* test file's pattern) to verify the migration's DDL shape, and
  - call resolve_pos_entitlement()/is_pos_subscription_active() directly
    with plain dicts, since both are pure functions with no DB access of
    their own -- exactly like is_subscription_active() and
    is_active_business_power_owner() are already tested/used elsewhere.

"Invalid plan/status rejected" is proven at the DDL level (the CHECK
constraints are the actual enforcement mechanism in Phase A -- there is
no application-level validation to unit-test yet, since no route accepts
these values from a client).
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

from migrations.add_pos_subscriptions import add_pos_subscriptions
from services.subscription_access import (
    POS_PLANS,
    is_pos_subscription_active,
    resolve_pos_entitlement,
)


def _norm(sql):
    return " ".join(str(sql).lower().split())


class FakeConn:
    def __init__(self):
        self.executed = []
        self.committed = False

    def execute(self, sql, params=None):
        self.executed.append(str(getattr(sql, "text", sql)))
        return None

    def commit(self):
        self.committed = True

    def close(self):
        return None


class MigrationTests(unittest.TestCase):
    def test_migration_creates_pos_subscriptions_with_expected_shape(self):
        fake = FakeConn()
        with patch("migrations.add_pos_subscriptions.get_db_connection", lambda: fake):
            add_pos_subscriptions()

        ddl = _norm(" ".join(fake.executed))
        self.assertIn("create table if not exists pos_subscriptions", ddl)
        self.assertIn("owner_user_id integer not null unique references users(id)", ddl)
        self.assertIn("pos_plan text not null check (pos_plan in ('starter', 'growth'))", ddl)
        self.assertIn(
            "status text not null default 'active' check (status in ('active', 'suspended'))",
            ddl,
        )
        self.assertIn("expires_at timestamp", ddl)
        self.assertTrue(fake.committed)

    def test_migration_defines_exactly_two_plans_and_no_expired_status(self):
        fake = FakeConn()
        with patch("migrations.add_pos_subscriptions.get_db_connection", lambda: fake):
            add_pos_subscriptions()

        ddl = _norm(" ".join(fake.executed))
        self.assertNotIn("'expired'", ddl)
        self.assertNotIn("business_limit", ddl)

    def test_migration_is_idempotent(self):
        """CREATE TABLE/constraints use IF NOT EXISTS-safe syntax -- running
        it twice must not raise (the fake doesn't enforce SQL semantics,
        so this only proves the Python side re-runs cleanly, matching
        every other add_pos_*.py migration's own idempotence claim)."""
        fake = FakeConn()
        with patch("migrations.add_pos_subscriptions.get_db_connection", lambda: fake):
            add_pos_subscriptions()
            add_pos_subscriptions()

        self.assertEqual(fake.executed.count(fake.executed[0]), 2)


class PosPlansTests(unittest.TestCase):
    def test_exactly_two_plans_defined(self):
        self.assertEqual(set(POS_PLANS.keys()), {"starter", "growth"})

    def test_starter_business_limit_is_one(self):
        self.assertEqual(POS_PLANS["starter"]["business_limit"], 1)

    def test_growth_business_limit_is_three(self):
        self.assertEqual(POS_PLANS["growth"]["business_limit"], 3)


class IsPosSubscriptionActiveTests(unittest.TestCase):
    def test_none_row_is_inactive(self):
        self.assertFalse(is_pos_subscription_active(None))

    def test_active_status_with_null_expiry_is_active(self):
        self.assertTrue(is_pos_subscription_active({"status": "active", "expires_at": None}))

    def test_active_status_with_future_expiry_is_active(self):
        future = datetime.utcnow() + timedelta(days=10)
        self.assertTrue(is_pos_subscription_active({"status": "active", "expires_at": future}))

    def test_active_status_with_past_expiry_is_inactive(self):
        past = datetime.utcnow() - timedelta(days=1)
        self.assertFalse(is_pos_subscription_active({"status": "active", "expires_at": past}))

    def test_suspended_status_is_inactive_regardless_of_expiry(self):
        future = datetime.utcnow() + timedelta(days=10)
        self.assertFalse(is_pos_subscription_active({"status": "suspended", "expires_at": future}))


class ResolvePosEntitlementTests(unittest.TestCase):
    def test_valid_starter_subscription_grants_access(self):
        result = resolve_pos_entitlement(
            {"plan": "free"},
            {"pos_plan": "starter", "status": "active", "expires_at": None},
        )
        self.assertEqual(
            result, {"has_access": True, "plan": "starter", "business_limit": 1}
        )

    def test_valid_growth_subscription_grants_access(self):
        result = resolve_pos_entitlement(
            {"plan": "free"},
            {"pos_plan": "growth", "status": "active", "expires_at": None},
        )
        self.assertEqual(
            result, {"has_access": True, "plan": "growth", "business_limit": 3}
        )

    def test_suspended_subscription_denies_access(self):
        result = resolve_pos_entitlement(
            {"plan": "free"},
            {"pos_plan": "growth", "status": "suspended", "expires_at": None},
        )
        self.assertEqual(result["has_access"], False)

    def test_expired_subscription_denies_access(self):
        past = datetime.utcnow() - timedelta(days=1)
        result = resolve_pos_entitlement(
            {"plan": "free"},
            {"pos_plan": "starter", "status": "active", "expires_at": past},
        )
        self.assertEqual(result["has_access"], False)

    def test_non_expired_subscription_grants_access(self):
        future = datetime.utcnow() + timedelta(days=30)
        result = resolve_pos_entitlement(
            {"plan": "free"},
            {"pos_plan": "starter", "status": "active", "expires_at": future},
        )
        self.assertEqual(result["has_access"], True)

    def test_null_expires_at_remains_valid(self):
        result = resolve_pos_entitlement(
            {"plan": "free"},
            {"pos_plan": "growth", "status": "active", "expires_at": None},
        )
        self.assertEqual(result["has_access"], True)

    def test_no_pos_subscription_denies_access(self):
        result = resolve_pos_entitlement({"plan": "free"}, None)
        self.assertEqual(
            result, {"has_access": False, "plan": None, "business_limit": None}
        )

    def test_business_power_owner_grants_pos_access_with_unlimited_businesses(self):
        # Real business_power plan + real future expiry, going through the
        # actual (unmodified) is_active_business_power_owner() -- not a
        # mock -- to prove genuine reuse, not a plan-string shortcut.
        future_expiry = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
        business_power_user = {"plan": "business_power", "subscription_expiry": future_expiry}

        result = resolve_pos_entitlement(business_power_user, None)

        self.assertEqual(
            result, {"has_access": True, "plan": "growth", "business_limit": None}
        )

    def test_business_power_owner_with_expired_marketplace_subscription_falls_through(self):
        # Proves the Business Power path genuinely reuses
        # is_active_business_power_owner()'s own expiry logic rather than
        # a shortcut that only checks the plan string -- an expired
        # Business Power owner must fall through to pos_subscriptions,
        # exactly like a non-Business-Power user would.
        past_expiry = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        lapsed_business_power_user = {
            "plan": "business_power",
            "subscription_expiry": past_expiry,
        }

        result = resolve_pos_entitlement(lapsed_business_power_user, None)

        self.assertEqual(result["has_access"], False)

    def test_non_business_power_user_uses_their_own_pos_subscription(self):
        regular_user = {"plan": "business_premium", "subscription_expiry": "2099-01-01"}

        result = resolve_pos_entitlement(
            regular_user,
            {"pos_plan": "starter", "status": "active", "expires_at": None},
        )

        # A marketplace Business Premium subscriber with no Business Power
        # grant is entitled strictly through their own POS subscription.
        self.assertEqual(
            result, {"has_access": True, "plan": "starter", "business_limit": 1}
        )


if __name__ == "__main__":
    unittest.main()
