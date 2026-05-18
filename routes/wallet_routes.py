from flask import Blueprint, request, jsonify, render_template
from flask_jwt_extended import jwt_required, get_jwt_identity
from database.init_db import get_db                        # <-- shared helper
from services.referral_commission import next_saturday_6pm_ist
from services.wallet_service import (
    get_wallet_transactions,
    get_wallet_balance,
    debit_wallet
)

wallet_bp = Blueprint("wallet", __name__)

@wallet_bp.route("/wallet")
def wallet_page():
    # Public shell, frontend fetches data via API
    return render_template("users/wallet.html")

@wallet_bp.route("/api/wallet/transactions")
@jwt_required()
def wallet_transactions():
    user_id = get_jwt_identity()
    data = get_wallet_transactions(user_id)
    return jsonify(data)

@wallet_bp.route("/api/wallet/overview")
@jwt_required()
def wallet_overview():
    user_id = get_jwt_identity()
    conn = get_db()

    # Available balance from wallet_balance table
    wallet = conn.execute("SELECT balance FROM wallet_balance WHERE user_id = ?",
                          (user_id,)).fetchone()
    available = wallet["balance"] if wallet else 0.0

    # Pending (locked) referral earnings
    pending = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions
        WHERE user_id = ? AND status = 'locked'
          AND (source IN ('activation_bonus','base_referral','referral_first_bonus','referral_recurring'))
    """, (user_id,)).fetchone()[0]

    next_payout = next_saturday_6pm_ist().strftime("%Y-%m-%d %H:%M IST") if next_saturday_6pm_ist else ""

    return jsonify({
        "available_balance": available,
        "pending_unlock": round(pending, 2),
        "next_payout_ist": next_payout
    })
    
@wallet_bp.route("/api/withdraw", methods=["POST"])
@jwt_required()
def withdraw():
    data = request.json
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    amount = data.get("amount")
    upi_id = data.get("upi_id")
    if not amount or not upi_id:
        return jsonify({"error": "Amount and UPI ID required"}), 400

    try:
        amount = float(amount)
    except ValueError:
        return jsonify({"error": "Invalid amount"}), 400

    if amount < 500:
        return jsonify({"error": "Minimum withdraw ₹500"}), 400

    user_id = get_jwt_identity()

    # Check balance (via service)
    current_balance = get_wallet_balance(user_id)
    if current_balance < amount:
        return jsonify({"error": "Insufficient balance"}), 400

    # Debit wallet (via service)
    success = debit_wallet(user_id, amount, source="withdraw_request", reference_id=None)
    if not success:
        return jsonify({"error": "Failed to debit wallet"}), 500

    # Create withdraw request record using shared connection
    conn = get_db()
    conn.execute("""
        INSERT INTO withdraw_requests (user_id, amount, upi_id, status, created_at)
        VALUES (?, ?, ?, 'pending', datetime('now'))
    """, (user_id, amount, upi_id))
    conn.commit()

    return jsonify({"status": "Withdrawal request submitted"}), 200