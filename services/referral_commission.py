"""Canonical referral commission.

First successful paid subscription: monthly billing cycle pays a fixed
bonus by plan (see FIRST_BONUS_BY_PLAN) — NOT a percentage, NOT derived
from amount paid. A first subscription billed 3-month or 12-month instead
pays 10% of the actual successful payment amount (no fixed bonus) because
the discounted multi-month price already reflects that commitment; the
billing cycle is derived server-side from the stored/activated payment
amount, never trusted from client input.
Every eligible renewal thereafter (same plan, upgrade, downgrade, or
billing-cycle change): 10% of the actual successful payment amount.

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
from config.payment_config import get_plan_spec, duration_days_for_stored_amount

logger = logging.getLogger(__name__)

CANONICAL_WALLET = "wallet_balance.balance"
FIRST_BONUS_SOURCE = "referral_first_bonus"
RECURRING_SOURCE = "referral_recurring"
COMMISSION_SOURCES = (FIRST_BONUS_SOURCE, RECURRING_SOURCE)
JOB_PENDING = "pending"
JOB_COMPLETED = "completed"
JOB_SKIPPED = "skipped"
RECURRING_RATE = 0.10

# Fixed first-sale bonus by plan (config.payment_config.PLANS[*]["plan"] key).
# Financial constant, not a percentage — never derive this from amount paid.
FIRST_BONUS_BY_PLAN = {
    "service_provider": 50.0,
    "business_basic": 100.0,
    "business_premium": 150.0,
}


class CommissionPlanLookupError(Exception):
    """Raised when the first-sale bonus plan cannot be safely determined.

    Deliberately a plain exception (not a returned reason string) so it
    flows through the existing job-retry path in
    process_pending_referral_commission_jobs unchanged: the job is left
    pending, attempts is incremented, and last_error is recorded — exactly
    how any other unexpected failure in this function is already handled.
    Never guess a bonus amount when this is raised.
    """


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


def _load_activated_payment(conn, referred_user_id, razorpay_payment_id):
    """Fetch the payment row for the exact (payment_id, user_id) pair and
    require it to be in the repository's activated successful-payment
    state. Shared by BOTH the first-sale bonus lookup and the recurring
    branch, so the same financial invariant applies to every referral
    commission regardless of source: it must be tied to a genuinely
    activated payment, never a created/pending/failed one. Never guesses:
    raises CommissionPlanLookupError (not a returned reason string) so
    callers get the existing safe job-retry/error behavior for free.
    """
    row = conn.execute(
        text("""
            SELECT plan, status, user_id, amount
            FROM payments
            WHERE payment_id = :pid AND user_id = :uid
            ORDER BY id DESC
            LIMIT 1
        """),
        {"pid": str(razorpay_payment_id), "uid": referred_user_id},
    ).fetchone()
    if not row:
        raise CommissionPlanLookupError(
            f"No activated payment row found for user_id={referred_user_id} "
            f"payment_id={razorpay_payment_id}; refusing to create commission"
        )
    mapping = row._mapping
    if (mapping.get("status") or "").lower() != "activated":
        raise CommissionPlanLookupError(
            f"Payment {razorpay_payment_id} for user_id={referred_user_id} is not "
            f"in activated state (status={mapping.get('status')!r}); refusing "
            f"commission"
        )
    return mapping


def _first_commission_amount(conn, referred_user_id, razorpay_payment_id, amount):
    """Resolve the first-sale commission for THIS payment.

    Looks up payments.plan/amount via the exact (payment_id, user_id) pair
    so a payment can never be attributed to the wrong subscriber. The
    billing cycle is derived server-side from the activated payment's
    stored amount (never trusted from client/job input):

    - monthly cycle: fixed bonus by plan (FIRST_BONUS_BY_PLAN) — NOT a
      percentage.
    - 3-month or 12-month cycle: 10% of the actual successful payment
      amount — NOT the fixed bonus.

    Never guesses: an unrecognized plan or an amount that doesn't match
    any known billing cycle for that plan raises CommissionPlanLookupError
    rather than returning a defaulted or fixed amount.
    """
    mapping = _load_activated_payment(conn, referred_user_id, razorpay_payment_id)
    plan_key = mapping.get("plan")
    if plan_key not in FIRST_BONUS_BY_PLAN:
        raise CommissionPlanLookupError(
            f"Unrecognized plan {plan_key!r} on payment {razorpay_payment_id} "
            f"for user_id={referred_user_id}; refusing to guess first-sale commission"
        )
    display, spec = get_plan_spec(plan_key)
    if not spec:
        raise CommissionPlanLookupError(
            f"No plan spec found for plan {plan_key!r} on payment {razorpay_payment_id} "
            f"for user_id={referred_user_id}; refusing to guess first-sale commission"
        )
    cycle, _duration_days = duration_days_for_stored_amount(
        spec["amount_paise"], mapping.get("amount")
    )
    if cycle is None:
        raise CommissionPlanLookupError(
            f"Stored payment amount {mapping.get('amount')!r} for plan {plan_key!r} "
            f"on payment {razorpay_payment_id} (user_id={referred_user_id}) does not "
            f"match any known billing cycle; refusing to guess first-sale commission"
        )
    if cycle == "monthly":
        return FIRST_BONUS_BY_PLAN[plan_key]
    return round(float(amount) * RECURRING_RATE, 2)


def process_referral_commission(referred_user_id, payment_amount, razorpay_payment_id=None, conn=None):
    """Queue the fixed first-sale bonus (by plan) and 10% recurring commission
    for the referrer.

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

        # Payment-level idempotency guard: since first-bonus vs. recurring is
        # now decided by the (mutable) first_sub_commission_paid flag rather
        # than a per-source-only check, a retry of the SAME razorpay payment
        # could otherwise land in a different branch than its original
        # attempt (e.g. first attempt sets the flag and pays the fixed bonus;
        # a retry of that identical payment would then see the flag already
        # true and wrongly pay a second, different-source commission for the
        # same payment). Block any further processing once this exact
        # payment has produced a commission row under either source.
        # Scoped to (this payment AND this referred subscriber's own commission
        # row) via razorpay_payment_id + reference_id (the existing "user_<id>"
        # tag every commission row is written with — see _insert_commission_tx
        # call sites below). Scoping by reference_id, not just payment_id,
        # means a row that happens to share this payment_id but belongs to a
        # different referred user's commission can never be mistaken for
        # "this user's payment already processed".
        already = conn.execute(
            text("""
                SELECT 1 FROM wallet_transactions
                WHERE razorpay_payment_id = :rzp
                  AND reference_id = :ref_id
                  AND source IN (:src_first, :src_recurring)
                LIMIT 1
            """),
            {
                "rzp": razorpay_payment_id,
                "ref_id": user_ref,
                "src_first": FIRST_BONUS_SOURCE,
                "src_recurring": RECURRING_SOURCE,
            },
        ).fetchone()
        if already:
            return {"created": [], "reason": "already_processed", "referrer_id": referrer_id}

        # First successful paid subscription pays its first-sale commission
        # ONLY (fixed by-plan bonus for monthly, 10% of actual amount for
        # 3-month/12-month); every payment thereafter pays 10% recurring
        # ONLY — the two are mutually exclusive per payment, not stacked on
        # the first one.
        if not _is_truthy_flag(mapping.get("first_sub_commission_paid")):
            # Raises CommissionPlanLookupError on any ambiguity — deliberately
            # NOT caught here, so first_sub_commission_paid is only ever set
            # once the first-sale commission has actually been determined
            # (never burns the one-time first-sale opportunity on a failed
            # lookup).
            bonus = _first_commission_amount(conn, referred_user_id, razorpay_payment_id, amount)
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
        else:
            # Same activation requirement as the first-sale bonus: a
            # recurring commission must never be created from a payment
            # that isn't in the repository's activated successful-payment
            # state. Raises (not caught here) so an ineligible payment
            # never inserts a wallet row — no ledger write, no
            # first_sub_commission_paid change (it's already 1 here), and
            # the existing job retry/error path handles the failure safely.
            _load_activated_payment(conn, referred_user_id, razorpay_payment_id)
            recurring = round(amount * RECURRING_RATE, 2)
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
