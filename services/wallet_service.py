# services/wallet_service.py
from sqlalchemy import text
from database.init_db import get_db_connection


# =========================
# GET WALLET BALANCE
# =========================
def get_wallet_balance(user_id):
    conn = get_db_connection()
    row = conn.execute(
        text("SELECT balance FROM wallet_balance WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchone()
    return row._mapping["balance"] if row else 0


# =========================
# CREDIT WALLET
# =========================
def credit_wallet(user_id, amount, source="system", reference_id=None):
    conn = get_db_connection()
    # Ensure wallet row exists (PostgreSQL upsert)
    conn.execute(text("""
        INSERT INTO wallet_balance (user_id, balance)
        VALUES (:uid, 0)
        ON CONFLICT (user_id) DO NOTHING
    """), {"uid": user_id})
    # Add amount
    conn.execute(text("""
        UPDATE wallet_balance
        SET balance = balance + :amount
        WHERE user_id = :uid
    """), {"amount": amount, "uid": user_id})
    # Record transaction
    conn.execute(text("""
        INSERT INTO wallet_transactions
        (user_id, amount, type, source, reference_id, status)
        VALUES (:uid, :amount, 'credit', :source, :ref_id, 'completed')
    """), {
        "uid": user_id,
        "amount": amount,
        "source": source,
        "ref_id": reference_id
    })
    conn.commit()


# =========================
# DEBIT WALLET
# =========================
def debit_wallet(user_id, amount, source="withdraw", reference_id=None):
    conn = get_db_connection()
    row = conn.execute(
        text("SELECT balance FROM wallet_balance WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if not row or row._mapping["balance"] < amount:
        return False

    conn.execute(text("""
        UPDATE wallet_balance
        SET balance = balance - :amount
        WHERE user_id = :uid
    """), {"amount": amount, "uid": user_id})
    conn.execute(text("""
        INSERT INTO wallet_transactions
        (user_id, amount, type, source, reference_id, status)
        VALUES (:uid, :amount, 'debit', :source, :ref_id, 'completed')
    """), {
        "uid": user_id,
        "amount": amount,
        "source": source,
        "ref_id": reference_id
    })
    conn.commit()
    return True


# =========================
# PROCESS REFERRAL REWARD (alternative entry point)
# =========================
def process_referral(user_id, purchase_amount):
    """
    Legacy referral reward – credits referrer with 20% of purchase_amount.
    (This is a simpler version; the dedicated `process_referral_reward` in
    referral_service is more robust.)
    """
    conn = get_db_connection()
    # Get referrer's referral code stored in 'referred_by' column
    referred_row = conn.execute(
        text("SELECT referred_by FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if not referred_row or not referred_row._mapping["referred_by"]:
        return

    referral_code = referred_row._mapping["referred_by"]

    # Find the referrer user by their own referral_code
    referrer = conn.execute(
        text("SELECT id FROM users WHERE referral_code = :code"),
        {"code": referral_code}
    ).fetchone()
    if not referrer:
        return

    referrer_id = referrer._mapping["id"]
    reward = purchase_amount * 0.20

    # Credit the referrer's wallet
    credit_wallet(referrer_id, reward, "Referral reward", f"REF-{user_id}")


# =========================
# GET WALLET TRANSACTIONS
# =========================
def get_wallet_transactions(user_id):
    conn = get_db_connection()
    rows = conn.execute(text("""
        SELECT type, amount, description, created_at
        FROM wallet_transactions
        WHERE user_id = :uid
        ORDER BY id DESC
        LIMIT 20
    """), {"uid": user_id}).fetchall()

    return [
        {
            "type": r._mapping["type"],
            "amount": r._mapping["amount"],
            "description": r._mapping["description"],
            "date": r._mapping["created_at"]
        }
        for r in rows
    ]

# =========================
# ADD PENDING REFERRAL REWARD
# =========================
def add_pending_referral_reward(user_id, referral_transaction_id):
    """
    Adds ₹2 pending referral reward to wallet when a new user is referred.
    Reward stays 'pending' until admin verifies the referral.
    """
    conn = get_db_connection()
    # Ensure wallet row exists
    conn.execute(text("""
        INSERT INTO wallet_balance (user_id, balance)
        VALUES (:uid, 0)
        ON CONFLICT (user_id) DO NOTHING
    """), {"uid": user_id})
    
    # Insert pending transaction
    conn.execute(text("""
        INSERT INTO wallet_transactions
        (user_id, amount, type, source, reference_id, status, created_at)
        VALUES (:uid, 2, 'credit', 'referral', :ref_id, 'pending', NOW())
    """), {
        "uid": user_id,
        "ref_id": str(referral_transaction_id)
    })
    
    conn.commit()
    return True