from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt

from services.wallet_service import (
    request_withdrawal,
    approve_withdrawal,
    reject_withdrawal,
)

withdraw_bp = Blueprint("withdraw", __name__)


def _json_result(result):
    http = result.pop("_http", None) if isinstance(result, dict) else None
    if result.get("success"):
        return jsonify(result), 200
    return jsonify({"error": result.get("error", "Request failed")}), http or 400


@withdraw_bp.route("/api/withdraw/request", methods=["POST"])
@jwt_required()
def request_withdraw():
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    idem = data.get("idempotency_key") or request.headers.get("Idempotency-Key")
    result = request_withdrawal(
        user_id,
        data.get("amount"),
        data.get("upi_id"),
        idempotency_key=idem,
    )
    return _json_result(result)


@withdraw_bp.route("/api/withdraw/history", methods=["GET"])
@jwt_required()
def withdraw_history():
    from sqlalchemy import text
    from database.init_db import get_db_connection

    user_id = get_jwt_identity()
    conn = get_db_connection()
    try:
        rows = conn.execute(text("""
            SELECT id, amount, status, payment_method, upi_id, created_at
            FROM withdraw_requests
            WHERE user_id = :uid
            ORDER BY created_at DESC
        """), {"uid": int(user_id) if str(user_id).isdigit() else user_id}).fetchall()
        withdrawals = [{
            "id": row._mapping["id"],
            "amount": row._mapping["amount"],
            "status": row._mapping["status"],
            "payment_method": row._mapping["payment_method"],
            "upi_id": row._mapping["upi_id"],
            "created_at": row._mapping["created_at"],
        } for row in rows]
        return jsonify(withdrawals)
    finally:
        try:
            conn.close()
        except Exception:
            pass


@withdraw_bp.route("/api/admin/withdraw-requests", methods=["GET"])
@jwt_required()
def admin_withdraw_requests():
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    from sqlalchemy import text
    from database.init_db import get_db_connection
    conn = get_db_connection()
    try:
        rows = conn.execute(text(
            "SELECT * FROM withdraw_requests ORDER BY created_at DESC"
        )).fetchall()
        return jsonify([dict(row._mapping) for row in rows])
    finally:
        try:
            conn.close()
        except Exception:
            pass


@withdraw_bp.route("/api/admin/approve-withdraw/<int:withdraw_id>", methods=["POST"])
@jwt_required()
def approve_withdraw(withdraw_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    result = approve_withdrawal(withdraw_id)
    if result.get("success"):
        return jsonify({"message": "Withdrawal approved", "status": "approved"})
    return jsonify({"error": result.get("error")}), result.get("_http") or 400


@withdraw_bp.route("/api/admin/reject-withdraw/<int:withdraw_id>", methods=["POST"])
@jwt_required()
def reject_withdraw(withdraw_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    result = reject_withdrawal(withdraw_id)
    if result.get("success"):
        return jsonify({"message": "Withdrawal rejected", "status": "rejected"})
    return jsonify({"error": result.get("error")}), result.get("_http") or 400
