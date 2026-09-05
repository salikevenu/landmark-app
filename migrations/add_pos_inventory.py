# migrations/add_pos_inventory.py
from sqlalchemy import text
from database.init_db import get_db_connection
import logging
logger = logging.getLogger(__name__)

def add_pos_inventory():
    conn = get_db_connection()

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_inventory (
            product_id INTEGER PRIMARY KEY REFERENCES pos_products(id),
            business_id INTEGER NOT NULL REFERENCES pos_businesses(id),
            quantity INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_inventory_business ON pos_inventory(business_id)"))

    conn.commit()
    logger.info("✅ pos_inventory table ready.")

if __name__ == "__main__":
    add_pos_inventory()
