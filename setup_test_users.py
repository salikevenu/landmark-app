# seed_test_users.py
import os
from sqlalchemy import create_engine, text

# Use the same PostgreSQL connection string as your app
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/landmark")
engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    # 1. Remove old test users by phone pattern
    conn.execute(text("DELETE FROM users WHERE phone LIKE '999999%'"))
    # Ensure the specific IDs are free (delete if they exist)
    conn.execute(text("DELETE FROM users WHERE id IN (2, 3, 4, 5)"))

    # 2. Insert fresh test users (explicit IDs, but safe now)
    conn.execute(text("""
        INSERT INTO users
        (id, phone, name, role, plan, referral_code, subscription_expiry, business_limit, extra_businesses_purchased)
        VALUES
        (2, '9999999991', 'Free Tester', 'free', NULL, 'FREE01', NULL, 0, 0),
        (3, '9999999992', 'Service Tester', 'service_provider', 'service', 'SERV01', '2027-06-01', 0, 0),
        (4, '9999999993', 'Basic Biz', 'business_basic', 'basic', 'BASIC01', '2026-01-01', 1, 0),
        (5, '9999999994', 'Premium Biz', 'business_premium', 'premium', 'PREMIUM01', '2027-06-01', 3, 0)
    """))
    conn.commit()

    # 3. Show inserted rows
    print("✅ Test users inserted. Current test users:")
    rows = conn.execute(text("SELECT id, phone, role FROM users WHERE phone LIKE '9999%' ORDER BY id")).fetchall()
    for r in rows:
        print(f"ID {r._mapping['id']}: {r._mapping['phone']} → {r._mapping['role']}")