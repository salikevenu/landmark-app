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

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

PLAN_PRICES = {
    "service": 49900,
    "basic":   99900,
    "premium": 199900
}

PLAN_PRICES_RUPEES = {
    "service": 499,
    "basic":   999,
    "premium": 1999
}

PLAN_REWARDS = {
    "service": 25,
    "basic":   50,
    "premium": 100
}