# agents/payment_agent.py
"""Payment Agent — non-canonical Razorpay helpers.

LIVE payment checkout is POST /api/payment/create-order and
POST /api/payment/verify-payment. These methods must not create orders from
client amounts or credit wallet_balance from a payment_id alone.
"""

import razorpay
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class PaymentAgent:
    """Placeholder agent. Does not mutate payments, wallets, or subscriptions."""

    def __init__(self, app=None):
        self.app = app
        self.client = None
        if app:
            self.init_razorpay(app)

    def init_razorpay(self, app):
        try:
            self.client = razorpay.Client(
                auth=(app.config.get("RAZORPAY_KEY_ID"), app.config.get("RAZORPAY_KEY_SECRET"))
            )
            logger.info("Razorpay client initialized")
        except Exception as e:
            logger.error(f"Razorpay initialization failed: {str(e)}")

    def create_order(self, user_id: int, amount: int, currency: str = "INR") -> Dict[str, Any]:
        return {
            "success": False,
            "error": "Use POST /api/payment/create-order",
        }

    def verify_payment(self, payment_id: str, user_id: int) -> Dict[str, Any]:
        return {
            "success": False,
            "error": "Use POST /api/payment/verify-payment",
        }
