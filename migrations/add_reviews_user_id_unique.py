"""Additive reviews identity uniqueness. Do NOT apply to production from this agent.

Preflight (read-only) must show zero duplicates:

    SELECT listing_id, user_id, COUNT(*) AS c
    FROM reviews
    WHERE user_id IS NOT NULL
    GROUP BY listing_id, user_id
    HAVING COUNT(*) > 1;

If any rows are returned, stop. Do not create the unique index until duplicates
are resolved manually.

This migration:
- adds reviews.user_id (canonical reviewer identity)
- does not drop reviews.user_phone
- creates a partial unique index UNIQUE(listing_id, user_id) WHERE user_id IS NOT NULL
"""
from sqlalchemy import text

from database.init_db import get_db_connection


def migrate_reviews_user_id_unique():
    conn = get_db_connection()
    try:
        conn.execute(text("""
            ALTER TABLE reviews
            ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_listing_user
            ON reviews (listing_id, user_id)
            WHERE user_id IS NOT NULL
        """))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit("Do not apply this migration to production from this script.")
