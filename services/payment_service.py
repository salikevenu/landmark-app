# services/payment_service.py
"""Canonical Razorpay checkout verification and subscription activation."""

from datetime import datetime, timedelta
from sqlalchemy import text

from config.payment_config import get_plan_spec
from database.init_db import get_db_connection
from extensions import get_razorpay_client

# Payment row source of truth:
#   created    — Razorpay order stored, not verified
#   captured   — legacy: Razorpay verified, activation may be incomplete
#   activated  — subscription applied exactly once
ACTIVATED_STATUS = "activated"
NEEDS_ACTIVATION = ("created", "captured", "verified", "paid", "processing")


def _as_int_user_id(user_id):
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def _row_map(row):
    if row is None:
        return None
    return dict(row._mapping)


def _expiry_date(duration_days):
    return (datetime.utcnow() + timedelta(days=duration_days)).strftime("%Y-%m-%d")


def ensure_payments_plan_column():
    """Smallest schema add: payments.plan (display or internal key)."""
    conn = get_db_connection()
    try:
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS plan TEXT"))
        conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS order_id TEXT"))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def success_payload(message, spec, expiry, extra=None):
    body = {
        "success": True,
        "status": "success",
        "message": message,
        "plan": spec["plan"],
        "role": spec["role"],
        "expiry": expiry,
        "redirect": "/dashboard",
    }
    if extra:
        body.update(extra)
    return body


def error_payload(message, http_hint=400):
    return {
        "success": False,
        "status": "error",
        "error": message,
        "_http": http_hint,
    }


def activate_subscription(phone, plan, days=None):
    """Admin-compatible activator: lookup by phone, set plan+role+expiry+limit.

    `plan` may be a display name ('Business Basic') or internal key ('business_basic').
    Does not write subscription_status (column is not in the canonical users schema).
    """
    display, spec = get_plan_spec(plan)
    if not spec:
        raise ValueError("Unknown plan")
    duration = spec["duration_days"] if days is None else int(days)
    expiry_date = _expiry_date(duration)
    conn = get_db_connection()
    try:
        conn.execute(text("""
            UPDATE users
            SET role = :role,
                plan = :plan,
                subscription_expiry = :expiry_date,
                business_limit = :blimit
            WHERE phone = :phone
        """), {
            "role": spec["role"],
            "plan": spec["plan"],
            "expiry_date": expiry_date,
            "blimit": spec["business_limit"],
            "phone": phone,
        })
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return expiry_date


def _activate_user_on_conn(conn, user_id, spec, duration_days=None):
    """Write plan/role/expiry on the given connection. Caller commits/rolls back."""
    duration = spec["duration_days"] if duration_days is None else int(duration_days)
    expiry_date = _expiry_date(duration)
    uid = _as_int_user_id(user_id)
    conn.execute(text("""
        UPDATE users
        SET role = :role,
            plan = :plan,
            subscription_expiry = :expiry_date,
            business_limit = :blimit
        WHERE id = :uid
    """), {
        "role": spec["role"],
        "plan": spec["plan"],
        "expiry_date": expiry_date,
        "blimit": spec["business_limit"],
        "uid": uid,
    })
    return expiry_date


def activate_subscription_for_user(user_id, spec, duration_days=None):
    """Standalone activator. Prefer verify_payment_service which is transactional."""
    conn = get_db_connection()
    try:
        expiry = _activate_user_on_conn(conn, user_id, spec, duration_days)
        conn.commit()
        return expiry
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_order_row_for_update(conn, order_id, payment_id, user_id):
    uid = _as_int_user_id(user_id)
    if uid is not None:
        row = conn.execute(text("""
            SELECT id, user_id, order_id, payment_id, amount, status, plan
            FROM payments
            WHERE user_id = :uid
              AND (
                    order_id = :oid
                    OR payment_id = :oid
                    OR payment_id = :pid
              )
            ORDER BY id DESC
            LIMIT 1
            FOR UPDATE
        """), {"uid": uid, "oid": order_id, "pid": payment_id}).fetchone()
    else:
        row = conn.execute(text("""
            SELECT id, user_id, order_id, payment_id, amount, status, plan
            FROM payments
            WHERE order_id = :oid
               OR payment_id = :oid
               OR payment_id = :pid
            ORDER BY id DESC
            LIMIT 1
            FOR UPDATE
        """), {"oid": order_id, "pid": payment_id}).fetchone()
    return _row_map(row)


def _mark_activated(conn, row_id, razorpay_order_id, razorpay_payment_id, amount_paise, plan_key):
    conn.execute(text("""
        UPDATE payments
        SET payment_id = :pid,
            order_id = :oid,
            amount = :amount,
            status = :status,
            plan = :plan
        WHERE id = :id
    """), {
        "pid": razorpay_payment_id,
        "oid": razorpay_order_id,
        "amount": amount_paise,
        "status": ACTIVATED_STATUS,
        "plan": plan_key,
        "id": row_id,
    })


