"""Canonical referral commission (10% first successful sub, 5% each activation).

Stage 2B money-safety:
- Lock the referred user row (SELECT ... FOR UPDATE) before first-bonus decisions.
- Persist Razorpay payment identity on every commission ledger row.
- Durable referral_commission_jobs outbox, enqueued in the same transaction as
  payment activation. Duplicate payment notifications retry pending jobs.
- Saturday/admin payout uses one implementation: FOR UPDATE SKIP LOCKED, then
  UPDATE ... WHERE status='locked' and credit only if rowcount == 1.

Canonical spendable wallet
--------------------------
Application spend/credit APIs use ``wallet_balance.balance``
(see ``services.wallet_service.get_wallet_balance``, withdraw, wallet overview).
``users.wallet_balance`` is a legacy display column and is NOT updated by
referral payout. Do not drop it without a dedicated migration plan.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta
import logging

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database.init_db import get_db_connection

logger = logging.getLogger(__name__)

CANONICAL_WALLET = "wallet_balance.balance"
FIRST_BONUS_SOURCE = "referral_first_bonus"
RECURRING_SOURCE = "referral_recurring"
COMMISSION_SOURCES = (FIRST_BONUS_SOURCE, RECURRING_SOURCE)
JOB_PENDING = "pending"
JOB_COMPLETED = "completed"
JOB_SKIPPED = "skipped"


def get_unlock_utc(referral_utc: datetime):
    """Return UTC unlock datetime for a referral commission."""
    ist = referral_utc + timedelta(hours=5, minutes=30)
    days_until_saturday = (5 - ist.weekday()) % 7
    next_saturday_ist = ist + timedelta(days=days_until_saturday)
    next_saturday_ist = next_saturday_ist.replace(hour=18, minute=0, second=0, microsecond=0)

    is_friday_after_6pm = (ist.weekday() == 4 and ist.hour >= 18)
    is_saturday_before_6pm = (ist.weekday() == 5 and ist.hour < 18)
    if is_friday_after_6pm or is_saturday_before_6pm:
        next_saturday_ist += timedelta(days=7)

    return next_saturday_ist - timedelta(hours=5, minutes=30)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_truthy_flag(value):
    if value is True:
        return True
    if value in (False, None, 0, "0", "", "false", "False", "f", "F"):
        return False
    return bool(value)


def _nested(conn):
    begin = getattr(conn, "begin_nested", None)
    if callable(begin):
        return begin()
    return nullcontext()


def _insert_commission_tx(conn, referrer_id, amount, source, reference_id,
                          razorpay_payment_id, unlock_at_str):
    sql = text("""
        INSERT INTO wallet_transactions
            (user_id, amount, type, source, reference_id, status, unlock_at,
             created_at, razorpay_payment_id)
        VALUES
            (:referrer_id, :amount, 'credit', :source, :ref_id, 'locked',
             :unlock_at, CURRENT_TIMESTAMP, :rzp)
    """)
    params = {
        "referrer_id": referrer_id,
        "amount": amount,
        "source": source,
        "ref_id": reference_id,
        "unlock_at": unlock_at_str,
        "rzp": razorpay_payment_id,
    }
    try:
        with _nested(conn):
            conn.execute(sql, params)
        return True
    except IntegrityError:
        logger.info(
            "Idempotent skip: commission already recorded source=%s payment=%s ref=%s",
            source, razorpay_payment_id, reference_id,
        )
        return False


def ensure_referral_commission_schema():
    """Additive outbox/column ensure. Partial unique indexes stay in the approved migration."""
    conn = get_db_connection()
    try:
        conn.execute(text(
            "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS razorpay_payment_id TEXT"
        ))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS referral_commission_jobs (
                id SERIAL PRIMARY KEY,
                payment_id TEXT NOT NULL,
                razorpay_payment_id TEXT,
                referred_user_id INTEGER NOT NULL REFERENCES users(id),
                amount_rupees NUMERIC(12,2) NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                CONSTRAINT uq_referral_commission_jobs_payment UNIQUE (payment_id)
            )
        """))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("Could not ensure referral commission schema")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def process_referral_commission(referred_user_id, payment_amount, razorpay_payment_id=None, conn=None):
    """Queue 10% first-bonus and 5% recurring commission for the referrer.

    Must run inside one DB transaction with the referred user row locked.
    ``razorpay_payment_id`` is required for money-safe idempotency.
    """
    referred_user_id = _as_int(referred_user_id)
    if referred_user_id is None:
        return {"created": [], "reason": "invalid_user"}
    if not razorpay_payment_id:
        logger.error(
            "Refusing commission without razorpay_payment_id for user %s",
            referred_user_id,
        )
        return {"created": [], "reason": "missing_payment_id"}

    own = conn is None
    if own:
        conn = get_db_connection()
    created = []
    try:
        user = conn.execute(
            text("""
                SELECT referred_by, first_sub_commission_paid
                FROM users
                WHERE id = :uid
                FOR UPDATE
            """),
            {"uid": referred_user_id},
        ).fetchone()
        if not user:
            return {"created": [], "reason": "user_not_found"}
        mapping = user._mapping
        referrer_id = _as_int(mapping.get("referred_by"))
        if referrer_id is None:
            return {"created": [], "reason": "no_referrer"}
        if referrer_id == referred_user_id:
            logger.warning(
                "Self-referral blocked at commission layer user_id=%s",
                referred_user_id,
            )
            return {"created": [], "reason": "self_referral"}

        referral_time = datetime.utcnow()
        unlock_utc = get_unlock_utc(referral_time)
        unlock_at_str = unlock_utc.strftime("%Y-%m-%d %H:%M:%S")
        user_ref = f"user_{referred_user_id}"
        amount = float(payment_amount)

        if not _is_truthy_flag(mapping.get("first_sub_commission_paid")):
            bonus = round(amount * 0.10, 2)
            if bonus > 0:
                if _insert_commission_tx(
                    conn, referrer_id, bonus, FIRST_BONUS_SOURCE,
                    user_ref, razorpay_payment_id, unlock_at_str,
                ):
                    created.append(FIRST_BONUS_SOURCE)
            conn.execute(
                text("UPDATE users SET first_sub_commission_paid = 1 WHERE id = :uid"),
                {"uid": referred_user_id},
            )

        recurring = round(amount * 0.05, 2)
        if recurring > 0:
            if _insert_commission_tx(
                conn, referrer_id, recurring, RECURRING_SOURCE,
                user_ref, razorpay_payment_id, unlock_at_str,
            ):
                created.append(RECURRING_SOURCE)

        if own:
            conn.commit()
        return {"created": created, "reason": "ok", "referrer_id": referrer_id}
    except Exception:
        if own:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def enqueue_referral_commission_job(conn, payment_id, razorpay_payment_id,
                                    referred_user_id, amount_rupees):
    """Insert a pending commission job. Safe to call on duplicate activations."""
    if not payment_id or referred_user_id is None or amount_rupees is None:
        return
    conn.execute(text("""
        INSERT INTO referral_commission_jobs
            (payment_id, razorpay_payment_id, referred_user_id, amount_rupees, status)
        VALUES
            (:payment_id, :razorpay_payment_id, :referred_user_id, :amount_rupees, :status)
        ON CONFLICT (payment_id) DO NOTHING
    """), {
        "payment_id": str(payment_id),
        "razorpay_payment_id": str(razorpay_payment_id or payment_id),
        "referred_user_id": int(referred_user_id),
        "amount_rupees": float(amount_rupees),
        "status": JOB_PENDING,
    })


