import sys
import os
# Add the project root (one level up) to the Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.init_db import get_db
from sqlalchemy import text

def create_audit_table():
    conn = get_db()
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
    conn.commit()
    print("✅ admin_audit_log table ready.")

if __name__ == "__main__":
    create_audit_table()