"""Backfill users.referral_code for rows created before it was populated.

Idempotent and safe to run repeatedly (e.g. once per deploy, by hand):
- Only touches rows where referral_code IS NULL.
- Never overwrites an existing code.
- Reuses the same generator + collision-retry logic as registration
  (routes.auth_routes.generate_referral_code), so backfilled codes are
  indistinguishable in format from ones assigned at signup.
- The UPDATE's "AND referral_code IS NULL" guard makes each row assignment
  safe even if run concurrently with another process.
"""
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
import logging

from database.init_db import get_db_connection
from routes.auth_routes import generate_referral_code

logger = logging.getLogger(__name__)

MAX_ATTEMPTS_PER_USER = 8


def backfill_referral_codes():
    conn = get_db_connection()
    try:
        rows = conn.execute(
            text("SELECT id FROM users WHERE referral_code IS NULL")
        ).fetchall()
        user_ids = [row._mapping["id"] for row in rows]

        assigned = 0
        failed = []
        for uid in user_ids:
            for _ in range(MAX_ATTEMPTS_PER_USER):
                code = generate_referral_code()
                try:
                    result = conn.execute(
                        text("""
                            UPDATE users SET referral_code = :code
                            WHERE id = :uid AND referral_code IS NULL
                        """),
                        {"code": code, "uid": uid},
                    )
                    conn.commit()
                    if result.rowcount:
                        assigned += 1
                    break
                except IntegrityError:
                    conn.rollback()
                    continue
            else:
                failed.append(uid)
                logger.error(
                    "backfill_referral_codes: could not assign a unique code to user id=%s "
                    "after %d attempts", uid, MAX_ATTEMPTS_PER_USER,
                )

        logger.info(
            "backfill_referral_codes: %d/%d users assigned a code (%d failed)",
            assigned, len(user_ids), len(failed),
        )
        return {"total_missing": len(user_ids), "assigned": assigned, "failed": failed}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    backfill_referral_codes()
