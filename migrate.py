# migrate_add_status_column.py
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from database.init_db import get_db

def migrate_add_status_column():
    conn = get_db()
    try:
        # Check if column already exists
        column_exists = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'listings' AND column_name = 'status'
        """)).fetchone()

        if column_exists:
            print("ℹ️ Column 'status' already exists.")
        else:
            conn.execute(text("ALTER TABLE listings ADD COLUMN status TEXT DEFAULT 'pending'"))
            conn.commit()
            print("✅ Column 'status' added to listings table.")
    except ProgrammingError as e:
        # Catch any unexpected error (e.g., table doesn't exist)
        print(f"⚠️ Error: {e}")
    # No need to close connection; Flask's teardown handles it

if __name__ == "__main__":
    migrate_add_status_column()