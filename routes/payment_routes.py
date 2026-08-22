# routes/payment_routes.py
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from datetime import datetime
import hmac
import hashlib
import os
from sqlalchemy import text

from extensions import get_razorpay_client
from config.payment_config import (
    PLAN_PRICES,
    billed_term,
    get_plan_spec,
    get_razorpay_webhook_secret,
)
from services.payment_service import (
    verify_payment_service,
    ensure_payments_plan_column,
    finalize_paid_order,
)
from services.referral_commission import after_payment_finalized
from database.init_db import get_db_connection
import logging

logger = logging.getLogger(__name__)

payment_bp = Blueprint("payment", __name__)


def _json_from_result(result):
    http = result.pop("_http", None) if isinstance(result, dict) else None
    if not isinstance(result, dict):
        return jsonify({"success": False, "error": "Unexpected error"}), 500
    if result.get("success"):
        return jsonify(result), 200
    return jsonify(result), http or 400


def _payment_amount_paise(user_id, order_id, payment_id):
    conn = get_db_connection()
    try:
        row = conn.execute(text("""
            SELECT amount FROM payments
            WHERE user_id = :uid
              AND (order_id = :oid OR payment_id = :oid OR payment_id = :pid)
            ORDER BY id DESC
            LIMIT 1
        """), {
            "uid": int(user_id) if str(user_id).isdigit() else user_id,
            "oid": order_id or "",
            "pid": payment_id or "",
        }).fetchone()
        if row and row._mapping.get("amount") is not None:
            return int(row._mapping["amount"])
    except Exception:
        logger.exception("Could not load payment amount for referral commission")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return None


def _credit_referral_after_payment(result, user_id, amount_paise, razorpay_payment_id=None):
    """Process durable commission jobs after activation, including duplicate notices.

    Payment remains successful if commission processing fails; the outbox stays pending.
    """
    pid = razorpay_payment_id
    if isinstance(result, dict):
        pid = pid or result.get("razorpay_payment_id")
    after_payment_finalized(result, razorpay_payment_id=pid)


@payment_bp.route("/create-order-debug", methods=["POST"])
def create_order_debug():
    """Disabled: must not create orders without authentication."""
    return jsonify({
        "success": False,
        "error": "This endpoint is disabled",
    }), 404


