import sqlite3
import os
from database.init_db import DB_PATH

def add_admin_tables():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Admin audit log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            admin_phone TEXT,
            action TEXT,
            target_type TEXT,
            target_id TEXT,
            details TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_log(admin_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_log(created_at)")

    # Admin settings (key-value store)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Insert default settings
    defaults = [
        ('commission_rate', '10'),
        ('withdrawal_min_amount', '100'),
        ('withdrawal_max_amount', '50000'),
        ('referral_bonus_percent', '10'),
        ('recurring_commission_percent', '5'),
        ('sponsor_price', '999'),
        ('verify_price', '499')
    ]
    cursor.executemany("INSERT OR IGNORE INTO admin_settings (key, value) VALUES (?, ?)", defaults)

    # Add missing column to users if not exists
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'subscription_status' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'inactive'")
    if 'is_banned' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")

    conn.commit()
    conn.close()
    print("Admin tables and columns added successfully.")

if __name__ == "__main__":
    add_admin_tables()