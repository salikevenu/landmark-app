from datetime import datetime, timedelta
from PIL import Image
from sqlalchemy import text
import logging

from database.init_db import get_db_connection

logger = logging.getLogger(__name__)

PLAN_REWARDS = {
    "service": 25,
    "basic": 50,
    "premium": 100
}


def get_referral_info(user_id):
    with get_db_connection() as conn:
        row = conn.execute(text("""
            SELECT u.referral_code, COALESCE(wb.balance, 0) AS wallet_balance
            FROM users u
            LEFT JOIN wallet_balance wb ON wb.user_id = u.id
            WHERE u.id = :uid
        """), {"uid": user_id}).fetchone()

    if not row:
        return None

    code = row._mapping["referral_code"]
    return {
        "referral_code": code,
        "wallet_balance": row._mapping["wallet_balance"],
    }


def process_referral_reward(user_id, plan_type, payment_id):
    """LEGACY / DISABLED. Old flat ₹25/50/100 rewards. Live path is 10% + 5%."""
    logger.error(
        "LEGACY DISABLED: referral_service.process_referral_reward is not the live commission path"
    )
    return None