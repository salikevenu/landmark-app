import random
import string
import qrcode
import os
import logging
logger = logging.getLogger(__name__)
from database.init_db import get_db
from config.payment_config import BASE_URL       # make sure this is defined

def create_unique_referral_code(length=6):
    """Generate an unused referral code using the shared DB connection."""
    conn = get_db()
    letters = string.ascii_uppercase + string.digits

    while True:
        code = ''.join(random.choice(letters) for _ in range(length))
        existing = conn.execute(
            "SELECT id FROM users WHERE referral_code = ?", (code,)
        ).fetchone()
        if not existing:
            return code

def create_referral_assets(user_id, referral_code):
    """Create QR code and return link + path. Uses BASE_URL from config."""
    referral_link = f"{BASE_URL}/ref/{referral_code}"

    # Use the same folder as the rest of the app
    qr_dir = "static/qrcodes"
    os.makedirs(qr_dir, exist_ok=True)

    qr_path = os.path.join(qr_dir, f"user_{user_id}.png")

    try:
        qr = qrcode.make(referral_link)
        qr.save(qr_path)
    except Exception as e:
        logger.info(f"Failed to create QR: {e}")
        return referral_link, None

    return referral_link, qr_path