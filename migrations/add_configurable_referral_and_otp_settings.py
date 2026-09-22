"""Make referral commission and OTP timing admin-configurable.

Two things happen here:

1. Data fix: recurring_commission_percent was seeded long ago as '5' by an
   older migration (migrations/add_admin_tables.py), before it was ever
   actually read by any code -- services/referral_commission.py's live
   RECURRING_RATE has always been the real, hardcoded 10%. database/init_db.py
   was later corrected to seed '10' for fresh databases, but ON CONFLICT DO
   NOTHING means that fix never reached this already-existing row. Unfreezing
   this setting (routes/admin_routes.py, services/admin_service.py) without
   this fix would silently halve real commissions the moment anyone saved it
   unchanged. Only touches the row if it's still at that known-stale '5'.

2. Seeds the new settings keys referral_commission.py and auth_routes.py now
   read at runtime, with defaults matching their previous hardcoded values --
   so wiring them up changes no behavior until an admin actually edits one.

Idempotent and safe to run repeatedly (e.g. once per deploy, by hand).
"""
from sqlalchemy import text
import logging

from database.init_db import get_db_connection

logger = logging.getLogger(__name__)

NEW_DEFAULTS = [
    ("referral_first_bonus_service_provider", "50"),
    ("referral_first_bonus_business_basic", "100"),
    ("referral_first_bonus_business_premium", "150"),
    ("otp_verification_expiry_seconds", "300"),
    ("otp_resend_cooldown_seconds", "60"),
    ("otp_max_attempts", "5"),
]


def add_configurable_referral_and_otp_settings():
    conn = get_db_connection()
    try:
        fixed = conn.execute(
            text("""
                UPDATE admin_settings SET value = '10', updated_at = CURRENT_TIMESTAMP
                WHERE key = 'recurring_commission_percent' AND value = '5'
            """)
        )

        for key, value in NEW_DEFAULTS:
            conn.execute(
                text("""
                    INSERT INTO admin_settings (key, value)
                    VALUES (:key, :value)
                    ON CONFLICT (key) DO NOTHING
                """),
                {"key": key, "value": value},
            )

        conn.commit()
        logger.info(
            "add_configurable_referral_and_otp_settings: corrected recurring_commission_percent=%d row(s), "
            "seeded %d new setting keys",
            fixed.rowcount, len(NEW_DEFAULTS),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    add_configurable_referral_and_otp_settings()
