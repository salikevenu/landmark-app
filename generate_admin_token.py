# generate_admin_token.py
import os
from datetime import timedelta
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from sqlalchemy import create_engine, text

# -----------------------------------
# Flask App Setup (same JWT secret)
# -----------------------------------
app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = "hlecFd2cJQY0UCyXcq5Fpo1UCLyBERNEu2hUiv_kM60"
jwt = JWTManager(app)

# -----------------------------------
# PostgreSQL connection (use the same DB URL as in init_db)
# -----------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/landmark")
engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    user = conn.execute(
        text("SELECT id, phone, role FROM users WHERE role = 'admin' LIMIT 1")
    ).fetchone()

    if not user:
        print("❌ No admin user found.")
        print("Run: UPDATE users SET role='admin' WHERE phone='YOUR_PHONE';")
    else:
        with app.app_context():
            token = create_access_token(
                identity=str(user._mapping["id"]),
                additional_claims={
                    "role": user._mapping["role"],
                    "phone": user._mapping["phone"]
                },
                expires_delta=timedelta(days=30)
            )
            print("\n✅ ADMIN USER FOUND")
            print("Phone:", user._mapping["phone"])
            print("Role:", user._mapping["role"])
            print("\n✅ ADMIN TOKEN (valid 30 days):\n")
            print(token)