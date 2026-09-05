# migrations/add_pos_customers.py
from sqlalchemy import text
from database.init_db import get_db_connection
import logging
logger = logging.getLogger(__name__)

def add_pos_customers():
    conn = get_db_connection()

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_customers (
            id SERIAL PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES pos_businesses(id),
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_customers_business ON pos_customers(business_id)"))
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pos_customers_business_phone
        ON pos_customers (business_id, phone)
    """))

    conn.commit()
    logger.info("✅ pos_customers table ready.")

if __name__ == "__main__":
    add_pos_customers()
