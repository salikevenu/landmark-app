"""Add phone-keyed pending_referrals for durable OTP attribution."""
from sqlalchemy import text
from database.init_db import get_db_connection
import logging

logger = logging.getLogger(__name__)


def migrate_add_pending_referrals():
    conn = get_db_connection()
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pending_referrals (
            phone TEXT PRIMARY KEY,
            ref_code TEXT NOT NULL,
            referrer_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_pending_referrals_referrer ON pending_referrals(referrer_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_pending_referrals_expires ON pending_referrals(expires_at)"
    ))
    conn.commit()
    logger.info("pending_referrals table ensured")


if __name__ == "__main__":
    migrate_add_pending_referrals()
