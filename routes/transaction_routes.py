"""Transactions dashboard — page + API for wallet_transactions."""
import logging

from flask import Blueprint, jsonify, render_template, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text

from database.init_db import get_db_connection

transaction_bp = Blueprint("transactions", __name__, url_prefix="/transactions")
transactions_api_bp = Blueprint("transactions_api", __name__, url_prefix="/api/transactions")
logger = logging.getLogger(__name__)

PAGE_SIZE = 20


@transaction_bp.route("/")
@jwt_required()
def transactions_page():
    return render_template("transactions/index.html")


def _list_filters(user_id):
    tx_type = (request.args.get("type") or "").strip().lower()
    source = (request.args.get("source") or "").strip()
    search = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()

    clauses = ["user_id = :uid"]
    params = {"uid": user_id}

    if tx_type in ("credit", "debit", "lock"):
        clauses.append("type = :type")
        params["type"] = tx_type

    if source:
        clauses.append("source ILIKE :source")
        params["source"] = f"%{source}%"

    if status:
        clauses.append("status = :status")
        params["status"] = status

    if search:
        clauses.append(
            "(COALESCE(source, '') ILIKE :q OR COALESCE(type, '') ILIKE :q "
            "OR COALESCE(status, '') ILIKE :q OR COALESCE(reference_id, '') ILIKE :q)"
        )
        params["q"] = f"%{search}%"

    return " AND ".join(clauses), params


@transactions_api_bp.route("/list", methods=["GET"])
@jwt_required()
def list_transactions():
    """Paginated wallet_transactions for the current user."""
    user_id = get_jwt_identity()
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    offset = (page - 1) * PAGE_SIZE

    try:
        conn = get_db_connection()
        where_sql, params = _list_filters(user_id)
        params["limit"] = PAGE_SIZE
        params["offset"] = offset

        total = conn.execute(
            text(f"""
                SELECT COUNT(*)::int AS cnt
                FROM wallet_transactions
                WHERE {where_sql}
            """),
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        ).scalar() or 0

        rows = conn.execute(
            text(f"""
                SELECT
                    id,
                    amount,
                    type,
                    source,
                    reference_id,
                    status,
                    unlock_at,
                    created_at
                FROM wallet_transactions
                WHERE {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT :limit OFFSET :offset
            """),
            params,
        ).fetchall()

        transactions = []
        for row in rows:
            m = row._mapping
            created = m["created_at"]
            unlock_at = m["unlock_at"]
            transactions.append({
                "id": m["id"],
                "amount": float(m["amount"] or 0),
                "type": m["type"] or "",
                "source": m["source"] or "",
                "reference_id": m["reference_id"],
                "status": m["status"] or "",
                "unlock_at": unlock_at.isoformat() if unlock_at else None,
                "created_at": created.isoformat() if created else None,
            })

        return jsonify({
            "success": True,
            "transactions": transactions,
            "count": len(transactions),
            "page": page,
            "page_size": PAGE_SIZE,
            "total": int(total),
            "has_more": (page * PAGE_SIZE) < int(total),
        })
    except Exception as e:
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500


@transactions_api_bp.route("/stats", methods=["GET"])
@jwt_required()
def transaction_stats():
    """Total credits, debits, and locked/pending amounts for the current user."""
    user_id = get_jwt_identity()
    try:
        conn = get_db_connection()
        row = conn.execute(
            text("""
                SELECT
                    COALESCE(SUM(amount) FILTER (WHERE type = 'credit'), 0) AS total_credits,
                    COALESCE(SUM(amount) FILTER (WHERE type = 'debit'), 0) AS total_debits,
                    COALESCE(SUM(amount) FILTER (
                        WHERE status IN ('locked', 'pending')
                           OR type = 'lock'
                    ), 0) AS total_locked,
                    COUNT(*)::int AS total_count
                FROM wallet_transactions
                WHERE user_id = :uid
            """),
            {"uid": user_id},
        ).fetchone()

        m = row._mapping
        return jsonify({
            "success": True,
            "stats": {
                "total_credits": round(float(m["total_credits"] or 0), 2),
                "total_debits": round(float(m["total_debits"] or 0), 2),
                "total_locked": round(float(m["total_locked"] or 0), 2),
                "total_count": int(m["total_count"] or 0),
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
