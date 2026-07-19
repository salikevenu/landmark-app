from datetime import datetime, timedelta
from sqlalchemy import text
from database.init_db import get_db_connection

def get_unlock_utc(referral_utc: datetime):
    """Return UTC unlock datetime for a referral commission."""
    ist = referral_utc + timedelta(hours=5, minutes=30)
    days_until_saturday = (5 - ist.weekday()) % 7
    next_saturday_ist = ist + timedelta(days=days_until_saturday)
    next_saturday_ist = next_saturday_ist.replace(hour=18, minute=0, second=0, microsecond=0)

    is_friday_after_6pm = (ist.weekday() == 4 and ist.hour >= 18)
    is_saturday_before_6pm = (ist.weekday() == 5 and ist.hour < 18)
    if is_friday_after_6pm or is_saturday_before_6pm:
        next_saturday_ist += timedelta(days=7)

    return next_saturday_ist - timedelta(hours=5, minutes=30)

def process_referral_commission(referred_user_id, payment_amount):
    """Queue 10% first-bonus and 5% recurring commission for the referrer."""
    conn = get_db_connection()
    user = conn.execute(
        text("SELECT referred_by, first_sub_commission_paid FROM users WHERE id = :uid"),
        {"uid": referred_user_id}
    ).fetchone()
    if not user or not user._mapping["referred_by"]:
        return

    referrer_id = user._mapping["referred_by"]
    referral_time = datetime.utcnow()
    unlock_utc = get_unlock_utc(referral_time)
    unlock_at_str = unlock_utc.strftime("%Y-%m-%d %H:%M:%S")

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
                "unlock_at": unlock_at_str
            })
            conn.execute(text("UPDATE users SET first_sub_commission_paid = 1 WHERE id = :uid"),
                         {"uid": referred_user_id})

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
            "unlock_at": unlock_at_str
        })

    conn.commit()

# ----- LEGACY WRAPPER for wallet_routes.py -----
def next_saturday_6pm_ist():
    """Return next Saturday 6pm IST (as UTC datetime)."""
    return get_unlock_utc(datetime.utcnow())