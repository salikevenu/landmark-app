from flask import Blueprint, request, jsonify, render_template
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text
from database.init_db import get_db_connection
from services.referral_commission import next_saturday_6pm_ist
from extensions import limiter
from services.wallet_service import (
    request_withdrawal,
)

wallet_bp = Blueprint("wallet", __name__)


def _safe_limit(limit_value):
    def decorator(f):
        if limiter:
            return limiter.limit(limit_value)(f)
        return f
    return decorator

@wallet_bp.route("/wallet")
def wallet_page():
    return render_template("users/wallet.html")

@wallet_bp.route("/api/wallet/transactions")
@jwt_required()
def wallet_transactions():
    user_id = get_jwt_identity()
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
        m = dict(r._mapping)
        created = m.get("created_at")
        if hasattr(created, "isoformat"):
            m["created_at"] = created.isoformat()
        items.append(m)
    return jsonify(items)

@wallet_bp.route("/api/wallet/overview")
@jwt_required()
def wallet_overview():
    user_id = get_jwt_identity()
    conn = get_db_connection()

    # Available balance
    wallet = conn.execute(
        text("SELECT balance FROM wallet_balance WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchone()
    available = wallet._mapping["balance"] if wallet else 0.0

    # Pending (locked) referral earnings
    pending = conn.execute(text("""
        SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions
        WHERE user_id = :uid AND status = 'locked'
          AND source IN ('activation_bonus','base_referral','referral_first_bonus','referral_recurring')
    """), {"uid": user_id}).scalar()

    next_payout = next_saturday_6pm_ist().strftime("%Y-%m-%d %H:%M IST") if next_saturday_6pm_ist else ""

    return jsonify({
        "available_balance": available,
        "pending_unlock": round(pending, 2),
        "next_payout_ist": next_payout
    })

@wallet_bp.route("/api/withdraw", methods=["POST"])
@_safe_limit("10 per minute")
@jwt_required()
def withdraw():
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Invalid request"}), 400
    user_id = get_jwt_identity()
    # Never trust client user_id / balance.
    idem = data.get("idempotency_key") or request.headers.get("Idempotency-Key")
    result = request_withdrawal(
        user_id,
        data.get("amount"),
        data.get("upi_id"),
        idempotency_key=idem,
    )
    http = result.pop("_http", None) if isinstance(result, dict) else 400
    if result.get("success"):
        return jsonify({
            "status": "Withdrawal request submitted",
            "message": result.get("message"),
            "withdrawal_id": result.get("withdrawal_id"),
            "new_balance": result.get("new_balance"),
            "duplicate": result.get("duplicate", False),
        }), 200
    return jsonify({"error": result.get("error", "Request failed")}), http or 400