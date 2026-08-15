# setup_admin.py (PostgreSQL version)
import argparse
import jwt
import datetime
import os
from sqlalchemy import create_engine, text
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# Use the same PostgreSQL URL as in database/init_db.py
DATABASE_URL = os.getenv("DATABASE_URL")
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

def setup_admin(phone=None):
    phone = (phone or os.getenv("ADMIN_PHONE") or "").strip()
    if not phone:
        raise SystemExit("Admin phone required: pass --phone or set ADMIN_PHONE in the environment.")

    secret_key = os.environ.get("JWT_SECRET_KEY")
    if not secret_key:
        raise SystemExit("JWT_SECRET_KEY is not set. Refusing to mint an admin token.")

    with engine.connect() as conn:
        # =============================================
        # ✅ TEMPORARY: Add missing logo_url column
        # =============================================
        conn.execute(text("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS logo_url TEXT;"))
        conn.commit()
        print("✅ Column logo_url added successfully.")
        # =============================================

        # Check if user exists
        user = conn.execute(text("SELECT id, phone, role FROM users WHERE phone = :phone"), {"phone": phone}).fetchone()
        
        if not user:
            # User doesn't exist, create them as admin
            name = "Admin User"
            role = "admin"
            referral_code = "ADMIN123"

            result = conn.execute(text("""
                INSERT INTO users (phone, name, role, referral_code, wallet_balance)
                VALUES (:phone, :name, :role, :code, 0)
                ON CONFLICT (phone) DO NOTHING
                RETURNING id, phone, role
            """), {"phone": phone, "name": name, "role": role, "code": referral_code}).fetchone()
            
            if result:
                admin_id, admin_phone, admin_role = result
                logger.info(f"✅ Admin user created with phone: {admin_phone}")
            else:
                logger.info("User already exists but could not be created.")
                return None
        else:
            admin_id, admin_phone, admin_role = user._mapping["id"], user._mapping["phone"], user._mapping["role"]
            # ✅ If the user exists but is not admin, update them
            if admin_role != "admin":
                conn.execute(text("UPDATE users SET role = 'admin' WHERE phone = :phone"), {"phone": phone})
                conn.commit()
                logger.info(f"✅ Updated user {admin_phone} to admin role")
                admin_role = "admin"
            else:
                logger.info(f"✅ User {admin_phone} is already an admin")

    token = create_jwt_token(admin_id, admin_role, secret_key)

    logger.info("\n" + "="*60)
    logger.info("✅ ADMIN ACCESS TOKEN (use this in Authorization header):")
    logger.info(token)
    logger.info("="*60)
    logger.info("\n📌 Test with curl:")
    logger.info(f'curl -H "Authorization: Bearer {token}" http://localhost:8000/api/admin/stats')
    logger.info("\n📌 Or in Python requests:")
    logger.info(f'requests.get("http://localhost:8000/api/admin/stats", headers={{"Authorization": f"Bearer {token}"}})')
    return token

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create or promote an admin user.")
    parser.add_argument(
        "--phone",
        default=os.getenv("ADMIN_PHONE"),
        help="10-digit admin phone. Or set ADMIN_PHONE in the environment.",
    )
    args = parser.parse_args()
    setup_admin(args.phone)
