# config/payment_config.py

import os

RAZORPAY_MODE = os.getenv("RAZORPAY_MODE", "test").lower()

TEST_KEY_ID = os.getenv("RAZORPAY_TEST_KEY_ID")
TEST_KEY_SECRET = os.getenv("RAZORPAY_TEST_KEY_SECRET")

LIVE_KEY_ID = os.getenv("RAZORPAY_LIVE_KEY_ID")
LIVE_KEY_SECRET = os.getenv("RAZORPAY_LIVE_KEY_SECRET")

if RAZORPAY_MODE == "live":
    RAZORPAY_KEY_ID = LIVE_KEY_ID
    RAZORPAY_KEY_SECRET = LIVE_KEY_SECRET
else:
    RAZORPAY_KEY_ID = TEST_KEY_ID
    RAZORPAY_KEY_SECRET = TEST_KEY_SECRET

if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
    raise ValueError(
        f"Razorpay keys missing for mode: {RAZORPAY_MODE}"
    )

BASE_URL = os.getenv(
    "BASE_URL",
    "https://landmarkvts.in"
)

PLAN_PRICES = {
    "Service Provider": 49900,
    "Business Basic": 99900,
    "Business Premium": 199900
}

PLAN_PRICES_RUPEES = {
    "Service Provider": 499,
    "Business Basic": 999,
    "Business Premium": 1999
}

PLAN_REWARDS = {
    "Service Provider": 25,
    "Business Basic": 50,
    "Business Premium": 100
}