def process_pending_referral_commission_jobs(razorpay_payment_id=None, conn=None, limit=50):
    """Consume pending outbox rows. Idempotent. Failures leave the job pending."""
    own = conn is None
    if own:
        conn = get_db_connection()
    processed = []
    failed = []
    try:
        if razorpay_payment_id:
            rows = conn.execute(text("""
                SELECT id, payment_id, razorpay_payment_id, referred_user_id, amount_rupees
                FROM referral_commission_jobs
                WHERE status = 'pending'
                  AND (razorpay_payment_id = :pid OR payment_id = :pid)
                ORDER BY id
                FOR UPDATE SKIP LOCKED
            """), {"pid": str(razorpay_payment_id)}).fetchall()
        else:
            rows = conn.execute(text("""
                SELECT id, payment_id, razorpay_payment_id, referred_user_id, amount_rupees
                FROM referral_commission_jobs
                WHERE status = 'pending'
                ORDER BY id
                LIMIT :lim
                FOR UPDATE SKIP LOCKED
            """), {"lim": int(limit)}).fetchall()

        for row in rows:
            job = dict(row._mapping)
            job_id = job["id"]
            try:
                result = process_referral_commission(
                    job["referred_user_id"],
                    job["amount_rupees"],
                    razorpay_payment_id=job["razorpay_payment_id"] or job["payment_id"],
                    conn=conn,
                )
                reason = (result or {}).get("reason")
                if reason in ("no_referrer", "self_referral", "user_not_found", "invalid_user"):
                    conn.execute(text("""
                        UPDATE referral_commission_jobs
                        SET status = :status,
                            processed_at = CURRENT_TIMESTAMP,
                            last_error = NULL
                        WHERE id = :id AND status = 'pending'
                    """), {"status": JOB_SKIPPED, "id": job_id})
                    processed.append(job_id)
                elif reason == "missing_payment_id":
                    conn.execute(text("""
                        UPDATE referral_commission_jobs
                        SET attempts = attempts + 1,
                            last_error = :err
                        WHERE id = :id AND status = 'pending'
                    """), {
                        "err": "missing_payment_id: commission requires razorpay_payment_id",
                        "id": job_id,
                    })
                    failed.append(job_id)
                else:
                    conn.execute(text("""
                        UPDATE referral_commission_jobs
                        SET status = :status,
                            processed_at = CURRENT_TIMESTAMP,
                            last_error = NULL
                        WHERE id = :id AND status = 'pending'
                    """), {"status": JOB_COMPLETED, "id": job_id})
                    processed.append(job_id)
            except Exception as exc:
                logger.exception(
                    "Referral commission job %s failed for payment %s; leaving pending",
                    job_id, job.get("razorpay_payment_id"),
                )
                conn.execute(text("""
                    UPDATE referral_commission_jobs
                    SET attempts = attempts + 1,
                        last_error = :err
                    WHERE id = :id AND status = 'pending'
                """), {"err": str(exc)[:2000], "id": job_id})
                failed.append(job_id)

        if own:
            conn.commit()
        return {"processed": processed, "failed": failed}
    except Exception:
        if own:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def after_payment_finalized(result, razorpay_payment_id=None):
    """Retry/process outbox after verify or webhook, including duplicate notices.

    Payment success is independent of commission outcome. Failures stay pending.
    """
    if not result or not result.get("success"):
        return None
    pid = razorpay_payment_id or result.get("razorpay_payment_id")
    try:
        return process_pending_referral_commission_jobs(razorpay_payment_id=pid)
    except Exception:
        logger.exception(
            "Referral commission processing failed for payment %s; job remains recoverable",
            pid,
        )
        return None


