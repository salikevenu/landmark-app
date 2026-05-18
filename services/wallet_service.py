from database.init_db import get_db

# =========================
# GET WALLET BALANCE
# =========================
def get_wallet_balance(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT balance FROM wallet_balance WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["balance"] if row else 0

# =========================
# CREDIT WALLET
# =========================
def credit_wallet(user_id, amount, source="system", reference_id=None):
    conn = get_db()
    # Ensure wallet row exists
    conn.execute("INSERT OR IGNORE INTO wallet_balance (user_id, balance) VALUES (?, 0)", (user_id,))
    # Add amount
    conn.execute(
        "UPDATE wallet_balance SET balance = balance + ? WHERE user_id = ?",
        (amount, user_id)
    )
    # Record transaction
    conn.execute("""
        INSERT INTO wallet_transactions
        (user_id, amount, type, source, reference_id, status)
        VALUES (?, ?, 'credit', ?, ?, 'completed')
    """, (user_id, amount, source, reference_id))
    conn.commit()


# =========================
# DEBIT WALLET
# =========================
def debit_wallet(user_id, amount, source="withdraw", reference_id=None):
    conn = get_db()
    row = conn.execute(
        "SELECT balance FROM wallet_balance WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row or row["balance"] < amount:
        return False

    conn.execute(
        "UPDATE wallet_balance SET balance = balance - ? WHERE user_id = ?",
        (amount, user_id)
    )
    conn.execute("""
        INSERT INTO wallet_transactions
        (user_id, amount, type, source, reference_id, status)
        VALUES (?, ?, 'debit', ?, ?, 'completed')
    """, (user_id, amount, source, reference_id))
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
    conn = get_db()
    # Get referrer's referral code stored in 'referred_by' column
    referred_row = conn.execute(
        "SELECT referred_by FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not referred_row or not referred_row["referred_by"]:
        return

    referral_code = referred_row["referred_by"]

    # Find the referrer user by their own referral_code
    referrer = conn.execute(
        "SELECT id FROM users WHERE referral_code = ?", (referral_code,)
    ).fetchone()
    if not referrer:
        return

    referrer_id = referrer["id"]
    reward = purchase_amount * 0.20

    # Credit the referrer's wallet
    credit_wallet(referrer_id, reward, "Referral reward", f"REF-{user_id}")


# =========================
# GET WALLET TRANSACTIONS
# =========================
def get_wallet_transactions(user_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT type, amount, description, created_at
        FROM wallet_transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
    """, (user_id,)).fetchall()

    return [
        {
            "type": r["type"],
            "amount": r["amount"],
            "description": r["description"],
            "date": r["created_at"]
        }
        for r in rows
    ]