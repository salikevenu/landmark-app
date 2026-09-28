"""Lower the otp_max_attempts admin setting from 5 to 3 on existing databases.

database/init_db.py now seeds otp_max_attempts = '3' (was '5'), but its
seed insert is ON CONFLICT (key) DO NOTHING, so every database created
before that change still holds '5'. routes/auth_routes.py already caps the
value it reads at MAX_OTP_ATTEMPTS (3), so runtime behaviour is already 3;
this makes the stored value (and what the admin settings page shows) match.

Any value that isn't a valid 1-3 is set to '3'. An admin who deliberately
tightened it to 1 or 2 keeps that. Inserts the row if it's missing.

Idempotent and safe to run repeatedly (e.g. once per deploy, by hand):
    python -m migrations.set_otp_max_attempts_3
"""
from sqlalchemy import text
import logging

from database.init_db import get_db_connection

logger = logging.getLogger(__name__)


def set_otp_max_attempts_3():
    conn = get_db_connection()
    try:
        updated = conn.execute(
            text("""
                UPDATE admin_settings SET value = '3', updated_at = CURRENT_TIMESTAMP
                WHERE key = 'otp_max_attempts'
                  AND (value IS NULL OR value NOT IN ('1', '2', '3'))
            """)
        )
        conn.execute(
            text("""
                INSERT INTO admin_settings (key, value)
                VALUES ('otp_max_attempts', '3')
                ON CONFLICT (key) DO NOTHING
            """)
        )
        conn.commit()
        logger.info(
            "set_otp_max_attempts_3: lowered otp_max_attempts on %d row(s)",
            updated.rowcount,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    set_otp_max_attempts_3()
