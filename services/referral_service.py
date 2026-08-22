import os
from datetime import datetime, timedelta
import qrcode
from PIL import Image
from sqlalchemy import text
import logging

from config.payment_config import BASE_URL
from database.init_db import get_db_connection

logger = logging.getLogger(__name__)

PLAN_REWARDS = {
    "service": 25,
    "basic": 50,
    "premium": 100
}


def get_referral_info(user_id):
    conn = get_db_connection()
    row = conn.execute(text("""
        SELECT referral_code, wallet_balance
        FROM users
        WHERE id = :uid
    """), {"uid": user_id}).fetchone()

    if not row:
        return None

    return {
        "referral_code": row._mapping["referral_code"],
        "wallet_balance": row._mapping["wallet_balance"]
    }


def process_referral_reward(user_id, plan_type, payment_id):
    """LEGACY / DISABLED. Old flat ₹25/50/100 rewards. Live path is 10% + 5%."""
    logger.error(
        "LEGACY DISABLED: referral_service.process_referral_reward is not the live commission path"
    )
    return None


def create_referral_assets(user_id, referral_code):
    referral_link = f"{BASE_URL}?ref={referral_code}"

    os.makedirs("static/qrcodes", exist_ok=True)

    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(referral_link)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    qr_path = f"static/qrcodes/user_{user_id}.png"
    img.save(qr_path)

    return referral_link, qr_path