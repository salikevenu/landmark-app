# services/referral_commission.py
from datetime import datetime, timedelta
from sqlalchemy import text
from database.init_db import get_db

def next_saturday_6pm_ist():
    """Return next Saturday 6:00 PM as UTC datetime (IST = UTC+5:30)."""
    now_utc = datetime.utcnow()
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    weekday = now_ist.weekday()  # Monday=0, Sunday=6
    days_until_saturday = (5 - weekday) % 7
    # If today is Saturday and time is already past 6 PM, move to next Saturday
    if days_until_saturday == 0 and now_ist.hour >= 18:
        days_until_saturday = 7
    sat_ist = now_ist + timedelta(days=days_until_saturday)
    sat_ist = sat_ist.replace(hour=18, minute=0, second=0, microsecond=0)
    return sat_ist - timedelta(hours=5, minutes=30)  # back to UTC

def process_referral_commission(referred_user_id, payment_amount):
    """Queue 10% first-bonus and 5% recurring commission for the referrer."""
    conn = get_db()
    user = conn.execute(
        text("SELECT referred_by, first_sub_commission_paid FROM users WHERE id = :uid"),
        {"uid": referred_user_id}
    ).fetchone()
    if not user or not user._mapping["referred_by"]:
        return  # no referrer

    referrer_id = user._mapping["referred_by"]
    unlock_at = next_saturday_6pm_ist().strftime("%Y-%m-%d %H:%M:%S")

    # 10% one-time bonus
    if not user._mapping["first_sub_commission_paid"]:
        bonus = round(payment_amount * 0.10, 2)
        if bonus > 0:
            conn.execute(text("""
                INSERT INTO wallet_transactions
                (user_id, amount, type, source, reference_id, status, unlock_at, created_at)
                VALUES (:referrer_id, :bonus, 'credit', '5%_base_+_5%_activation', :ref_id, 'locked', :unlock_at, CURRENT_TIMESTAMP)
            """), {
                "referrer_id": referrer_id,
                "bonus": bonus,
                "ref_id": f"user_{referred_user_id}",
                "unlock_at": unlock_at
            })
            conn.execute(text("UPDATE users SET first_sub_commission_paid = 1 WHERE id = :uid"),
                         {"uid": referred_user_id})

    # 5% recurring commission
    recurring = round(payment_amount * 0.05, 2)
    if recurring > 0:
        conn.execute(text("""
            INSERT INTO wallet_transactions
            (user_id, amount, type, source, reference_id, status, unlock_at, created_at)
            VALUES (:referrer_id, :recurring, 'credit', 'referral_recurring', :ref_id, 'locked', :unlock_at, CURRENT_TIMESTAMP)
        """), {
            "referrer_id": referrer_id,
            "recurring": recurring,
            "ref_id": f"user_{referred_user_id}",
            "unlock_at": unlock_at
        })

    conn.commit()