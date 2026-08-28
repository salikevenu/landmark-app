"""Stage 2B additive schema for money-safe referral commissions.

Does NOT DROP/TRUNCATE/DELETE production rows.
If duplicate non-null payments.order_id values exist, this migration STOPS.
Do not run against production until preflight is approved.
"""
from sqlalchemy import text
from database.init_db import get_db_connection
import logging

logger = logging.getLogger(__name__)

PREFLIGHT_SQL = {
    "duplicate_order_ids": """
        SELECT order_id, COUNT(*) AS n, array_agg(id) AS ids
        FROM payments
        WHERE order_id IS NOT NULL
        GROUP BY order_id
        HAVING COUNT(*) > 1
    """,
    "referral_wallet_rows": """
        SELECT source, COUNT(*) AS n
        FROM wallet_transactions
        WHERE source IN ('referral_first_bonus', 'referral_recurring')
        GROUP BY source
    """,
    "first_bonus_duplicates": """
        SELECT reference_id, COUNT(*) AS n
        FROM wallet_transactions
        WHERE source = 'referral_first_bonus'
        GROUP BY reference_id
        HAVING COUNT(*) > 1
    """,
    "recurring_payment_duplicates": """
        SELECT razorpay_payment_id, source, COUNT(*) AS n
        FROM wallet_transactions
        WHERE razorpay_payment_id IS NOT NULL
          AND source IN ('referral_first_bonus', 'referral_recurring')
        GROUP BY razorpay_payment_id, source
        HAVING COUNT(*) > 1
    """,
    "wallet_balance_drift": """
        SELECT u.id,
               COALESCE(u.wallet_balance, 0) AS users_wallet_balance,
               COALESCE(wb.balance, 0) AS canonical_balance
        FROM users u
        LEFT JOIN wallet_balance wb ON wb.user_id = u.id
        WHERE COALESCE(u.wallet_balance, 0) <> 0
           OR COALESCE(wb.balance, 0) <> 0
    """,
    "commission_references": """
        SELECT id, user_id, source, reference_id, razorpay_payment_id, amount, status
        FROM wallet_transactions
        WHERE source IN ('referral_first_bonus', 'referral_recurring')
        ORDER BY id
        LIMIT 50
    """,
}


def preflight_readonly(conn):
    """Read-only inspection. Caller must not write."""
    report = {}
    report["duplicate_order_ids"] = [
        dict(r._mapping) for r in conn.execute(text(PREFLIGHT_SQL["duplicate_order_ids"])).fetchall()
    ]
    report["referral_wallet_rows"] = [
        dict(r._mapping) for r in conn.execute(text(PREFLIGHT_SQL["referral_wallet_rows"])).fetchall()
    ]
    report["first_bonus_duplicates"] = [
        dict(r._mapping) for r in conn.execute(text(PREFLIGHT_SQL["first_bonus_duplicates"])).fetchall()
    ]
    try:
        report["recurring_payment_duplicates"] = [
            dict(r._mapping) for r in conn.execute(text(PREFLIGHT_SQL["recurring_payment_duplicates"])).fetchall()
        ]
    except Exception as exc:
        report["recurring_payment_duplicates"] = f"column missing or query failed: {exc}"
    report["wallet_balance_drift"] = [
        dict(r._mapping) for r in conn.execute(text(PREFLIGHT_SQL["wallet_balance_drift"])).fetchall()
    ]
    report["commission_references"] = [
        dict(r._mapping) for r in conn.execute(text(PREFLIGHT_SQL["commission_references"])).fetchall()
    ]
    return report


def migrate_referral_commission_money_safety():
    conn = get_db_connection()
    try:
        dups = conn.execute(text(PREFLIGHT_SQL["duplicate_order_ids"])).fetchall()
        if dups:
            details = [dict(r._mapping) for r in dups]
            raise RuntimeError(
                "STOP: duplicate non-null payments.order_id exist; "
                "refusing UNIQUE index. Rows: %s" % details
            )

        conn.execute(text(
            "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS razorpay_payment_id TEXT"
        ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS referral_commission_jobs (
                id SERIAL PRIMARY KEY,
                payment_id TEXT NOT NULL,
                razorpay_payment_id TEXT,
                referred_user_id INTEGER NOT NULL REFERENCES users(id),
                amount_rupees NUMERIC(12,2) NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                CONSTRAINT uq_referral_commission_jobs_payment UNIQUE (payment_id)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_referral_commission_jobs_status "
            "ON referral_commission_jobs(status)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_referral_commission_jobs_rzp "
            "ON referral_commission_jobs(razorpay_payment_id)"
        ))

        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_first_bonus_reference
            ON wallet_transactions (source, reference_id)
            WHERE source = 'referral_first_bonus'
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_source_razorpay_payment
            ON wallet_transactions (source, razorpay_payment_id)
            WHERE razorpay_payment_id IS NOT NULL
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_order_id_not_null
            ON payments (order_id)
            WHERE order_id IS NOT NULL
        """))
        conn.commit()
        logger.info("Stage 2B referral commission money-safety schema applied")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    migrate_referral_commission_money_safety()
