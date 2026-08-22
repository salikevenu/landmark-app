# services/payment_service.py
"""Canonical Razorpay checkout verification and subscription activation."""

from datetime import datetime, timedelta
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from config.payment_config import (
    billed_term,
    duration_days_for_stored_amount,
    get_plan_spec,
    is_extra_business_plan,
    EXTRA_BUSINESS_AMOUNT_PAISE,
    EXTRA_BUSINESS_PLAN,
)
from database.init_db import get_db_connection
from extensions import get_razorpay_client
from services.referral_commission import enqueue_referral_commission_job, ensure_referral_commission_schema

# Payment row source of truth (do not move backwards from activated):
#   created     — Razorpay order stored, not verified
#   captured    — Razorpay captured; activation may still be incomplete (legacy)
#   verified    — legacy admin/manual rows
#   paid        — legacy
#   processing  — in-flight
#   failed      — Razorpay failed/cancelled; not activatable unless a later captured recovery
#   cancelled   — abandoned
#   activated   — subscription or extra-slot applied exactly once (terminal success)
ACTIVATED_STATUS = "activated"
NEEDS_ACTIVATION = ("created", "captured", "verified", "paid", "processing")
FAILED_STATES = ("failed", "cancelled")
TERMINAL_SUCCESS = (ACTIVATED_STATUS,)


def _as_int_user_id(user_id):
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def _row_map(row):
    if row is None:
        return None
    mapping = getattr(row, "_mapping", None)
    if mapping is None:
        return None
    try:
        return dict(mapping)
    except Exception:
        return None


def _expiry_date(duration_days):
    # Product behavior: expiry is now (UTC) + billed duration, not "extend from
    # current expiry". Replay of the same order does not call this again.
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
    Does not enqueue referral commission (admin path is excluded by product rule).
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


def _result_rowcount(result):
    rc = getattr(result, "rowcount", None)
    if rc is None:
        return 1
    try:
        return int(rc)
    except (TypeError, ValueError):
        return 1


def _mark_activated(conn, row_id, razorpay_order_id, razorpay_payment_id, amount_paise, plan_key):
    result = conn.execute(text("""
        UPDATE payments
        SET payment_id = :pid,
            order_id = :oid,
            amount = :amount,
            status = :status,
            plan = :plan
        WHERE id = :id
          AND lower(coalesce(status, '')) <> :activated
    """), {
        "pid": razorpay_payment_id,
        "oid": razorpay_order_id,
        "amount": amount_paise,
        "status": ACTIVATED_STATUS,
        "plan": plan_key,
        "id": row_id,
        "activated": ACTIVATED_STATUS,
    })
    return _result_rowcount(result)


def _read_user_expiry_on_conn(conn, user_id):
    uid = _as_int_user_id(user_id)
    row = conn.execute(text("""
        SELECT plan, role, subscription_expiry FROM users WHERE id = :uid
    """), {"uid": uid}).fetchone()
    mapped = _row_map(row) or {}
    return mapped.get("subscription_expiry") or ""


def _plan_and_duration_from_row(row, expected_paise=None):
    """Server-side plan + duration from the locked payments row (not request notes)."""
    if is_extra_business_plan(row.get("plan")):
        return None, None, None
    _, spec = get_plan_spec(row.get("plan"))
    if not spec:
        return None, None, None
    stored = row.get("amount")
    if stored is None:
        stored = expected_paise
    cycle, days = duration_days_for_stored_amount(spec["amount_paise"], stored)
    if days is None and expected_paise is not None:
        cycle, days = duration_days_for_stored_amount(spec["amount_paise"], expected_paise)
    return spec, cycle, days


def _row_order_matches(row, razorpay_order_id, razorpay_payment_id):
    row_oid = row.get("order_id")
    row_pid = row.get("payment_id")
    if row_oid:
        if str(row_oid) == str(razorpay_order_id):
            return True
        # Pre-activation placeholder: payment_id still holds the Razorpay order id.
        if str(row_pid) == str(razorpay_order_id):
            return True
        return False
    if row_pid and str(row_pid) not in (str(razorpay_order_id), str(razorpay_payment_id or "")):
        return False
    return True


