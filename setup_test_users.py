import sqlite3

DB = 'landmark.db'   # change if your database file is named differently

conn = sqlite3.connect(DB)

# 1. Remove old test users (if any)
conn.execute("DELETE FROM users WHERE phone LIKE '999999%'")

# 2. Insert fresh test users
conn.execute("""
    INSERT INTO users
    (id, phone, name, role, plan, referral_code, subscription_expiry, business_limit, extra_businesses_purchased)
    VALUES
    (2, '9999999991', 'Free Tester', 'free', NULL, 'FREE01', NULL, 0, 0),
    (3, '9999999992', 'Service Tester', 'service_provider', 'service', 'SERV01', '2027-06-01', 0, 0),
    (4, '9999999993', 'Basic Biz', 'business_basic', 'basic', 'BASIC01', '2026-01-01', 1, 0),
    (5, '9999999994', 'Premium Biz', 'business_premium', 'premium', 'PREMIUM01', '2027-06-01', 3, 0)
""")

conn.commit()

# 3. Show the inserted rows
print("✅ Test users inserted. Current test users:")
rows = conn.execute("SELECT id, phone, role FROM users WHERE phone LIKE '9999%' ORDER BY id").fetchall()
for r in rows:
    print(f"ID {r[0]}: {r[1]} → {r[2]}")

conn.close()