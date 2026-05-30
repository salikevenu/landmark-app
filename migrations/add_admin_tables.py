# migrations/add_admin_tables.py
from sqlalchemy import text
from database.init_db import get_db
import logging
logger = logging.getLogger(__name__)

def add_admin_tables():
    conn = get_db()

    # Admin audit log
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id SERIAL PRIMARY KEY,
            admin_id INTEGER,
            admin_phone TEXT,
            action TEXT,
            target_type TEXT,
            target_id TEXT,
            details TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_log(admin_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_log(created_at)"))

    # Admin settings (key-value store)
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    # Insert default settings (PostgreSQL upsert with ON CONFLICT)
    defaults = [
        ('commission_rate', '10'),
        ('withdrawal_min_amount', '100'),
        ('withdrawal_max_amount', '50000'),
        ('referral_bonus_percent', '10'),
        ('recurring_commission_percent', '5'),
        ('sponsor_price', '999'),
        ('verify_price', '499')
    ]
    for key, value in defaults:
        conn.execute(text("""
            INSERT INTO admin_settings (key, value)
            VALUES (:key, :value)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "value": value})

    # Add missing columns to users table if they don't exist (dynamic ALTER TABLE)
    # Check if columns exist using information_schema
    check_sub_status = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='users' AND column_name='subscription_status'
    """)).fetchone()
    if not check_sub_status:
        conn.execute(text("ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'inactive'"))

    check_is_banned = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='users' AND column_name='is_banned'
    """)).fetchone()
    if not check_is_banned:
        conn.execute(text("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0"))

    conn.commit()
    logger.info("Admin tables and columns added successfully.")

if __name__ == "__main__":
    add_admin_tables()