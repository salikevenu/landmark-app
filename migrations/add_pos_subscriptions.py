# migrations/add_pos_subscriptions.py
from sqlalchemy import text
from database.init_db import get_db_connection
import logging
logger = logging.getLogger(__name__)

def add_pos_subscriptions():
    conn = get_db_connection()

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_subscriptions (
            id SERIAL PRIMARY KEY,
            owner_user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
            pos_plan TEXT NOT NULL CHECK (pos_plan IN ('starter', 'growth')),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended')),
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    conn.commit()
    logger.info("✅ pos_subscriptions table ready.")

if __name__ == "__main__":
    add_pos_subscriptions()
