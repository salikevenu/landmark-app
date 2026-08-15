# Canonical subscription-active check for listing/business access.
from datetime import datetime

PAID_PLANS = ("service_provider", "business_basic", "business_premium")
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
