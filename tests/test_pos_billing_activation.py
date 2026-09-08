"""POS billing activation policy (Phase G-C): pure functions in
services/pos_billing_activation.py.

add_one_calendar_month and compute_activation_target have no database
access of their own, so they're tested directly with plain dicts/dates --
exactly like services/subscription_access.py's resolve_pos_entitlement is
tested in tests/test_pos_subscriptions.py.
"""
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.pos_billing_activation import add_one_calendar_month, compute_activation_target


class AddOneCalendarMonthTests(unittest.TestCase):
    def test_september_8_to_october_8(self):
        self.assertEqual(
            add_one_calendar_month(datetime(2026, 9, 8)), datetime(2026, 10, 8)
        )

    def test_january_31_clamps_to_february_28_in_non_leap_year(self):
        self.assertEqual(
            add_one_calendar_month(datetime(2027, 1, 31)), datetime(2027, 2, 28)
        )

    def test_january_31_clamps_to_february_29_in_leap_year(self):
        self.assertEqual(
            add_one_calendar_month(datetime(2028, 1, 31)), datetime(2028, 2, 29)
        )

    def test_august_31_clamps_to_september_30(self):
        self.assertEqual(
            add_one_calendar_month(datetime(2026, 8, 31)), datetime(2026, 9, 30)
        )

    def test_december_rolls_over_to_january_next_year(self):
        self.assertEqual(
            add_one_calendar_month(datetime(2026, 12, 15)), datetime(2027, 1, 15)
        )

    def test_preserves_time_of_day(self):
        self.assertEqual(
            add_one_calendar_month(datetime(2026, 9, 8, 13, 45, 30)),
            datetime(2026, 10, 8, 13, 45, 30),
        )


class ComputeActivationTargetTests(unittest.TestCase):
    NOW = datetime(2026, 9, 8)

    def test_case_a_no_existing_subscription_creates_starter(self):
        action, target = compute_activation_target(None, "starter", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["pos_plan"], "starter")
        self.assertEqual(target["status"], "active")
        self.assertEqual(target["expires_at"], datetime(2026, 10, 8))

    def test_case_a_no_existing_subscription_creates_growth(self):
        action, target = compute_activation_target(None, "growth", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["pos_plan"], "growth")
        self.assertEqual(target["expires_at"], datetime(2026, 10, 8))

    def test_case_b_expired_starter_restarts_from_now_not_old_expiry(self):
        current = {"pos_plan": "starter", "status": "active", "expires_at": datetime(2026, 9, 1)}
        action, target = compute_activation_target(current, "starter", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["expires_at"], datetime(2026, 10, 8))

    def test_case_b_expired_growth_restarts_from_now(self):
        current = {"pos_plan": "growth", "status": "active", "expires_at": datetime(2026, 9, 1)}
        action, target = compute_activation_target(current, "growth", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["pos_plan"], "growth")
        self.assertEqual(target["expires_at"], datetime(2026, 10, 8))

    def test_case_c_starter_renewal_extends_from_existing_expiry(self):
        current = {"pos_plan": "starter", "status": "active", "expires_at": datetime(2026, 10, 1)}
        action, target = compute_activation_target(current, "starter", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["pos_plan"], "starter")
        # October 1 + 1 month = November 1, NOT September 8 + 1 month.
        self.assertEqual(target["expires_at"], datetime(2026, 11, 1))

    def test_case_c_growth_renewal_extends_from_existing_expiry(self):
        current = {"pos_plan": "growth", "status": "active", "expires_at": datetime(2026, 10, 1)}
        action, target = compute_activation_target(current, "growth", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["pos_plan"], "growth")
        self.assertEqual(target["expires_at"], datetime(2026, 11, 1))

    def test_case_d_starter_to_growth_upgrade_starts_fresh_from_now(self):
        current = {"pos_plan": "starter", "status": "active", "expires_at": datetime(2026, 10, 1)}
        action, target = compute_activation_target(current, "growth", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["pos_plan"], "growth")
        # Fresh from NOW (Sept 8), not extended from Starter's Oct 1 expiry.
        self.assertEqual(target["expires_at"], datetime(2026, 10, 8))

    def test_case_e_growth_to_starter_is_downgrade_protected(self):
        current = {"pos_plan": "growth", "status": "active", "expires_at": datetime(2026, 10, 1)}
        action, target = compute_activation_target(current, "starter", self.NOW)
        self.assertEqual(action, "skip")
        self.assertIsNone(target)

    def test_case_f_suspended_subscription_restarts_on_reactivation(self):
        current = {"pos_plan": "growth", "status": "suspended", "expires_at": datetime(2026, 12, 1)}
        action, target = compute_activation_target(current, "growth", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["pos_plan"], "growth")
        self.assertEqual(target["expires_at"], datetime(2026, 10, 8))

    def test_null_expiry_active_renewal_does_not_crash(self):
        current = {"pos_plan": "starter", "status": "active", "expires_at": None}
        action, target = compute_activation_target(current, "starter", self.NOW)
        self.assertEqual(action, "apply")
        self.assertEqual(target["expires_at"], datetime(2026, 10, 8))


if __name__ == "__main__":
    unittest.main()
