import os
from datetime import datetime, timedelta
import qrcode
from PIL import Image
from sqlalchemy import text

from config.payment_config import BASE_URL
from database.init_db import get_db_connection

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
    conn = get_db_connection()

    # Find referrer
    referral_row = conn.execute(
        text("SELECT referred_by FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()

    if not referral_row or not referral_row._mapping["referred_by"]:
        return

    referrer_id = referral_row._mapping["referred_by"]

    # Prevent self-referral
    if referrer_id == user_id:
        return

    reward = PLAN_REWARDS.get(plan_type, 0)
    if reward == 0:
        return

    # Prevent duplicate reward for the same payment
    existing = conn.execute(
        text("SELECT id FROM referral_transactions WHERE payment_id = :pid"),
        {"pid": payment_id}
    ).fetchone()
    if existing:
        return

    # Upsert wallet_balance row: insert if not exists, else update
    # Use PostgreSQL's ON CONFLICT ... DO NOTHING or DO UPDATE.
    # First, ensure a row exists (INSERT ... ON CONFLICT DO NOTHING, then update)
    conn.execute(text("""
        INSERT INTO wallet_balance (user_id, balance)
        VALUES (:uid, 0)
        ON CONFLICT (user_id) DO NOTHING
    """), {"uid": referrer_id})

    # Update wallet balance
    conn.execute(text("""
        UPDATE wallet_balance
        SET balance = balance + :reward
        WHERE user_id = :uid
    """), {"reward": reward, "uid": referrer_id})

    # Compute unlock date (7 days from now)
    unlock_date = datetime.utcnow() + timedelta(days=7)

    # Record wallet transaction (locked until unlock_date)
    conn.execute(text("""
        INSERT INTO wallet_transactions
        (user_id, amount, type, source, reference_id, status, unlock_at)
        VALUES (:uid, :amount, 'credit', 'referral_reward', :ref_id, 'locked', :unlock)
    """), {
        "uid": referrer_id,
        "amount": reward,
        "ref_id": payment_id,
        "unlock": unlock_date
    })

    # Record referral transaction
    conn.execute(text("""
        INSERT INTO referral_transactions
        (referrer_id, referred_user_id, reward_amount, status)
        VALUES (:referrer, :referred, :reward, 'completed')
    """), {
        "referrer": referrer_id,
        "referred": user_id,
        "reward": reward
    })

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