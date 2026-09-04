# migrations/add_pos_businesses.py
from sqlalchemy import text
from database.init_db import get_db_connection
import logging
logger = logging.getLogger(__name__)

def add_pos_businesses():
    conn = get_db_connection()

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_businesses (
            id SERIAL PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_businesses_owner ON pos_businesses(owner_user_id)"))

    conn.commit()
    logger.info("✅ pos_businesses table ready.")

if __name__ == "__main__":
    add_pos_businesses()
