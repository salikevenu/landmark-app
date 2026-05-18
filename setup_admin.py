import sqlite3
import jwt
import datetime
import os
from database.init_db import DB_PATH

def create_jwt_token(user_phone, user_role, secret_key, expires_days=30):
    """Create a JWT token manually using PyJWT"""
    payload = {
        'sub': user_phone,  # subject (user identifier)
        'role': user_role,
        'iat': datetime.datetime.utcnow(),
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=expires_days)
    }
    token = jwt.encode(payload, secret_key, algorithm='HS256')
    return token

def setup_admin():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if any admin exists
    cursor.execute("SELECT id, phone, role FROM users WHERE role = 'admin' LIMIT 1")
    admin = cursor.fetchone()
    
    if not admin:
        print("No admin found. Creating one...")
        # You can change these values
        phone = "admin@example.com"   # or your phone number
        name = "Super Admin"
        role = "admin"
        referral_code = "ADMIN123"
        
        try:
            cursor.execute("""
                INSERT INTO users (phone, name, role, referral_code, wallet_balance)
                VALUES (?, ?, ?, ?, 0)
            """, (phone, name, role, referral_code))
            conn.commit()
            print(f"✅ Admin user created with phone: {phone}")
            admin_phone = phone
            admin_role = role
        except sqlite3.IntegrityError:
            print(f"User with phone {phone} already exists. Trying to update role...")
            cursor.execute("UPDATE users SET role = 'admin' WHERE phone = ?", (phone,))
            conn.commit()
            admin_phone = phone
            admin_role = 'admin'
    else:
        admin_id, admin_phone, admin_role = admin
        print(f"Admin already exists: {admin_phone} (role={admin_role})")
    
    conn.close()
    
    # IMPORTANT: Use the SAME JWT_SECRET_KEY as your Flask app
    # You can get it from your config, e.g.:
    # Option A: Read from environment variable
    secret_key = os.environ.get('JWT_SECRET_KEY', 'your-secret-key-here')
    # Option B: If you have a config.py, you can import it
    # from config import Config
    # secret_key = Config.JWT_SECRET_KEY
    
    token = create_jwt_token(admin_phone, admin_role, secret_key)
    
    print("\n" + "="*60)
    print("✅ ADMIN ACCESS TOKEN (use this in Authorization header):")
    print(token)
    print("="*60)
    print("\n📌 Test with curl:")
    print(f'curl -H "Authorization: Bearer {token}" http://localhost:5000/api/admin/stats')
    print("\n📌 Or in Python requests:")
    print(f'requests.get("http://localhost:5000/api/admin/stats", headers={{"Authorization": f"Bearer {token}"}})')
    
    return token

if __name__ == "__main__":
    setup_admin()