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
        "amount_paise": 49900,
        "duration_days": 30,
        "business_limit": 10,
    },
    "Business Basic": {
        "plan": "business_basic",
        "role": "business_basic",
        "amount_paise": 99900,
        "duration_days": 30,
        "business_limit": 1,
    },
    "Business Premium": {
        "plan": "business_premium",
        "role": "business_premium",
        "amount_paise": 199900,
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

RAZORPAY_MODE = os.getenv("RAZORPAY_MODE", "test")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
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


_IN_RUNTIME = os.getenv("RENDER", "false") == "true" or __name__ == "__main__"

if _IN_RUNTIME and RAZORPAY_MODE == "live":
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise ValueError("Razorpay keys missing for live mode")
    print("Razorpay configured for LIVE mode")
elif _IN_RUNTIME and RAZORPAY_MODE == "test":
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        print("WARNING: Razorpay test keys not configured - payment will not work")
    else:
        print(f"Razorpay configured for TEST mode with key: {RAZORPAY_KEY_ID[:10]}...")
