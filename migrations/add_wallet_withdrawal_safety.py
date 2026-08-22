"""Additive withdrawal uniqueness. Do NOT apply to production from this agent.

Creates unique indexes so:
- one debit ledger row per withdraw:{id}
- one refund ledger row per withdraw-refund:{id}
- one withdraw_requests.reference_id per user (idempotency key)
"""
from sqlalchemy import text

from database.init_db import get_db_connection


def migrate_wallet_withdrawal_safety():
    conn = get_db_connection()
    try:
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_withdraw_requests_user_reference
            ON withdraw_requests (user_id, reference_id)
            WHERE reference_id IS NOT NULL
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_withdraw_debit_ref
            ON wallet_transactions (reference_id)
            WHERE type = 'debit' AND source = 'withdraw_request' AND reference_id IS NOT NULL
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_withdraw_refund_ref
            ON wallet_transactions (reference_id)
            WHERE type = 'credit' AND source = 'withdraw_refund' AND reference_id IS NOT NULL
        """))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit("Do not apply this migration to production from this script.")
