# Canonical subscription-active check for listing/business access.
from datetime import datetime

from config.payment_config import BUSINESS_POWER_PLAN

PAID_PLANS = ("service_provider", "business_basic", "business_premium", BUSINESS_POWER_PLAN)
CANONICAL_CREATE_LISTING_API = "/api/listing/create-listing"


def legacy_add_business_gone():
    """410 payload for retired POST /api/add-business. Do not delete the route yet."""
    return {
        "success": False,
        "error": "This endpoint is retired. Create listings with POST /api/listing/create-listing.",
        "canonical": CANONICAL_CREATE_LISTING_API,
    }, 410


def is_subscription_active(user_row):
    """True only for a paid plan with a current expiry date.

    Used by listing API, listing page decorator, and any remaining business routes.
    Free / missing / expired plans are inactive.
    """
    if not user_row:
        return False
    plan = user_row.get("plan") or "free"
    if plan not in PAID_PLANS:
        return False
    expiry_str = user_row.get("subscription_expiry")
    if not expiry_str:
        return False
    try:
        expiry = datetime.strptime(str(expiry_str)[:10], "%Y-%m-%d").date()
        return expiry >= datetime.utcnow().date()
    except Exception:
        return False


def get_business_limit_for_user(user_row):
    """Single authoritative business/listing creation cap for a user.

    Returns None for unlimited (Business Power). Otherwise returns the
    plan's stored business_limit plus any purchased extra slots. Both
    listing-creation enforcement points (the create-listing page gate and
    the create-listing API) must call this instead of re-deriving the cap
    from plan strings themselves.
    """
    if not user_row:
        return 0
    plan = (user_row.get("plan") or "").strip().lower()
    if plan == BUSINESS_POWER_PLAN:
        return None
    limit = int(user_row.get("business_limit") or 0)
    extra = int(user_row.get("extra_businesses_purchased") or 0)
    return limit + extra
