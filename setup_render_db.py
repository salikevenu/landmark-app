import os
from sqlalchemy import create_engine, text
import logging
logger = logging.getLogger(__name__)

# Use the External Database URL from Render
DATABASE_URL = "postgresql://..."  # Paste your External URL here

engine = create_engine(DATABASE_URL, echo=True)

def run_migrations():
    with engine.connect() as conn:
        # =====================================================
        # USERS TABLE
        # =====================================================
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                phone TEXT UNIQUE,
                name TEXT,
                role TEXT DEFAULT 'user',
                plan TEXT DEFAULT 'free',
                business_limit INTEGER DEFAULT 0,
                extra_businesses_purchased INTEGER DEFAULT 0,
                subscription_expiry TEXT,
                device_id TEXT,
                ip_address TEXT,
                referral_rewarded INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER REFERENCES users(id),
                first_sub_commission_paid INTEGER DEFAULT 0,
                wallet_balance REAL DEFAULT 0,
                latitude REAL,
                longitude REAL,
                lat_grid INTEGER,
                lng_grid INTEGER,
                is_active INTEGER DEFAULT 1,
                is_blocked INTEGER DEFAULT 0,
                language TEXT DEFAULT 'en',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # ... add ALL other CREATE TABLE statements from your init_db.py here ...
        # (Copy everything from your database/init_db.py inside this function)

        # Insert admin user
        conn.execute(text("""
            INSERT INTO users (phone, role, plan, is_active, referral_code)
            VALUES ('9959543954', 'admin', 'free', 1, 'ADMIN9959')
            ON CONFLICT (phone) DO UPDATE SET role = 'admin'
        """))

        conn.commit()
        logger.info("✅ Database initialized and admin user created.")

if __name__ == "__main__":
    run_migrations()