def release_locked_referral_payouts():
    """Race-safe Saturday payout. Updates ONLY canonical wallet_balance.balance.

    Used by /internal/saturday-payout and admin trigger-payout. Do not duplicate.
    """
    released_count = 0
    conn = get_db_connection()
    try:
        locked = conn.execute(text("""
            SELECT id, user_id, amount
            FROM wallet_transactions
            WHERE type = 'credit'
              AND source IN ('referral_first_bonus', 'referral_recurring')
              AND status = 'locked'
              AND unlock_at <= NOW()
            FOR UPDATE SKIP LOCKED
        """)).fetchall()
        for row in locked:
            mapping = row._mapping
            uid = mapping["user_id"]
            amt = mapping["amount"]
            tid = mapping["id"]
            claimed = False
            try:
                with _nested(conn):
                    result = conn.execute(text("""
                        UPDATE wallet_transactions
                        SET status = 'released'
                        WHERE id = :tid AND status = 'locked'
                    """), {"tid": tid})
                    rowcount = getattr(result, "rowcount", None)
                    if rowcount != 1:
                        raise _SkipClaim()
                    conn.execute(text("""
                        INSERT INTO wallet_balance (user_id, balance, updated_at)
                        VALUES (:uid, :amt, NOW())
                        ON CONFLICT (user_id) DO UPDATE
                        SET balance = wallet_balance.balance + EXCLUDED.balance,
                            updated_at = NOW()
                    """), {"uid": uid, "amt": amt})
                    claimed = True
            except _SkipClaim:
                logger.info("Payout skip: wallet_transactions id=%s already claimed", tid)
            if claimed:
                released_count += 1
        conn.commit()
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
    return released_count


class _SkipClaim(Exception):
    """Internal: another worker already released this transaction."""


# ----- LEGACY WRAPPER for wallet_routes.py -----
def next_saturday_6pm_ist():
    """Return next Saturday 6pm IST (as UTC datetime)."""
    return get_unlock_utc(datetime.utcnow())