@payment_bp.route("/create-order", methods=["POST"])
@jwt_required()
def create_order():
    try:
        user_id = get_jwt_identity()
        if not user_id:
            return jsonify({"success": False, "error": "Authentication required."}), 401

        data = request.get_json(silent=True) or {}
        plan = data.get("plan")
        if not plan:
            return jsonify({"success": False, "error": "Plan is required"}), 400

        display, spec = get_plan_spec(plan)
        if not spec:
            return jsonify({
                "success": False,
                "error": "Invalid plan",
                "allowed_plans": list(PLAN_PRICES.keys()),
            }), 400

        try:
            cycle, amount, duration_days = billed_term(
                spec["amount_paise"], data.get("billing_cycle")
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

        client = get_razorpay_client()
        if not client:
            return jsonify({"success": False, "error": "Razorpay client not initialized"}), 503

        order = client.order.create({
            "amount": amount,
            "currency": "INR",
            "payment_capture": 1,
            "notes": {
                "plan": spec["plan"],
                "plan_display": display,
                "user_id": str(user_id),
                "billing_cycle": cycle,
                "duration_days": str(duration_days),
            },
        })

        ensure_payments_plan_column()
        conn = get_db_connection()
        try:
            conn.execute(text("""
                INSERT INTO payments
                    (user_id, order_id, payment_id, amount, status, plan, created_at)
                VALUES
                    (:user_id, :order_id, :payment_id, :amount, :status, :plan, :created_at)
            """), {
                "user_id": int(user_id) if str(user_id).isdigit() else user_id,
                "order_id": order["id"],
                "payment_id": order["id"],
                "amount": amount,
                "status": "created",
                "plan": spec["plan"],
                "created_at": datetime.utcnow(),
            })
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return jsonify({
            "success": True,
            "order_id": order["id"],
            "key": os.getenv("RAZORPAY_KEY_ID"),
            "amount": amount,
            "currency": "INR",
            "plan": display,
            "billing_cycle": cycle,
            "user_id": user_id,
        }), 200

    except Exception:
        return jsonify({
            "success": False,
            "error": "Unable to create order",
        }), 500


@payment_bp.route("/wallet", methods=["GET"])
@jwt_required()
def wallet_balance():
    user_id = get_jwt_identity()
    conn = get_db_connection()
    row = conn.execute(
        text("SELECT balance FROM wallet_balance WHERE user_id = :uid"),
        {"uid": user_id},
    ).fetchone()
    balance = row._mapping["balance"] if row else 0
    return jsonify({"wallet_balance": balance})


@payment_bp.route("/wallet-transactions", methods=["GET"])
@jwt_required()
def wallet_transactions():
    user_id = get_jwt_identity()
    conn = get_db_connection()
    rows = conn.execute(text("""
        SELECT * FROM wallet_transactions
        WHERE user_id = :uid
        ORDER BY created_at DESC
    """), {"uid": user_id}).fetchall()
    return jsonify([dict(r._mapping) for r in rows])


@payment_bp.route("/verify-payment", methods=["POST"])
@jwt_required()
def verify_payment():
    user_id = get_jwt_identity()
    if not user_id:
        return jsonify({"success": False, "error": "Authentication required."}), 401
    data = request.get_json(silent=True) or {}
    result = verify_payment_service(data, user_id)
    amount_paise = _payment_amount_paise(
        user_id,
        data.get("razorpay_order_id"),
        data.get("razorpay_payment_id"),
    )
    _credit_referral_after_payment(result, user_id, amount_paise)
    return _json_from_result(result)


@payment_bp.route("/razorpay/webhook", methods=["POST"])
def razorpay_webhook():
    """HMAC-verified webhook. Activates only if a matching created order exists (idempotent)."""
    payload = request.get_data() or b""
    signature = request.headers.get("X-Razorpay-Signature") or ""

    webhook_secret = get_razorpay_webhook_secret()
    if not webhook_secret:
        return jsonify({"success": False, "error": "Webhook secret not configured"}), 503

    expected = hmac.new(
        webhook_secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    if not signature or not hmac.compare_digest(expected, signature):
        return jsonify({"success": False, "error": "Invalid webhook signature"}), 400

    data = request.get_json(silent=True) or {}
    try:
        entity = data["payload"]["payment"]["entity"]
        payment_id = entity["id"]
        order_id = entity.get("order_id")
        status = entity.get("status")
        amount = entity.get("amount")
        notes = entity.get("notes") or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid payload"}), 400

    if status != "captured" or not order_id:
        return jsonify({"success": True, "status": "ignored"}), 200

    ensure_payments_plan_column()
    conn = get_db_connection()
    try:
        row = conn.execute(text("""
            SELECT id, user_id, amount, plan, status, payment_id
            FROM payments
            WHERE order_id = :oid OR payment_id = :oid
            ORDER BY id DESC
            LIMIT 1
        """), {"oid": order_id}).fetchone()
        if not row:
            return jsonify({"success": True, "status": "order_not_found"}), 200
        m = dict(row._mapping)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    display, spec = get_plan_spec(m.get("plan") or notes.get("plan") or notes.get("plan_display"))
    if not spec:
        return jsonify({"success": True, "status": "unknown_plan"}), 200
    try:
        _, expected_paise, duration_days = billed_term(
            spec["amount_paise"], notes.get("billing_cycle")
        )
    except ValueError:
        _, expected_paise, duration_days = billed_term(spec["amount_paise"], "monthly")
    if notes.get("duration_days"):
        try:
            duration_days = int(notes["duration_days"])
        except (TypeError, ValueError):
            pass
    if amount is not None and int(amount) != expected_paise:
        return jsonify({"success": False, "error": "Amount mismatch"}), 400

    result = finalize_paid_order(
        order_id,
        payment_id,
        spec,
        expected_paise,
        user_id=m.get("user_id"),
        duration_days=duration_days,
    )
    if result.get("success"):
        _credit_referral_after_payment(result, m.get("user_id"), amount, razorpay_payment_id=payment_id)
        status_out = "already_processed" if result.get("duplicate") else "captured"
        return jsonify({"success": True, "status": status_out}), 200
    return jsonify({"success": False, "error": result.get("error", "Could not record payment")}), 400


@payment_bp.route("/submit-payment-proof", methods=["POST"])
@jwt_required()
def submit_payment_proof():
    return jsonify({
        "success": False,
        "error": "Manual payment proof is not enabled",
    }), 404