def finalize_paid_order(razorpay_order_id, razorpay_payment_id, spec, expected_paise,
                        user_id=None, duration_days=None, allow_failed_recovery=False):
    """Lock the payment row, activate at most once, commit atomically.

    Payment row is the source of truth:
      created/captured/verified/paid/processing → activate user + status=activated
      failed/cancelled → activate only when allow_failed_recovery (later captured evidence)
      activated → return current expiry, do not extend, do not regress status
    """
    uid = _as_int_user_id(user_id) if user_id is not None else None
    ensure_referral_commission_schema()
    conn = get_db_connection()
    try:
        row = _load_order_row_for_update(conn, razorpay_order_id, razorpay_payment_id, uid)
        if not row:
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Order not found for this account")

        if not _row_order_matches(row, razorpay_order_id, razorpay_payment_id):
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Order does not match this payment")

        if is_extra_business_plan(row.get("plan")):
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Use extra-business verification for this order")

        locked_spec, _cycle, stored_days = _plan_and_duration_from_row(row, expected_paise)
        if locked_spec:
            spec = locked_spec
        if stored_days is not None:
            duration_days = stored_days

        stored_amount = row.get("amount")
        if stored_amount is not None:
            sa = int(stored_amount)
            if sa != int(expected_paise):
                try:
                    conn.rollback()
                except Exception:
                    pass
                return error_payload("Amount mismatch")

        owner_id = row.get("user_id")
        if uid is not None and owner_id is not None and int(owner_id) != int(uid):
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Order does not belong to this account")

        status = (row.get("status") or "").lower()
        extra = {
            "razorpay_payment_id": razorpay_payment_id,
            "referred_user_id": owner_id,
            "amount_paise": expected_paise,
        }
        if status == ACTIVATED_STATUS:
            enqueue_referral_commission_job(
                conn,
                payment_id=razorpay_payment_id,
                razorpay_payment_id=razorpay_payment_id,
                referred_user_id=owner_id,
                amount_rupees=float(expected_paise) / 100.0,
            )
            expiry = _read_user_expiry_on_conn(conn, owner_id)
            conn.commit()
            extra["duplicate"] = True
            return success_payload(
                "Payment already processed",
                spec,
                expiry,
                extra=extra,
            )

        recoverable = status in NEEDS_ACTIVATION or (
            allow_failed_recovery and status in FAILED_STATES
        )
        if not recoverable:
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Payment is not in an activatable state")

        amount_rupees = float(expected_paise) / 100.0
        enqueue_referral_commission_job(
            conn,
            payment_id=razorpay_payment_id,
            razorpay_payment_id=razorpay_payment_id,
            referred_user_id=owner_id,
            amount_rupees=amount_rupees,
        )
        expiry = _activate_user_on_conn(conn, owner_id, spec, duration_days)
        updated = _mark_activated(
            conn,
            row["id"],
            razorpay_order_id,
            razorpay_payment_id,
            expected_paise,
            spec["plan"],
        )
        if updated < 1:
            conn.rollback()
            row2 = _load_order_row_for_update(conn, razorpay_order_id, razorpay_payment_id, owner_id)
            enqueue_referral_commission_job(
                conn,
                payment_id=razorpay_payment_id,
                razorpay_payment_id=razorpay_payment_id,
                referred_user_id=owner_id,
                amount_rupees=amount_rupees,
            )
            expiry = _read_user_expiry_on_conn(conn, owner_id)
            conn.commit()
            extra["duplicate"] = True
            return success_payload(
                "Payment already processed",
                spec,
                expiry,
                extra=extra,
            )
        conn.commit()
        return success_payload("Subscription activated", spec, expiry, extra=extra)
    except IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        return error_payload("Duplicate payment identity", 409)
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


