# setup_admin.py
"""Promote an existing user to admin. Does not create users."""
import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv

load_dotenv()

# Hardcoded for this admin promotion run.
ADMIN_PHONE = "9959543954"

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL is not set.")
    sys.exit(1)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    poolclass=NullPool,
    connect_args={
        "sslmode": "require",
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    },
)


def setup_admin():
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("UPDATE users SET role = 'admin' WHERE phone = :phone"),
                {"phone": ADMIN_PHONE},
            )
            conn.commit()
            if result.rowcount == 0:
                print(f"ERROR: No user found with phone {ADMIN_PHONE}. Role was not updated.")
                return 1
            print(f"SUCCESS: User {ADMIN_PHONE} is now an admin ({result.rowcount} row updated).")
            return 0
    except Exception as exc:
        print(f"ERROR: Failed to update admin role: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(setup_admin())
