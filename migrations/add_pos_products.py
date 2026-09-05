# migrations/add_pos_products.py
from sqlalchemy import text
from database.init_db import get_db_connection
import logging
logger = logging.getLogger(__name__)

def add_pos_products():
    conn = get_db_connection()

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_products (
            id SERIAL PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES pos_businesses(id),
            name TEXT NOT NULL,
            price INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_products_business ON pos_products(business_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_products_business_active ON pos_products(business_id, is_active)"))

    conn.commit()
    logger.info("✅ pos_products table ready.")

if __name__ == "__main__":
    add_pos_products()
