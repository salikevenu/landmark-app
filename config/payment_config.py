# config/payment_config.py

import os

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

# Plan prices – values are in PAISA (Razorpay requirement)
PLAN_PRICES = {
    "service": 49900,   # ₹499
    "basic":   99900,   # ₹999
    "premium": 199900   # ₹1999
}

# Convenience: same prices in rupees (for display / referral rewards)
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

MAX_MONTHLY_REFERRAL_EARNING = 5000