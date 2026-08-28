# services/wallet_service.py
"""Canonical wallet: spendable balance is wallet_balance.balance only.

users.wallet_balance is a legacy column and is never read or written here.
Locked referral credits are not spendable until Saturday payout releases them.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
import logging
import math

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database.init_db import get_db_connection

logger = logging.getLogger(__name__)

CANONICAL_WALLET = "wallet_balance.balance"
MIN_WITHDRAW = Decimal("100.00")
MAX_WITHDRAW = Decimal("50000.00")
FIRST_WITHDRAW_MIN_BALANCE = Decimal("500.00")
PAID_BUSINESS_PLANS = ("business_basic", "business_premium")
MONEY_QUANT = Decimal("0.01")
WITHDRAW_DEBIT_SOURCE = "withdraw_request"
WITHDRAW_REFUND_SOURCE = "withdraw_refund"
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_PAID = "paid"


def _as_int_user_id(user_id):
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def parse_money(value, *, field="amount"):
    """Parse a rupee amount. Rejects NaN/Inf/negative/excessive decimals."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"Invalid {field}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Invalid {field}")
        raw = format(value, "f")
    else:
        raw = str(value).strip()
        if raw == "":
            raise ValueError(f"Invalid {field}")
        lower = raw.lower()
        if lower in ("nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
            raise ValueError(f"Invalid {field}")
        if "e" in lower:
            raise ValueError(f"Invalid {field}")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid {field}") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"Invalid {field}")
    if amount.as_tuple().exponent < -2:
        raise ValueError(f"{field} cannot have more than 2 decimal places")
    quantized = amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    return quantized


def _money(value):
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _settings_limits(conn=None):
    """Hard product limits. admin_settings must not weaken or raise these."""
    return MIN_WITHDRAW, MAX_WITHDRAW


def get_wallet_balance(user_id):
    """Spendable balance from wallet_balance.balance. Locked commissions are excluded."""
    uid = _as_int_user_id(user_id)
    conn = get_db_connection()
    try:
        row = conn.execute(
            text("SELECT balance FROM wallet_balance WHERE user_id = :uid"),
            {"uid": uid},
        ).fetchone()
        if not row:
            return Decimal("0.00")
        return _money(row._mapping["balance"])
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ensure_wallet_row(conn, user_id):
    conn.execute(text("""
        INSERT INTO wallet_balance (user_id, balance)
        VALUES (:uid, 0)
        ON CONFLICT (user_id) DO NOTHING
    """), {"uid": user_id})


def _cas_debit(conn, user_id, amount):
    result = conn.execute(text("""
        UPDATE wallet_balance
        SET balance = balance - :amount, updated_at = NOW()
        WHERE user_id = :uid AND balance >= :amount
    """), {"amount": str(amount), "uid": user_id})
    rc = getattr(result, "rowcount", None)
    try:
        return int(rc) == 1
    except (TypeError, ValueError):
        return False


def _credit_balance(conn, user_id, amount):
    _ensure_wallet_row(conn, user_id)
    conn.execute(text(
        "SELECT balance FROM wallet_balance WHERE user_id = :uid FOR UPDATE"
    ), {"uid": user_id})
    result = conn.execute(text("""
        UPDATE wallet_balance
        SET balance = balance + :amount, updated_at = NOW()
        WHERE user_id = :uid
    """), {"amount": str(amount), "uid": user_id})
    rc = getattr(result, "rowcount", None)
    try:
        return int(rc) == 1
    except (TypeError, ValueError):
        return False


def credit_wallet(user_id, amount, source="system", reference_id=None):
    uid = _as_int_user_id(user_id)
    if uid is None:
        return False
    try:
        money = parse_money(amount)
    except ValueError:
        return False
    conn = get_db_connection()
    try:
        if not _credit_balance(conn, uid, money):
            conn.rollback()
            return False
        conn.execute(text("""
            INSERT INTO wallet_transactions
            (user_id, amount, type, source, reference_id, status)
            VALUES (:uid, :amount, 'credit', :source, :ref_id, 'completed')
        """), {
            "uid": uid,
            "amount": str(money),
            "source": source,
            "ref_id": reference_id,
        })
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("credit_wallet failed")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def debit_wallet(user_id, amount, source="withdraw", reference_id=None):
    """Atomic debit of spendable wallet_balance.balance. Never goes negative."""
    uid = _as_int_user_id(user_id)
    if uid is None:
        return False
    try:
        money = parse_money(amount)
    except ValueError:
        return False
    conn = get_db_connection()
    try:
        _ensure_wallet_row(conn, uid)
        conn.execute(text(
            "SELECT balance FROM wallet_balance WHERE user_id = :uid FOR UPDATE"
        ), {"uid": uid})
        if not _cas_debit(conn, uid, money):
            conn.rollback()
            return False
        conn.execute(text("""
            INSERT INTO wallet_transactions
            (user_id, amount, type, source, reference_id, status)
            VALUES (:uid, :amount, 'debit', :source, :ref_id, 'completed')
        """), {
            "uid": uid,
            "amount": str(money),
            "source": source,
            "ref_id": reference_id,
        })
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("debit_wallet failed")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _had_first_withdrawal(conn, user_id):
    """True after an approved or paid withdrawal. Does not use optional prod columns."""
    row = conn.execute(text("""
        SELECT id FROM withdraw_requests
        WHERE user_id = :uid AND status IN ('approved', 'paid')
        LIMIT 1
    """), {"uid": user_id}).fetchone()
    return row is not None


def _paid_business_referral_count(conn, user_id):
    row = conn.execute(text("""
        SELECT COUNT(*) AS cnt FROM users
        WHERE referred_by = :uid
          AND plan IN ('business_basic', 'business_premium')
    """), {"uid": user_id}).fetchone()
    if not row:
        return 0
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return int(dict(mapping).get("cnt") or 0)
        except Exception:
            pass
    try:
        return int(row[0] or 0)
    except Exception:
        return 0


def _existing_idempotent_withdraw(conn, user_id, idempotency_key):
    if not idempotency_key:
        return None
    row = conn.execute(text("""
        SELECT id, amount, status
        FROM withdraw_requests
        WHERE user_id = :uid AND reference_id = :ref
        ORDER BY id DESC
        LIMIT 1
    """), {"uid": user_id, "ref": idempotency_key}).fetchone()
    if not row:
        return None
    return dict(row._mapping)


def request_withdrawal(user_id, amount, upi_id, idempotency_key=None):
    """Reserve spendable balance and create a pending withdrawal in one transaction.

    Money leaves wallet_balance.balance immediately. Reject restores it once.
    Admin approve must NOT debit again.

    Product rules:
      - Amount always ≥ ₹100 and ≤ ₹50,000 (hardcoded; not taken from admin_settings).
      - First withdrawal also requires ₹500 spendable balance and at least one
        referred user on a paid business plan (business_basic / business_premium).
      - Later withdrawals only need the ₹100 minimum and sufficient balance.
    """
    uid = _as_int_user_id(user_id)
    if uid is None:
        return {"success": False, "error": "Authentication required", "_http": 401}
    try:
        money = parse_money(amount)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "_http": 400}
    upi = (upi_id or "").strip()
    if not upi or len(upi) > 120:
        return {"success": False, "error": "UPI ID required", "_http": 400}
    key = (idempotency_key or "").strip() or None
    if key and len(key) > 120:
        return {"success": False, "error": "Invalid request id", "_http": 400}

    conn = get_db_connection()
    try:
        min_amt, max_amt = _settings_limits(conn)
        if money < min_amt:
            return {"success": False, "error": f"Minimum withdrawal amount is ₹{min_amt}", "_http": 400}
        if money > max_amt:
            return {"success": False, "error": f"Maximum withdrawal amount is ₹{max_amt}", "_http": 400}

        existing = _existing_idempotent_withdraw(conn, uid, key)
        if existing:
            return {
                "success": True,
                "message": "Withdrawal request submitted",
                "status": existing.get("status") or STATUS_PENDING,
                "withdrawal_id": existing.get("id"),
                "duplicate": True,
            }

        _ensure_wallet_row(conn, uid)
        locked = conn.execute(text(
            "SELECT balance FROM wallet_balance WHERE user_id = :uid FOR UPDATE"
        ), {"uid": uid}).fetchone()
        mapped = getattr(locked, "_mapping", None) if locked is not None else None
        try:
            available = _money(mapped["balance"] if mapped else 0)
        except Exception:
            available = Decimal("0.00")

        if not _had_first_withdrawal(conn, uid):
            if available < FIRST_WITHDRAW_MIN_BALANCE:
                conn.rollback()
                return {
                    "success": False,
                    "error": "First withdrawal requires minimum ₹500 balance",
                    "_http": 400,
                }
            if _paid_business_referral_count(conn, uid) < 1:
                conn.rollback()
                return {
                    "success": False,
                    "error": "You must refer at least 1 paid business subscription to withdraw",
                    "_http": 400,
                }

        if not _cas_debit(conn, uid, money):
            conn.rollback()
            return {"success": False, "error": "Insufficient wallet balance", "_http": 400}

        result = conn.execute(text("""
            INSERT INTO withdraw_requests
                (user_id, amount, payment_method, upi_id, status, reference_id, created_at)
            VALUES
                (:uid, :amount, 'upi', :upi, :status, :ref, :created_at)
            RETURNING id
        """), {
            "uid": uid,
            "amount": str(money),
            "upi": upi,
            "status": STATUS_PENDING,
            "ref": key,
            "created_at": datetime.utcnow(),
        })
        row = result.fetchone()
        wid = row[0] if row is not None else None
        if wid is None and hasattr(row, "_mapping"):
            wid = row._mapping.get("id")
        ledger_ref = f"withdraw:{wid}"
        conn.execute(text("""
            INSERT INTO wallet_transactions
            (user_id, amount, type, source, reference_id, status)
            VALUES (:uid, :amount, 'debit', :source, :ref_id, 'completed')
        """), {
            "uid": uid,
            "amount": str(money),
            "source": WITHDRAW_DEBIT_SOURCE,
            "ref_id": ledger_ref,
        })
        conn.commit()
        new_balance = get_wallet_balance(uid)
        return {
            "success": True,
            "message": "Withdrawal request submitted",
            "status": STATUS_PENDING,
            "withdrawal_id": wid,
            "new_balance": float(new_balance),
        }
    except IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        existing = None
        try:
            existing = _existing_idempotent_withdraw(conn, uid, key)
        except Exception:
            existing = None
        if existing:
            return {
                "success": True,
                "message": "Withdrawal request submitted",
                "status": existing.get("status") or STATUS_PENDING,
                "withdrawal_id": existing.get("id"),
                "duplicate": True,
            }
        return {"success": False, "error": "Duplicate withdrawal request", "_http": 409}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("request_withdrawal failed")
        return {"success": False, "error": "Unable to submit withdrawal", "_http": 500}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _transition_withdraw(conn, wid, from_status, to_status):
    conn.execute(text("""
        SELECT id FROM withdraw_requests WHERE id = :wid FOR UPDATE
    """), {"wid": int(wid)})
    result = conn.execute(text("""
        UPDATE withdraw_requests
        SET status = :to_status, processed_at = NOW()
        WHERE id = :wid AND status = :from_status
        RETURNING id, user_id, amount, status
    """), {
        "to_status": to_status,
        "wid": int(wid),
        "from_status": from_status,
    })
    row = result.fetchone()
    if row is None:
        return None
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return dict(mapping)
        except Exception:
            pass
    return {"id": row[0], "user_id": row[1], "amount": row[2], "status": row[3]}