def _fetch_captured_payment(client, razorpay_order_id, razorpay_payment_id):
    """Razorpay payment entity is authoritative for capture/order/amount."""
    try:
        payment = client.payment.fetch(razorpay_payment_id)
    except Exception:
        return None, error_payload("Unable to fetch payment")
    if not isinstance(payment, dict):
        return None, error_payload("Unable to fetch payment")
    if payment.get("status") != "captured":
        return None, error_payload("Payment not captured")
    if str(payment.get("order_id") or "") != str(razorpay_order_id):
        return None, error_payload("Payment does not belong to this order")
    return payment, None


def verify_payment_service(data, user_id):
    """Canonical verifier used by POST /api/payment/verify-payment.

    Trusts: Razorpay signature, Razorpay payment/order fetch, server-side payments row.
    Does not trust frontend plan, amount, role, expiry, user_id, or payment status.
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

    payment, pay_err = _fetch_captured_payment(client, razorpay_order_id, razorpay_payment_id)
    if pay_err:
        return pay_err

    notes = rzp_order.get("notes") or {}
    pay_notes = payment.get("notes") or {}
    note_uid = notes.get("user_id") or pay_notes.get("user_id")
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

    if is_extra_business_plan(row.get("plan")):
        return verify_extra_business_payment(data, user_id)

    plan_key = row.get("plan") or notes.get("plan") or notes.get("plan_display")
    display, spec = get_plan_spec(plan_key)
    if not spec:
        return error_payload("Unknown plan on order")

    stored = row.get("amount")
    cycle, duration_days = duration_days_for_stored_amount(spec["amount_paise"], stored)
    if duration_days is None:
        return error_payload("Amount mismatch")
    expected_paise = int(stored)
    rzp_amount = int(rzp_order.get("amount") or 0)
    pay_amount = int(payment.get("amount") or 0)
    if rzp_amount != expected_paise or pay_amount != expected_paise:
        return error_payload("Amount mismatch")

    return finalize_paid_order(
        razorpay_order_id,
        razorpay_payment_id,
        spec,
        expected_paise,
        user_id=uid,
        duration_days=duration_days,
        allow_failed_recovery=True,
    )


def _increment_extra_slot(conn, user_id):
    uid = _as_int_user_id(user_id)
    conn.execute(text("""
        UPDATE users
        SET extra_businesses_purchased = COALESCE(extra_businesses_purchased, 0) + 1
        WHERE id = :uid
    """), {"uid": uid})


def finalize_extra_business_order(razorpay_order_id, razorpay_payment_id, expected_paise,
                                  user_id=None, allow_failed_recovery=False):
    """Grant one extra listing slot. Never activates a subscription or commission."""
    uid = _as_int_user_id(user_id)
    conn = get_db_connection()
    try:
        row = _load_order_row_for_update(conn, razorpay_order_id, razorpay_payment_id, uid)
        if not row:
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Order not found for this account")
        if not _row_order_matches(row, razorpay_order_id, razorpay_payment_id):
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Order does not match this payment")
        if not is_extra_business_plan(row.get("plan")):
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Not an extra-business order")
        stored = row.get("amount")
        if stored is not None and int(stored) != int(expected_paise):
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Amount mismatch")
        owner_id = row.get("user_id")
        if uid is not None and owner_id is not None and int(owner_id) != int(uid):
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Order does not belong to this account")
        status = (row.get("status") or "").lower()
        extra = {
            "razorpay_payment_id": razorpay_payment_id,
            "plan": EXTRA_BUSINESS_PLAN,
            "redirect": "/create-listing",
        }
        if status == ACTIVATED_STATUS:
            conn.commit()
            extra["duplicate"] = True
            extra["success"] = True
            extra["status"] = "success"
            extra["message"] = "Extra business slot already purchased"
            extra["error"] = None
            return {
                "success": True,
                "status": "success",
                "message": "Extra business slot already purchased",
                "plan": EXTRA_BUSINESS_PLAN,
                "role": None,
                "expiry": "",
                "redirect": "/create-listing",
                "duplicate": True,
                "razorpay_payment_id": razorpay_payment_id,
            }
        recoverable = status in NEEDS_ACTIVATION or (
            allow_failed_recovery and status in FAILED_STATES
        )
        if not recoverable:
            try:
                conn.rollback()
            except Exception:
                pass
            return error_payload("Payment is not in an activatable state")
        _increment_extra_slot(conn, owner_id)
        updated = _mark_activated(
            conn,
            row["id"],
            razorpay_order_id,
            razorpay_payment_id,
            expected_paise,
            EXTRA_BUSINESS_PLAN,
        )
        if updated < 1:
            conn.rollback()
            return {
                "success": True,
                "status": "success",
                "message": "Extra business slot already purchased",
                "plan": EXTRA_BUSINESS_PLAN,
                "role": None,
                "expiry": "",
                "redirect": "/create-listing",
                "duplicate": True,
                "razorpay_payment_id": razorpay_payment_id,
            }
        conn.commit()
        return {
            "success": True,
            "status": "success",
            "message": "Extra business slot purchased successfully",
            "plan": EXTRA_BUSINESS_PLAN,
            "role": None,
            "expiry": "",
            "redirect": "/create-listing",
            "razorpay_payment_id": razorpay_payment_id,
        }
    except IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        return error_payload("Duplicate payment identity", 409)
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


def verify_extra_business_payment(data, user_id):
    """Verify a captured extra-business slot purchase. No referral commission."""
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
    payment, pay_err = _fetch_captured_payment(client, razorpay_order_id, razorpay_payment_id)
    if pay_err:
        return pay_err
    notes = rzp_order.get("notes") or {}
    note_uid = notes.get("user_id") or (payment.get("notes") or {}).get("user_id")
    if note_uid and str(note_uid) != str(uid):
        return error_payload("Order does not belong to this account")
    expected_paise = EXTRA_BUSINESS_AMOUNT_PAISE
    if int(rzp_order.get("amount") or 0) != expected_paise:
        return error_payload("Amount mismatch")
    if int(payment.get("amount") or 0) != expected_paise:
        return error_payload("Amount mismatch")

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
        if not row:
            try:
                conn.execute(text("""
                    INSERT INTO payments
                        (user_id, order_id, payment_id, amount, status, plan, created_at)
                    VALUES
                        (:user_id, :order_id, :payment_id, :amount, :status, :plan, :created_at)
                """), {
                    "user_id": uid,
                    "order_id": razorpay_order_id,
                    "payment_id": razorpay_order_id,
                    "amount": expected_paise,
                    "status": "captured",
                    "plan": EXTRA_BUSINESS_PLAN,
                    "created_at": datetime.utcnow(),
                })
                conn.commit()
            except IntegrityError:
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return finalize_extra_business_order(
        razorpay_order_id,
        razorpay_payment_id,
        expected_paise,
        user_id=uid,
        allow_failed_recovery=True,
    )


def mark_payment_failed(order_id, payment_id=None, status="failed"):
    """Record a failed/cancelled Razorpay event. Never regresses activated rows."""
    if status not in FAILED_STATES:
        status = "failed"
    conn = get_db_connection()
    try:
        conn.execute(text("""
            UPDATE payments
            SET status = :st
            WHERE (order_id = :oid OR payment_id = :oid OR payment_id = :pid)
              AND lower(coalesce(status, '')) <> :activated
        """), {
            "st": status,
            "oid": order_id or "",
            "pid": payment_id or "",
            "activated": ACTIVATED_STATUS,
        })
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


def process_payment(user_id, payment_id, amount_in_rupees):
    """DEAD/LEGACY helper. Does not activate subscriptions. Do not call from routes."""
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
