import sqlite3
from database.init_db import DB_PATH

def migrate_add_status_column():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE listings ADD COLUMN status TEXT DEFAULT 'pending'")
        print("✅ Column 'status' added to listings table.")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e):
            print("ℹ️ Column 'status' already exists.")
        else:
            print(f"⚠️ Error: {e}")
    finally:
        conn.commit()
        conn.close()

if __name__ == "__main__":
    migrate_add_status_column()