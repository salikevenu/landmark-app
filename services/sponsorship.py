"""Admin-granted sponsorship windows.

Public ranking and badges must use a live window, not listings.is_sponsored alone:

    is_sponsored = 1
    AND sponsored_ads.start_date <= now
    AND sponsored_ads.end_date > now
    AND sponsored_ads is active

Cleanup only clears listings.is_sponsored when no non-expired grant remains.
It never deletes sponsored_ads rows and never touches is_featured.
"""
import logging
from datetime import datetime

from sqlalchemy import text

from database.init_db import get_db_connection

logger = logging.getLogger(__name__)

# Exclusive end: at exactly end_date the grant is no longer live.
_WINDOW_SQL = """
    SELECT 1 FROM sponsored_ads sa
    WHERE sa.listing_id = {id_col}
      AND COALESCE(sa.is_active, 1) = 1
      AND sa.start_date <= CURRENT_TIMESTAMP
      AND sa.end_date > CURRENT_TIMESTAMP
"""


def public_is_sponsored_sql(alias=""):
    """Boolean SQL for public ranking/badges. alias is a trusted identifier or empty."""
    if alias and not str(alias).replace("_", "").isalnum():
        raise ValueError("invalid SQL alias")
    prefix = f"{alias}." if alias else ""
    id_col = f"{prefix}id"
    flag_col = f"{prefix}is_sponsored"
    return (
        f"(COALESCE({flag_col}, 0) = 1 "
        f"AND EXISTS ({_WINDOW_SQL.format(id_col=id_col)}))"
    )


def sponsorship_rank_sql(alias=""):
    return f"(CASE WHEN {public_is_sponsored_sql(alias)} THEN 1 ELSE 0 END)"


def is_live_sponsored(*, is_sponsored, start_date, end_date, now=None, ad_is_active=1):
    """Pure window check used by tests and any Python-side ranking."""
    now = now or datetime.utcnow()
    if not is_sponsored:
        return False
    if not ad_is_active:
        return False
    if start_date is None or end_date is None:
        return False
    return start_date <= now < end_date


CLEANUP_EXPIRED_SQL = """
    UPDATE listings AS l
    SET is_sponsored = 0
    WHERE l.id IN (
        SELECT x.id
        FROM listings x
        WHERE COALESCE(x.is_sponsored, 0) = 1
          AND NOT EXISTS (
              SELECT 1 FROM sponsored_ads sa
              WHERE sa.listing_id = x.id
                AND COALESCE(sa.is_active, 1) = 1
                AND sa.end_date > CURRENT_TIMESTAMP
          )
        FOR UPDATE OF x SKIP LOCKED
    )
"""


def cleanup_expired_sponsorships(conn=None):
    """Idempotent, concurrent-safe flag cleanup. Does not delete sponsored_ads."""
    owns = conn is None
    if owns:
        conn = get_db_connection()
    try:
        result = conn.execute(text(CLEANUP_EXPIRED_SQL))
        cleared = int(result.rowcount or 0)
        if owns:
            conn.commit()
        return {"cleared": cleared}
    except Exception:
        if owns:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("cleanup_expired_sponsorships failed")
        raise
    finally:
        if owns:
            try:
                conn.close()
            except Exception:
                pass
