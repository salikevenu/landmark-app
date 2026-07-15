from flask import Blueprint, request, jsonify
from datetime import datetime
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from sqlalchemy import text
from database.init_db import get_db

withdraw_bp = Blueprint("withdraw", __name__)


# ============================
# REQUEST WITHDRAWAL
# ============================
@withdraw_bp.route("/api/withdraw/request", methods=["POST"])
@jwt_required()
def request_withdraw():
    try:
        user_id = get_jwt_identity()
        data = request.json
        amount = data.get("amount")
        upi_id = data.get("upi_id")
        payment_method = "upi"

        if not amount or amount <= 0:
            return jsonify({"error": "Invalid withdrawal amount"}), 400
        if not upi_id:
            return jsonify({"error": "UPI ID required"}), 400

        conn = get_db()

        # Get wallet with policy flags
        wallet = conn.execute(
            text("SELECT balance, had_first_withdrawal, active_business_referrals_count FROM wallet_balance WHERE user_id = :uid"),
            {"uid": user_id}
        ).fetchone()

        if not wallet:
            return jsonify({"error": "Wallet not found"}), 404

        balance = float(wallet._mapping["balance"])
        had_first = wallet._mapping["had_first_withdrawal"]
        biz_refs = wallet._mapping["active_business_referrals_count"]

        # ========== FIRST WITHDRAWAL RULES ==========
        if not had_first:
            if balance < 500:
                return jsonify({"error": "First withdrawal requires minimum ₹500 balance"}), 400
            if biz_refs < 1:
                return jsonify({"error": "You must refer at least 1 paid business subscription to withdraw"}), 400

            # Check for pending referral rewards (fraud verification)
            pending = conn.execute(
                text("SELECT COUNT(*) FROM wallet_transactions WHERE user_id=:uid AND source='referral' AND status='pending'"),
                {"uid": user_id}
            ).scalar()
            if pending > 0:
                return jsonify({"error": "Your referral rewards are still under verification"}), 400
        else:
            # ========== AFTER FIRST WITHDRAWAL ==========
            if amount < 100:
                return jsonify({"error": "Minimum withdrawal amount is ₹100"}), 400

        # Check sufficient balance
        if balance < amount:
            return jsonify({"error": "Insufficient wallet balance"}), 400

        # Deduct from wallet and create withdraw request (atomic)
        conn.execute(
            text("UPDATE wallet_balance SET balance = balance - :amount, updated_at = NOW() WHERE user_id = :uid"),
            {"amount": amount, "uid": user_id}
        )

        conn.execute(text("""
            INSERT INTO withdraw_requests
            (user_id, amount, payment_method, upi_id, status, created_at)
            VALUES (:uid, :amount, :payment_method, :upi_id, 'pending', :created_at)
        """), {
            "uid": user_id,
            "amount": amount,
            "payment_method": payment_method,
            "upi_id": upi_id,
            "created_at": datetime.utcnow()
        })

        conn.commit()

        return jsonify({
            "message": "Withdrawal request submitted",
            "status": "pending",
            "new_balance": round(balance - amount, 2)
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================
# USER WITHDRAW HISTORY
# ============================
@withdraw_bp.route("/api/withdraw/history", methods=["GET"])
@jwt_required()
def withdraw_history():
    try:
        user_id = get_jwt_identity()
        conn = get_db()
        rows = conn.execute(text("""
            SELECT id, amount, status, payment_method, upi_id, created_at
            FROM withdraw_requests
            WHERE user_id = :uid
            ORDER BY created_at DESC
        """), {"uid": user_id}).fetchall()

        withdrawals = [{
            "id": row._mapping["id"],
            "amount": row._mapping["amount"],
            "status": row._mapping["status"],
            "payment_method": row._mapping["payment_method"],
            "upi_id": row._mapping["upi_id"],
            "created_at": row._mapping["created_at"]
        } for row in rows]
        return jsonify(withdrawals)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================
# ADMIN VIEW WITHDRAW REQUESTS
# ============================
@withdraw_bp.route("/api/admin/withdraw-requests", methods=["GET"])
@jwt_required()
def admin_withdraw_requests():
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    try:
        conn = get_db()
        rows = conn.execute(text("SELECT * FROM withdraw_requests ORDER BY created_at DESC")).fetchall()
        return jsonify([dict(row._mapping) for row in rows])

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================
# ADMIN APPROVE WITHDRAW
# ============================
@withdraw_bp.route("/api/admin/approve-withdraw/<int:withdraw_id>", methods=["POST"])
@jwt_required()
def approve_withdraw(withdraw_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    try:
        conn = get_db()
        req = conn.execute(
            text("SELECT * FROM withdraw_requests WHERE id = :wid"),
            {"wid": withdraw_id}
        ).fetchone()
        if not req:
            return jsonify({"error": "Withdraw request not found"}), 404
        if req._mapping["status"] != "pending":
            return jsonify({"error": "Already processed"}), 400

        user_id = req._mapping["user_id"]

        # Mark as approved
        conn.execute(
            text("UPDATE withdraw_requests SET status = 'approved', processed_at = NOW() WHERE id = :wid"),
            {"wid": withdraw_id}
        )

        # Set first withdrawal flag if not already set
        conn.execute(
            text("UPDATE wallet_balance SET had_first_withdrawal = TRUE WHERE user_id = :uid AND had_first_withdrawal = FALSE"),
            {"uid": user_id}
        )

        conn.commit()
        return jsonify({"message": "Withdrawal approved"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
# ============================
# ADMIN REJECT WITHDRAW
# ============================
@withdraw_bp.route("/api/admin/reject-withdraw/<int:withdraw_id>", methods=["POST"])
@jwt_required()
def reject_withdraw(withdraw_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    try:
        conn = get_db()
        conn.execute(text("""
            UPDATE withdraw_requests
            SET status = 'rejected'
            WHERE id = :wid AND status = 'pending'
        """), {"wid": withdraw_id})
        conn.commit()

        return jsonify({"message": "Withdrawal rejected"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500