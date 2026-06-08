# config/payment_config.py
import os

# Plan prices in paise
PLAN_PRICES = {
    "Service Provider": 49900,
    "Business Basic": 99900,
    "Business Premium": 199900
}

# Get Razorpay mode from environment
RAZORPAY_MODE = os.getenv("RAZORPAY_MODE", "test")

# Get keys - don't fail immediately
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

# Base URL for the app
BASE_URL = os.getenv("BASE_URL", "https://landmarkvts.in")

# Only validate keys if we're not in a build/import context
# and if we're actually running the app
_IN_RUNTIME = os.getenv("RENDER", "false") == "true" or __name__ == "__main__"

if _IN_RUNTIME and RAZORPAY_MODE == "live":
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise ValueError("Razorpay keys missing for live mode")
    print("✅ Razorpay configured for LIVE mode")
elif _IN_RUNTIME and RAZORPAY_MODE == "test":
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        # Don't fail in test mode - just warn
        print("⚠️ Razorpay test keys not configured - payment will not work")
    else:
        print(f"✅ Razorpay configured for TEST mode with key: {RAZORPAY_KEY_ID[:10]}...")