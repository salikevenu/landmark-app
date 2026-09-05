# migrations/add_pos_sales.py
from sqlalchemy import text
from database.init_db import get_db_connection
import logging
logger = logging.getLogger(__name__)

def add_pos_sales():
    conn = get_db_connection()

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_sales (
            id SERIAL PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES pos_businesses(id),
            total_amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_sales_business ON pos_sales(business_id)"))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_sale_items (
            id SERIAL PRIMARY KEY,
            sale_id INTEGER NOT NULL REFERENCES pos_sales(id),
            product_id INTEGER NOT NULL REFERENCES pos_products(id),
            product_name TEXT NOT NULL,
            unit_price INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            line_total INTEGER NOT NULL
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_sale_items_sale ON pos_sale_items(sale_id)"))

    conn.commit()
    logger.info("✅ pos_sales/pos_sale_items tables ready.")

if __name__ == "__main__":
    add_pos_sales()
