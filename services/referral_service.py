import os
from datetime import datetime, timedelta
import qrcode
from PIL import Image

from config.payment_config import BASE_URL
from database.init_db import get_db         # canonical shared helper

PLAN_REWARDS = {
    "service": 25,
    "basic": 50,
    "premium": 100
}


def get_referral_info(user_id):
    conn = get_db()
    row = conn.execute("""
        SELECT referral_code, wallet_balance
        FROM users
        WHERE id = ?
    """, (user_id,)).fetchone()

    if not row:
        return None

    return {
        "referral_code": row["referral_code"],
        "wallet_balance": row["wallet_balance"]
    }


def process_referral_reward(user_id, plan_type, payment_id):
    conn = get_db()

    # Find referrer
    referral_row = conn.execute(
        "SELECT referred_by FROM users WHERE id = ?", (user_id,)
    ).fetchone()

    if not referral_row or not referral_row["referred_by"]:
        return

    referrer_id = referral_row["referred_by"]

    # Prevent self-referral
    if referrer_id == user_id:
        return

    reward = PLAN_REWARDS.get(plan_type, 0)
    if reward == 0:
        return

    # Prevent duplicate reward
    existing = conn.execute(
        "SELECT id FROM referral_transactions WHERE payment_id = ?", (payment_id,)
    ).fetchone()
    if existing:
        return

    # Ensure wallet row exists
    conn.execute("INSERT OR IGNORE INTO wallet_balance (user_id, balance) VALUES (?, 0)", (referrer_id,))

    # Update wallet balance
    conn.execute(
        "UPDATE wallet_balance SET balance = balance + ? WHERE user_id = ?",
        (reward, referrer_id)
    )

    # Compute unlock date (7 days from now)
    unlock_date = datetime.utcnow() + timedelta(days=7)

    # Record wallet transaction (locked until unlock_date)
    conn.execute("""
        INSERT INTO wallet_transactions
        (user_id, amount, type, source, reference_id, status, unlock_at)
        VALUES (?, ?, ?, ?, ?, 'locked', ?)
    """, (referrer_id, reward, 'credit', 'referral_reward', payment_id, unlock_date))

    # Record referral transaction
    conn.execute("""
        INSERT INTO referral_transactions
        (referrer_id, referred_user_id, reward_amount, status)
        VALUES (?, ?, ?, 'completed')
    """, (referrer_id, user_id, reward))

    conn.commit()


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