import sqlite3
import os
from datetime import timedelta
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from database.init_db import DB_PATH

# -----------------------------------
# Flask App Setup – use the REAL secret
# -----------------------------------
app = Flask(__name__)

# THE SECRET MUST MATCH YOUR RUNNING APP
app.config["JWT_SECRET_KEY"] = "hlecFd2cJQY0UCyXcq5Fpo1UCLyBERNEu2hUiv_kM60"

jwt = JWTManager(app)

# -----------------------------------
# Connect DB
# -----------------------------------
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

user = conn.execute("""
    SELECT id, phone, role
    FROM users
    WHERE role = 'admin'
    LIMIT 1
""").fetchone()

conn.close()

# -----------------------------------
# Generate Token (30 days expiry)
# -----------------------------------
if not user:
    print("❌ No admin user found.")
    print("Run: UPDATE users SET role='admin' WHERE phone='YOUR_PHONE';")
else:
    with app.app_context():
        token = create_access_token(
            identity=str(user["id"]),          # or user["phone"]
            additional_claims={
                "role": user["role"],
                "phone": user["phone"]
            },
            expires_delta=timedelta(days=30)    # 30 days
        )
        print("\n✅ ADMIN USER FOUND")
        print("Phone:", user["phone"])
        print("Role:", user["role"])
        print("\n✅ ADMIN TOKEN (valid 30 days):\n")
        print(token)