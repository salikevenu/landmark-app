"""Seed the free_listing_limit admin_settings key for existing databases.

Before this, creating a listing required an active paid plan outright --
a free/no-plan user was hard-blocked at both the create-listing page and
the create-listing API, with business_limit defaulting to 0. That's the
confirmed reason the platform had signups but zero listings: every free
user hit a paywall before ever creating anything.

services/subscription_access.py's get_business_limit_for_user() now floors
every user's listing cap at this admin-configurable setting, so no
per-user data needs to change here -- existing free users automatically
get the allowance the moment this key exists, no backfill of the users
table required.

Idempotent and safe to run repeatedly (e.g. once per deploy, by hand).
"""
from sqlalchemy import text
import logging

from database.init_db import get_db_connection

logger = logging.getLogger(__name__)


def add_free_listing_allowance():
    conn = get_db_connection()
    try:
        conn.execute(
            text("""
                INSERT INTO admin_settings (key, value)
                VALUES ('free_listing_limit', '1')
                ON CONFLICT (key) DO NOTHING
            """)
        )
        conn.commit()
        logger.info("add_free_listing_allowance: free_listing_limit seeded (or already present)")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    add_free_listing_allowance()
