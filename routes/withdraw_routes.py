from flask import Blueprint, request, jsonify
from datetime import datetime
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from database.init_db import get_db          # use the canonical get_db

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

        # Check wallet balance
        wallet = conn.execute(
            "SELECT balance FROM wallet_balance WHERE user_id = ?", (user_id,)
        ).fetchone()

        if not wallet:
            return jsonify({"error": "Wallet not found"}), 404
        if wallet["balance"] < amount:
            return jsonify({"error": "Insufficient wallet balance"}), 400

        # Create withdraw request
        conn.execute("""
            INSERT INTO withdraw_requests
            (user_id, amount, payment_method, upi_id, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (user_id, amount, payment_method, upi_id, datetime.utcnow()))
        conn.commit()

        return jsonify({"message": "Withdrawal request submitted", "status": "pending"}), 200

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
        rows = conn.execute("""
            SELECT id, amount, status, payment_method, upi_id, created_at
            FROM withdraw_requests
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()

        withdrawals = [{
            "id": row["id"],
            "amount": row["amount"],
            "status": row["status"],
            "payment_method": row["payment_method"],
            "upi_id": row["upi_id"],
            "created_at": row["created_at"]
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
        rows = conn.execute("SELECT * FROM withdraw_requests ORDER BY created_at DESC").fetchall()
        return jsonify([dict(row) for row in rows])

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
        req = conn.execute("SELECT * FROM withdraw_requests WHERE id = ?", (withdraw_id,)).fetchone()
        if not req:
            return jsonify({"error": "Withdraw request not found"}), 404
        if req["status"] != "pending":
            return jsonify({"error": "Already processed"}), 400

        user_id = req["user_id"]
        amount = req["amount"]

        # Check current balance again (avoid race condition)
        wallet = conn.execute("SELECT balance FROM wallet_balance WHERE user_id = ?", (user_id,)).fetchone()
        if not wallet or wallet["balance"] < amount:
            return jsonify({"error": "Insufficient balance now"}), 400

        # Deduct wallet balance and update withdraw status in one transaction
        conn.execute("UPDATE wallet_balance SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
        conn.execute("UPDATE withdraw_requests SET status = 'approved' WHERE id = ?", (withdraw_id,))
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
        conn.execute("""
            UPDATE withdraw_requests
            SET status = 'rejected'
            WHERE id = ? AND status = 'pending'
        """, (withdraw_id,))
        conn.commit()

        return jsonify({"message": "Withdrawal rejected"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500