def _read_user_expiry_on_conn(conn, user_id):
    uid = _as_int_user_id(user_id)
    row = conn.execute(text("""
        SELECT plan, role, subscription_expiry FROM users WHERE id = :uid
    """), {"uid": uid}).fetchone()
    mapped = _row_map(row) or {}
    return mapped.get("subscription_expiry") or ""


def finalize_paid_order(razorpay_order_id, razorpay_payment_id, spec, expected_paise, user_id=None):
    """Lock the payment row, activate at most once, commit atomically.

    Payment row is the source of truth:
      created/captured/verified/paid/processing → activate user + status=activated
      activated → return current expiry, do not extend
    """
    uid = _as_int_user_id(user_id) if user_id is not None else None
    conn = get_db_connection()
    try:
        row = _load_order_row_for_update(conn, razorpay_order_id, razorpay_payment_id, uid)
        if not row:
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Order not found for this account")

        _, locked_spec = get_plan_spec(row.get("plan"))
        if locked_spec:
            spec = locked_spec
            expected_paise = spec["amount_paise"]

        stored_amount = row.get("amount")
        if stored_amount is not None:
            sa = int(stored_amount)
            if sa != expected_paise and sa != int(expected_paise / 100):
                try:
                    conn.rollback()
                except Exception:
                    pass
                return error_payload("Amount mismatch")

        owner_id = row.get("user_id")
        status = (row.get("status") or "").lower()
        if status == ACTIVATED_STATUS:
            expiry = _read_user_expiry_on_conn(conn, owner_id)
            conn.commit()
            return success_payload(
                "Payment already processed",
                spec,
                expiry,
                extra={"duplicate": True},
            )

        expiry = _activate_user_on_conn(conn, owner_id, spec)
        _mark_activated(
            conn,
            row["id"],
            razorpay_order_id,
            razorpay_payment_id,
            expected_paise,
            spec["plan"],
        )
        conn.commit()
        return success_payload("Subscription activated", spec, expiry)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return error_payload("Could not record payment")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def verify_payment_service(data, user_id):
    """Canonical verifier used by POST /api/payment/verify-payment.

    Trusts: Razorpay signature, Razorpay order amount, server-side payments row / order notes.
    Does not trust frontend plan, amount, role, or expiry.
    """
    uid = _as_int_user_id(user_id)
    if uid is None:
        return error_payload("Authentication required", 401)

    data = data or {}
    razorpay_order_id = data.get("razorpay_order_id")
    razorpay_payment_id = data.get("razorpay_payment_id")
    razorpay_signature = data.get("razorpay_signature")

    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature]):
        return error_payload("Missing required fields")

    client = get_razorpay_client()
    if not client:
        return error_payload("Payment provider unavailable", 503)

    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature,
        })
    except Exception:
        return error_payload("Payment signature verification failed")

    try:
        rzp_order = client.order.fetch(razorpay_order_id)
    except Exception:
        return error_payload("Unable to fetch order")

    if rzp_order.get("status") != "paid":
        return error_payload("Order not paid")

    notes = rzp_order.get("notes") or {}
    note_uid = notes.get("user_id")
    if note_uid and str(note_uid) != str(uid):
        return error_payload("Order does not belong to this account")

    ensure_payments_plan_column()
    conn = get_db_connection()
    try:
        preview = conn.execute(text("""
            SELECT id, user_id, order_id, payment_id, amount, status, plan
            FROM payments
            WHERE user_id = :uid
              AND (order_id = :oid OR payment_id = :oid OR payment_id = :pid)
            ORDER BY id DESC
            LIMIT 1
        """), {"uid": uid, "oid": razorpay_order_id, "pid": razorpay_payment_id}).fetchone()
        row = _row_map(preview)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not row:
        return error_payload("Order not found for this account")

    plan_key = row.get("plan") or notes.get("plan") or notes.get("plan_display")
    display, spec = get_plan_spec(plan_key)
    if not spec:
        return error_payload("Unknown plan on order")

    expected_paise = spec["amount_paise"]
    if int(rzp_order.get("amount") or 0) != expected_paise:
        return error_payload("Amount mismatch")

    return finalize_paid_order(
        razorpay_order_id,
        razorpay_payment_id,
        spec,
        expected_paise,
        user_id=uid,
    )


def process_payment(user_id, payment_id, amount_in_rupees):
    """Legacy helper kept for admin/old callers. Does not activate subscriptions."""
    conn = get_db_connection()
    try:
        existing = conn.execute(
            text("SELECT id FROM payments WHERE payment_id = :payment_id"),
            {"payment_id": payment_id},
        ).fetchone()
        if existing:
            return {"status": "duplicate", "success": True}
        conn.execute(text("""
            INSERT INTO payments (user_id, payment_id, amount, status, created_at)
            VALUES (:user_id, :payment_id, :amount, :status, :created_at)
        """), {
            "user_id": user_id,
            "payment_id": payment_id,
            "amount": amount_in_rupees,
            "status": "verified",
            "created_at": datetime.utcnow(),
        })
        conn.commit()
        return {"status": "success", "success": True}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"error": str(e), "success": False}
    finally:
        try:
            conn.close()
        except Exception:
            pass
