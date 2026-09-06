# config/payment_config.py
import os

# Canonical LANDMARK plans.
# display_name: pricing UI / Razorpay description (frontend still sends this)
# plan: users.plan — listing API limits check this
# role: users.role — create-listing page decorator checks this
# amount_paise: Razorpay order amount
# duration_days: existing monthly checkout (pricing copy + both verifiers used 30 days)
# business_limit: listing slot cap used by create-listing page

PLANS = {
    "Service Provider": {
        "plan": "service_provider",
        "role": "service_provider",
        "amount_paise": 29900,
        "duration_days": 30,
        "business_limit": 10,
    },
    "Business Basic": {
        "plan": "business_basic",
        "role": "business_basic",
        "amount_paise": 69900,
        "duration_days": 30,
        "business_limit": 1,
    },
    "Business Premium": {
        "plan": "business_premium",
        "role": "business_premium",
        "amount_paise": 149900,
        "duration_days": 30,
        "business_limit": 3,
    },
}

# Backward-compatible map used by existing imports (display name → paise)
PLAN_PRICES = {name: spec["amount_paise"] for name, spec in PLANS.items()}

# Amount = (monthly_paise × months) × discount, rounded to paise.
BILLING_CYCLES = {
    "monthly": {"months": 1, "discount": 1.0, "duration_days": 30},
    "3months": {"months": 3, "discount": 0.90, "duration_days": 90},
    "yearly": {"months": 12, "discount": 0.70, "duration_days": 365},
}

_CYCLE_ALIASES = {
    "3month": "3months",
    "quarter": "3months",
    "quarterly": "3months",
    "year": "yearly",
    "annual": "yearly",
    "annually": "yearly",
}


def resolve_billing_cycle(raw):
    key = (raw or "monthly").strip().lower().replace("-", "").replace("_", "")
    key = _CYCLE_ALIASES.get(key, key)
    if key not in BILLING_CYCLES:
        raise ValueError("Invalid billing_cycle. Use monthly, 3months, or yearly.")
    return key


def billed_amount_paise(monthly_paise, cycle):
    term = BILLING_CYCLES[cycle]
    discount_bp = int(round(term["discount"] * 100))
    return (int(monthly_paise) * term["months"] * discount_bp) // 100


def billed_duration_days(cycle):
    return int(BILLING_CYCLES[cycle]["duration_days"])


def billed_term(monthly_paise, cycle=None):
    """Return (cycle_key, amount_paise, duration_days)."""
    key = resolve_billing_cycle(cycle)
    return key, billed_amount_paise(monthly_paise, key), billed_duration_days(key)


# Extra business is a listing-slot purchase, not a subscription plan.
# Amount matches the existing PLAN_DETAILS extra_business path (₹249).
EXTRA_BUSINESS_PLAN = "extra_business"
EXTRA_BUSINESS_AMOUNT_PAISE = 24900
_EXTRA_BUSINESS_ALIASES = {
    "extra_business",
    "extra-business",
    "extrabusiness",
    "extra business",
}


def is_extra_business_plan(plan_key):
    if not plan_key:
        return False
    key = str(plan_key).strip().lower().replace("_", " ").replace("-", " ")
    compact = key.replace(" ", "")
    return compact in ("extrabusiness",) or key in _EXTRA_BUSINESS_ALIASES


def duration_days_for_stored_amount(monthly_paise, stored_amount):
    """Resolve billing duration from the server-stored Razorpay amount.

    Notes/frontend duration are not trusted. Returns (cycle, duration_days)
    or (None, None) if the stored amount is not a known billed term.
    """
    try:
        stored = int(stored_amount)
    except (TypeError, ValueError):
        return None, None
    for cycle in BILLING_CYCLES:
        _, paise, days = billed_term(monthly_paise, cycle)
        if stored == int(paise):
            return cycle, days
    return None, None


def _resolve_razorpay_mode():
    raw = (os.getenv("RAZORPAY_MODE") or "").strip().lower()
    if raw in ("live", "test"):
        return raw
    key_id = os.getenv("RAZORPAY_KEY_ID") or ""
    if key_id.startswith("rzp_live_"):
        return "live"
    if os.getenv("RENDER") == "true":
        return "live"
    return "test"


def get_razorpay_key_pair():
    """Live/test keys from env. Live never falls back to RAZORPAY_TEST_*."""
    mode = _resolve_razorpay_mode()
    key_id = (os.getenv("RAZORPAY_KEY_ID") or "").strip() or None
    key_secret = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip() or None
    if mode == "test":
        key_id = key_id or (os.getenv("RAZORPAY_TEST_KEY_ID") or "").strip() or None
        key_secret = key_secret or (os.getenv("RAZORPAY_TEST_KEY_SECRET") or "").strip() or None
    if key_id:
        if mode == "live" and key_id.startswith("rzp_test_"):
            print("ERROR: Razorpay TEST keys are not allowed in live/production mode")
            return None, None
        if mode == "test" and key_id.startswith("rzp_live_"):
            print("ERROR: Razorpay LIVE keys are not allowed in test mode")
            return None, None
    return key_id, key_secret


def get_razorpay_webhook_secret():
    """Live webhook HMAC secret. Read at call time so Render env is current."""
    return (os.getenv("RAZORPAY_WEBHOOK_SECRET") or "").strip() or None


def log_razorpay_config():
    """Safe boot log. Missing live keys warn; they do not crash the process."""
    mode = _resolve_razorpay_mode()
    key_id, key_secret = get_razorpay_key_pair()
    if mode == "live":
        if not key_id or not key_secret:
            print("WARNING: Razorpay LIVE keys missing (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET) — payments will not work")
        else:
            print("Razorpay configured for LIVE mode")
        if not get_razorpay_webhook_secret():
            print("WARNING: RAZORPAY_WEBHOOK_SECRET is not set — live webhooks will return 503")
        return
    if not key_id or not key_secret:
        print("WARNING: Razorpay test keys not configured - payment will not work")
    else:
        print(f"Razorpay configured for TEST mode with key: {key_id[:10]}...")


# Snapshots for callers that still import these names. Prefer the getters above.
RAZORPAY_MODE = _resolve_razorpay_mode()
RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET = get_razorpay_key_pair()
RAZORPAY_WEBHOOK_SECRET = get_razorpay_webhook_secret()
BASE_URL = os.getenv("BASE_URL", "https://landmarkvts.in")


def get_plan_spec(plan_key):
    """Resolve a display name or internal plan/role key to a plan spec + display name."""
    if not plan_key:
        return None, None
    if plan_key in PLANS:
        return plan_key, PLANS[plan_key]
    for display, spec in PLANS.items():
        if spec["plan"] == plan_key or spec["role"] == plan_key:
            return display, spec
    return None, None
