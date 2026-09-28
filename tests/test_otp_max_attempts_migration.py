"""migrations/set_otp_max_attempts_3.py: SQL shape (no real database).

Same FakeConn pattern as tests/test_pos_billing_orders_migration.py.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from migrations.set_otp_max_attempts_3 import set_otp_max_attempts_3


def _norm(sql):
    return " ".join(str(sql).lower().split())


class _Result:
    rowcount = 1


class FakeConn:
    def __init__(self):
        self.executed = []
        self.committed = False
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append(_norm(getattr(sql, "text", sql)))
        return _Result()

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class SetOtpMaxAttemptsMigrationTests(unittest.TestCase):
    def test_updates_existing_row_to_3_and_seeds_if_missing(self):
        fake = FakeConn()
        with patch("migrations.set_otp_max_attempts_3.get_db_connection", lambda: fake):
            set_otp_max_attempts_3()

        update, insert = fake.executed
        self.assertIn("update admin_settings set value = '3'", update)
        self.assertIn("where key = 'otp_max_attempts'", update)
        # Keeps an admin's deliberate 1 or 2.
        self.assertIn("value not in ('1', '2', '3')", update)
        self.assertIn("insert into admin_settings (key, value) values ('otp_max_attempts', '3')", insert)
        self.assertIn("on conflict (key) do nothing", insert)
        self.assertTrue(fake.committed)
        self.assertTrue(fake.closed)

    def test_no_seed_anywhere_still_says_5(self):
        for rel in ("database/init_db.py", "migrations/add_configurable_referral_and_otp_settings.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("('otp_max_attempts', '5')", src, rel)
            self.assertNotIn('("otp_max_attempts", "5")', src, rel)


if __name__ == "__main__":
    unittest.main()
