# services/wallet_service.py
from sqlalchemy import text
from database.init_db import get_db_connection
import logging

logger = logging.getLogger(__name__)


# =========================
# GET WALLET BALANCE
# =========================
# Canonical spendable wallet is wallet_balance.balance (not users.wallet_balance).
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
    """LEGACY / DISABLED. Do not use. Live path: services.referral_commission.

    Old 20% formula must not run. Historical rows are left unchanged.
    """
    logger.error(
        "LEGACY DISABLED: wallet_service.process_referral is not the live commission path"
    )
    return None


# =========================
# GET WALLET TRANSACTIONS
# =========================
def get_wallet_transactions(user_id):
    conn = get_db_connection()
    rows = conn.execute(text("""
        SELECT id, amount, type, source, status, created_at
        FROM wallet_transactions
        WHERE user_id = :uid
        ORDER BY id DESC
        LIMIT 50
    """), {"uid": user_id}).fetchall()

    items = []
    for r in rows:
        m = r._mapping
        created = m["created_at"]
        items.append({
            "id": m["id"],
            "amount": m["amount"],
            "type": m["type"],
            "source": m["source"],
            "status": m["status"],
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        })
    return items

# =========================
# ADD PENDING REFERRAL REWARD
# =========================
def add_pending_referral_reward(user_id, referral_transaction_id):
    """LEGACY / DISABLED. Old ₹2 pending reward. Not the live 10%/5% path."""
    logger.error(
        "LEGACY DISABLED: add_pending_referral_reward must not credit wallets"
    )
    return False