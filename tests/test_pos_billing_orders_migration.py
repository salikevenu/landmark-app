"""POS billing orders migration (Phase G-A + G-B): schema/migration DDL shape.

Mirrors tests/test_pos_subscriptions.py's MigrationTests pattern -- patches
get_db_connection with an in-memory fake and asserts on the executed DDL
text, since there is no real database connection in this test suite.

G-B widens `status` from the G-A-only 'created' to 'created'/'paid'/
'failed' and adds razorpay_payment_id/paid_at -- this file replaces the
G-A-era assertions that pinned status to a single value (see git history
for the prior version), since that shape is now intentionally superseded.
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

from migrations.add_pos_billing_orders import add_pos_billing_orders


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
    def test_migration_creates_pos_billing_orders_with_expected_shape(self):
        fake = FakeConn()
        with patch("migrations.add_pos_billing_orders.get_db_connection", lambda: fake):
            add_pos_billing_orders()

        ddl = _norm(" ".join(fake.executed))
        self.assertIn("create table if not exists pos_billing_orders", ddl)
        self.assertIn("owner_user_id integer not null references users(id)", ddl)
        self.assertIn("pos_plan text not null check (pos_plan in ('starter', 'growth'))", ddl)
        self.assertIn("amount_paise integer not null", ddl)
        self.assertIn("currency text not null default 'inr'", ddl)
        self.assertIn("razorpay_order_id text not null unique", ddl)
        self.assertIn("razorpay_payment_id text", ddl)
        self.assertIn(
            "status text not null default 'created' check (status in ('created', 'paid', 'failed'))",
            ddl,
        )
        self.assertIn("paid_at timestamp", ddl)
        self.assertIn("activated_at timestamp", ddl)
        self.assertIn("created_at timestamp default current_timestamp", ddl)
        self.assertIn("updated_at timestamp default current_timestamp", ddl)
        self.assertTrue(fake.committed)

    def test_migration_has_no_business_id_and_no_payment_credential_columns(self):
        fake = FakeConn()
        with patch("migrations.add_pos_billing_orders.get_db_connection", lambda: fake):
            add_pos_billing_orders()

        ddl = _norm(" ".join(fake.executed))
        self.assertNotIn("business_id", ddl)
        for forbidden in ("card_number", "cvv", "upi", "bank_account", "ifsc"):
            self.assertNotIn(forbidden, ddl)

    def test_migration_status_is_exactly_created_paid_failed(self):
        """G-B's deliberately minimal state model -- no speculative states
        (refunded, activated, cancelled, expired, ...) were added."""
        fake = FakeConn()
        with patch("migrations.add_pos_billing_orders.get_db_connection", lambda: fake):
            add_pos_billing_orders()

        ddl = _norm(" ".join(fake.executed))
        self.assertIn("check (status in ('created', 'paid', 'failed'))", ddl)
        for premature_status in ("refunded", "activated", "cancelled", "expired", "captured"):
            self.assertNotIn(f"'{premature_status}'", ddl)

    def test_migration_widens_status_constraint_via_alter(self):
        """The CHECK constraint change is applied idempotently via
        DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT, not a destructive
        table rewrite -- brings an existing G-A row set up to G-B without
        data loss."""
        fake = FakeConn()
        with patch("migrations.add_pos_billing_orders.get_db_connection", lambda: fake):
            add_pos_billing_orders()

        ddl = _norm(" ".join(fake.executed))
        self.assertIn("drop constraint if exists pos_billing_orders_status_check", ddl)
        self.assertIn("add constraint pos_billing_orders_status_check", ddl)
        self.assertIn("add column if not exists razorpay_payment_id text", ddl)
        self.assertIn("add column if not exists paid_at timestamp", ddl)

    def test_migration_adds_unique_index_on_payment_id(self):
        fake = FakeConn()
        with patch("migrations.add_pos_billing_orders.get_db_connection", lambda: fake):
            add_pos_billing_orders()

        ddl = _norm(" ".join(fake.executed))
        self.assertIn(
            "create unique index if not exists uq_pos_billing_orders_payment_id "
            "on pos_billing_orders (razorpay_payment_id) where razorpay_payment_id is not null",
            ddl,
        )

    def test_migration_adds_activated_at_column(self):
        fake = FakeConn()
        with patch("migrations.add_pos_billing_orders.get_db_connection", lambda: fake):
            add_pos_billing_orders()

        ddl = _norm(" ".join(fake.executed))
        self.assertIn("add column if not exists activated_at timestamp", ddl)

    def test_migration_is_idempotent(self):
        fake = FakeConn()
        with patch("migrations.add_pos_billing_orders.get_db_connection", lambda: fake):
            add_pos_billing_orders()
            add_pos_billing_orders()

        self.assertEqual(fake.executed.count(fake.executed[0]), 2)


if __name__ == "__main__":
    unittest.main()