def approve_withdrawal(wid):
    """pending → approved. Does not debit (already reserved at request)."""
    conn = get_db_connection()
    try:
        row = _transition_withdraw(conn, wid, STATUS_PENDING, STATUS_APPROVED)
        if not row:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"success": False, "error": "Withdrawal not found or already processed", "_http": 400}
        conn.commit()
        return {"success": True, "status": STATUS_APPROVED}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("approve_withdrawal failed")
        return {"success": False, "error": "Unable to approve withdrawal", "_http": 500}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def reject_withdrawal(wid):
    """pending → rejected, restore reserved funds exactly once."""
    conn = get_db_connection()
    try:
        row = _transition_withdraw(conn, wid, STATUS_PENDING, STATUS_REJECTED)
        if not row:
            conn.rollback()
            return {"success": False, "error": "Withdrawal not found or already processed", "_http": 400}
        uid = row["user_id"]
        amount = parse_money(row["amount"])
        refund_ref = f"withdraw-refund:{int(wid)}"
        existing = conn.execute(text("""
            SELECT id FROM wallet_transactions
            WHERE reference_id = :ref AND source = :src
            LIMIT 1
        """), {"ref": refund_ref, "src": WITHDRAW_REFUND_SOURCE}).fetchone()
        if not existing:
            if not _credit_balance(conn, uid, amount):
                conn.rollback()
                return {"success": False, "error": "Unable to restore wallet balance", "_http": 500}
            conn.execute(text("""
                INSERT INTO wallet_transactions
                (user_id, amount, type, source, reference_id, status)
                VALUES (:uid, :amount, 'credit', :source, :ref_id, 'completed')
            """), {
                "uid": uid,
                "amount": str(amount),
                "source": WITHDRAW_REFUND_SOURCE,
                "ref_id": refund_ref,
            })
        conn.commit()
        return {"success": True, "status": STATUS_REJECTED}
    except IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            check = get_db_connection()
            try:
                wr = check.execute(text("""
                    SELECT status FROM withdraw_requests WHERE id = :wid
                """), {"wid": int(wid)}).fetchone()
                paid = check.execute(text("""
                    SELECT id FROM wallet_transactions
                    WHERE reference_id = :ref AND source = :src
                    LIMIT 1
                """), {
                    "ref": f"withdraw-refund:{int(wid)}",
                    "src": WITHDRAW_REFUND_SOURCE,
                }).fetchone()
            finally:
                check.close()
            status = (wr._mapping.get("status") if wr else None)
            if status == STATUS_REJECTED and paid:
                return {"success": True, "status": STATUS_REJECTED, "duplicate": True}
        except Exception:
            pass
        return {"success": False, "error": "Unable to reject withdrawal", "_http": 409}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("reject_withdrawal failed")
        return {"success": False, "error": "Unable to reject withdrawal", "_http": 500}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def mark_withdrawal_paid(wid):
    """approved → paid (pending → paid allowed if admin skips approve). Never from rejected/paid."""
    conn = get_db_connection()
    try:
        row = _transition_withdraw(conn, wid, STATUS_APPROVED, STATUS_PAID)
        if not row:
            row = _transition_withdraw(conn, wid, STATUS_PENDING, STATUS_PAID)
        if not row:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"success": False, "error": "Withdrawal not found or not payable", "_http": 400}
        conn.commit()
        return {"success": True, "status": STATUS_PAID}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("mark_withdrawal_paid failed")
        return {"success": False, "error": "Unable to mark withdrawal paid", "_http": 500}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def process_referral(user_id, purchase_amount):
    """LEGACY / DISABLED. Do not use. Live path: services.referral_commission."""
    logger.error(
        "LEGACY DISABLED: wallet_service.process_referral is not the live commission path"
    )
    return None


def get_wallet_transactions(user_id):
    uid = _as_int_user_id(user_id)
    conn = get_db_connection()
    try:
        rows = conn.execute(text("""
            SELECT id, amount, type, source, status, created_at
            FROM wallet_transactions
            WHERE user_id = :uid
            ORDER BY id DESC
            LIMIT 50
        """), {"uid": uid}).fetchall()
        items = []
        for r in rows:
            m = r._mapping
            created = m["created_at"]
            items.append({
                "id": m["id"],
                "amount": m["amount"],
                "type": m["type"],
                "source": m["source"],
                "status": m["status"],
                "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            })
        return items
    finally:
        try:
            conn.close()
        except Exception:
            pass


def add_pending_referral_reward(user_id, referral_transaction_id):
    """LEGACY / DISABLED. Old ₹2 pending reward. Not the live 10%/5% path."""
    logger.error(
        "LEGACY DISABLED: add_pending_referral_reward must not credit wallets"
    )
    return False
