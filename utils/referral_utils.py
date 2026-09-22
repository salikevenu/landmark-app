import random
import string
import logging
logger = logging.getLogger(__name__)
from database.init_db import get_db_connection

def create_unique_referral_code(length=6):
    """Generate an unused referral code using the shared DB connection."""
    conn = get_db_connection()
    letters = string.ascii_uppercase + string.digits

    while True:
        code = ''.join(random.choice(letters) for _ in range(length))
        existing = conn.execute(
            "SELECT id FROM users WHERE referral_code = ?", (code,)
        ).fetchone()
        if not existing:
            return code