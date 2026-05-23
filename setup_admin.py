# setup_admin.py (PostgreSQL version)
import jwt
import datetime
import os
from sqlalchemy import create_engine, text

# Use the same PostgreSQL URL as in database/init_db.py
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/landmark")
engine = create_engine(DATABASE_URL)

def create_jwt_token(user_id, user_role, secret_key, expires_days=30):
    """Create a JWT token with user_id as identity (matching app's JWT)"""
    payload = {
        'sub': str(user_id),           # identity = user id
        'role': user_role,
        'iat': datetime.datetime.utcnow(),
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=expires_days)
    }
    token = jwt.encode(payload, secret_key, algorithm='HS256')
    return token

def setup_admin():
    with engine.connect() as conn:
        # Check if any admin exists
        admin = conn.execute(text("SELECT id, phone, role FROM users WHERE role = 'admin' LIMIT 1")).fetchone()
        admin_id = None
        admin_phone = None
        admin_role = None

        if not admin:
            print("No admin found. Creating one...")
            phone = "admin@example.com"
            name = "Super Admin"
            role = "admin"
            referral_code = "ADMIN123"

            try:
                # Use RETURNING to get the new id
                result = conn.execute(text("""
                    INSERT INTO users (phone, name, role, referral_code, wallet_balance)
                    VALUES (:phone, :name, :role, :code, 0)
                    ON CONFLICT (phone) DO NOTHING
                    RETURNING id, phone, role
                """), {"phone": phone, "name": name, "role": role, "code": referral_code}).fetchone()
                if result:
                    admin_id, admin_phone, admin_role = result
                else:
                    # Phone already exists, let's update role
                    conn.execute(text("UPDATE users SET role = 'admin' WHERE phone = :phone"), {"phone": phone})
                    conn.commit()
                    # Fetch the updated row
                    updated = conn.execute(text("SELECT id, phone, role FROM users WHERE phone = :phone"), {"phone": phone}).fetchone()
                    admin_id, admin_phone, admin_role = updated
                print(f"✅ Admin user created with phone: {admin_phone}")
            except Exception as e:
                print(f"Error creating admin: {e}")
                return None
        else:
            admin_id, admin_phone, admin_role = admin._mapping["id"], admin._mapping["phone"], admin._mapping["role"]
            print(f"Admin already exists: {admin_phone} (role={admin_role})")

    # IMPORTANT: Use the SAME JWT_SECRET_KEY as your Flask app
    secret_key = os.environ.get('JWT_SECRET_KEY', 'hlecFd2cJQY0UCyXcq5Fpo1UCLyBERNEu2hUiv_kM60')

    token = create_jwt_token(admin_id, admin_role, secret_key)

    print("\n" + "="*60)
    print("✅ ADMIN ACCESS TOKEN (use this in Authorization header):")
    print(token)
    print("="*60)
    print("\n📌 Test with curl:")
    print(f'curl -H "Authorization: Bearer {token}" http://localhost:8000/api/admin/stats')
    print("\n📌 Or in Python requests:")
    print(f'requests.get("http://localhost:8000/api/admin/stats", headers={{"Authorization": f"Bearer {token}"}})')
    return token

if __name__ == "__main__":
    setup_admin()