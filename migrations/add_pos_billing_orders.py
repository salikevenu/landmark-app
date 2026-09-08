# migrations/add_pos_billing_orders.py
from sqlalchemy import text
from database.init_db import get_db_connection
import logging
logger = logging.getLogger(__name__)


def add_pos_billing_orders():
    """Phase G-A creates the table; Phase G-B widens `status` to
    'paid'/'failed' and adds the verified-payment columns; Phase G-C
    (idempotent, additive) adds the durable activation marker. Safe to run
    against a fresh database (the CREATE TABLE below already has the full
    current shape) or an existing G-A/G-B database (the trailing
    ALTER/index statements bring it up to date) -- mirrors how `payments`
    evolved in database/init_db.py.
    """
    conn = get_db_connection()

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_billing_orders (
            id SERIAL PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id),
            pos_plan TEXT NOT NULL CHECK (pos_plan IN ('starter', 'growth')),
            amount_paise INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR',
            razorpay_order_id TEXT NOT NULL UNIQUE,
            razorpay_payment_id TEXT,
            status TEXT NOT NULL DEFAULT 'created'
                CHECK (status IN ('created', 'paid', 'failed')),
            paid_at TIMESTAMP,
            activated_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_pos_billing_orders_owner "
        "ON pos_billing_orders(owner_user_id)"
    ))

    # --- Phase G-B additive upgrade (no-op if the CREATE TABLE above just
    # created the final shape; brings an existing G-A table up to date
    # otherwise) ---
    conn.execute(text(
        "ALTER TABLE pos_billing_orders ADD COLUMN IF NOT EXISTS razorpay_payment_id TEXT"
    ))
    conn.execute(text(
        "ALTER TABLE pos_billing_orders ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP"
    ))
    # Postgres auto-names a single-column inline CHECK "<table>_<column>_check";
    # DROP+ADD under that same explicit name is idempotent across repeated runs.
    conn.execute(text(
        "ALTER TABLE pos_billing_orders DROP CONSTRAINT IF EXISTS pos_billing_orders_status_check"
    ))
    conn.execute(text("""
        ALTER TABLE pos_billing_orders ADD CONSTRAINT pos_billing_orders_status_check
            CHECK (status IN ('created', 'paid', 'failed'))
    """))
    # Enforces invariant J.6 ("one payment must not be attached to multiple
    # local billing orders") at the database level, not just in application
    # code. Partial (WHERE ... IS NOT NULL) since most rows never reach
    # 'paid' and razorpay_payment_id stays NULL for them.
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pos_billing_orders_payment_id
        ON pos_billing_orders (razorpay_payment_id)
        WHERE razorpay_payment_id IS NOT NULL
    """))

    # --- Phase G-C additive upgrade ---
    # The durable "has this paid order already been applied to
    # pos_subscriptions" marker. NULL = not yet activated. Combined with
    # locking the billing-order row (FOR UPDATE) before checking/setting
    # it, this is what makes activation safe to retry/duplicate/race --
    # see services/pos_billing_activation.py.
    conn.execute(text(
        "ALTER TABLE pos_billing_orders ADD COLUMN IF NOT EXISTS activated_at TIMESTAMP"
    ))

    conn.commit()
    logger.info(
        "✅ pos_billing_orders table ready "
        "(G-B: paid/failed + verified-payment columns; G-C: activated_at)."
    )


if __name__ == "__main__":
    add_pos_billing_orders()
