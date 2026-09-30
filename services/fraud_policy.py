"""Fraud and abuse policy: the ONE place abuse rules, thresholds and their log lines live.

Decisions (audit section 4, 2026-09-30):

* Flag, don't block. Signup never fails because of a rule here. A flagged
  account is created normally, gets ``users.is_flagged`` + a reason, and
  shows up in the admin user list (filter "Flagged").
* Fail open. A rule that errors is logged and skipped; it must never take
  signup or login down with it (the same principle as
  ``persist_referral_for_phone``: "never block OTP/login").
* Every block or flag writes one WARNING line starting ``fraud_policy``.
  Phone numbers are masked in those lines (last 4 digits only).

Rules:

* ip_accounts        - the 6th and later account created from one IP.
* phone_lookalike    - the 3rd and later account whose phone shares its
                       first 8 digits with others created in the last 24 h
                       (consecutive and look-alike numbers alike).
* referrer_shares_ip - a new account that uses a referral code whose owner
                       signed up from the same IP (own code on own second
                       account, or family/office). The referral is kept;
                       both accounts are flagged.
* self_referral_*    - a referral code used on the account that owns it is
                       dropped (never attributed) and logged.

Not in this module by design: ``services/referral_commission.py`` keeps its
own last-line ``referrer == referred`` guard because it sits in payout code.
"""
import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# The 6th and later account from one IP is flagged.
MAX_ACCOUNTS_PER_IP = 5

# Phones sharing this many leading digits are "look-alike"...
LOOKALIKE_PREFIX_DIGITS = 8
# ...and the 3rd and later such account inside the window is flagged.
LOOKALIKE_MIN_ACCOUNTS = 3
LOOKALIKE_WINDOW_HOURS = 24

# users.flag_reason is capped so a repeatedly flagged account can't grow it forever.
FLAG_REASON_MAX_CHARS = 1000


def mask_phone(phone):
    """Last 4 digits only, for log lines."""
    digits = str(phone or "")
    return ("*" * max(len(digits) - 4, 0)) + digits[-4:]


def _fields(fields):
    return " ".join(f"{key}={value}" for key, value in fields.items())


def log_block(reason, **fields):
    """A request or action that was refused/dropped by policy."""
    logger.warning("fraud_policy BLOCK reason=%s %s", reason, _fields(fields))


def log_throttle(reason, **fields):
    """A normal-use throttle (real users hit these too), so INFO rather than WARNING."""
    logger.info("fraud_policy THROTTLE reason=%s %s", reason, _fields(fields))


def note_resend_cooldown(phone, wait_seconds):
    """send/resend refused (429) because the previous OTP was sent moments ago."""
    log_throttle("otp_resend_cooldown", phone=mask_phone(phone), wait_seconds=wait_seconds)


def is_self_referral(referrer_phone, phone):
    """True (and logged) when the referral code belongs to this same phone."""
    if referrer_phone and referrer_phone == phone:
        log_block("self_referral_same_phone", phone=mask_phone(phone))
        return True
    return False


def is_self_referral_id(referrer_id, user_id):
    """True (and logged) when a new account would be its own referrer."""
    if referrer_id is not None and referrer_id == user_id:
        log_block("self_referral_same_account", user_id=user_id)
        return True
    return False


def note_otp_lockout(phone):
    """The OTP lockout answered 429 OTP_LOCKED for this number."""
    log_block("otp_locked", phone=mask_phone(phone))


def flag_user(conn, user_id, reason):
    """Mark an account flagged (idempotent) and log why. Caller commits."""
    conn.execute(
        text("""
            UPDATE users
            SET is_flagged = 1,
                flag_reason = LEFT(
                    CASE WHEN flag_reason IS NULL OR flag_reason = ''
                         THEN :reason
                         ELSE flag_reason || '; ' || :reason
                    END,
                    :max_chars
                ),
                flagged_at = COALESCE(flagged_at, NOW())
            WHERE id = :uid
        """),
        {"uid": user_id, "reason": reason, "max_chars": FLAG_REASON_MAX_CHARS},
    )
    logger.warning("fraud_policy FLAG user_id=%s reason=%s", user_id, reason)


def evaluate_signup(conn, user_id, phone, ip, referrer_id=None):
    """Run the signup rules for a just-created account. Never raises.

    ``conn`` is a live connection on which the new user row is already
    inserted (so counts include it). Returns the list of reasons flagged.
    """
    reasons = []
    try:
        if ip:
            accounts = conn.execute(
                text("SELECT COUNT(*) FROM users WHERE ip_address = :ip"), {"ip": ip}
            ).scalar()
            if accounts > MAX_ACCOUNTS_PER_IP:
                reasons.append(
                    f"ip_accounts: {accounts} accounts from ip {ip} (flagged above {MAX_ACCOUNTS_PER_IP})"
                )

        if phone and len(phone) > LOOKALIKE_PREFIX_DIGITS:
            prefix = phone[:LOOKALIKE_PREFIX_DIGITS]
            lookalikes = conn.execute(
                text("""
                    SELECT COUNT(*) FROM users
                    WHERE phone LIKE :prefix
                      AND created_at > NOW() - make_interval(hours => :hours)
                """),
                {"prefix": prefix + "%", "hours": LOOKALIKE_WINDOW_HOURS},
            ).scalar()
            if lookalikes >= LOOKALIKE_MIN_ACCOUNTS:
                reasons.append(
                    f"phone_lookalike: {lookalikes} accounts starting {prefix} "
                    f"in {LOOKALIKE_WINDOW_HOURS}h (flagged from {LOOKALIKE_MIN_ACCOUNTS})"
                )

        referrer_ip = None
        if referrer_id is not None and ip:
            referrer_ip = conn.execute(
                text("SELECT ip_address FROM users WHERE id = :rid"), {"rid": referrer_id}
            ).scalar()
            if referrer_ip and referrer_ip == ip:
                reasons.append(f"referrer_shares_ip: referrer user_id={referrer_id} signed up from ip {ip}")

        for reason in reasons:
            flag_user(conn, user_id, reason)
        if referrer_ip and referrer_ip == ip:
            flag_user(conn, referrer_id, f"referred_user_shares_ip: referred user_id={user_id} signed up from ip {ip}")

        if reasons:
            conn.commit()
    except Exception:
        logger.exception("fraud_policy evaluate_signup failed for user_id=%s; signup continues", user_id)
        try:
            conn.rollback()
        except Exception:
            pass
        return []
    return reasons
