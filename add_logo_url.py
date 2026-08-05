from database.init_db import engine
from sqlalchemy import text

print("🔄 Connecting to database...")
with engine.connect() as conn:
    conn.execute(text("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS logo_url TEXT;"))
    conn.commit()
    print("✅ Column logo_url added successfully.")