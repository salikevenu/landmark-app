# agents/payment_agent.py
"""Payment Agent - Handles Razorpay Integration"""

import razorpay
from flask import current_app
import logging
from typing import Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

class PaymentAgent:
    """Handles all payment operations with Razorpay"""
    
    def __init__(self, app=None):
        self.app = app
        self.client = None
        if app:
            self.init_razorpay(app)
    
    def init_razorpay(self, app):
        """Initialize Razorpay client"""
        try:
            self.client = razorpay.Client(
                auth=(app.config.get("RAZORPAY_KEY_ID"), app.config.get("RAZORPAY_KEY_SECRET"))
            )
            logger.info("Razorpay client initialized")
        except Exception as e:
            logger.error(f"Razorpay initialization failed: {str(e)}")
    
    def create_order(self, user_id: int, amount: int, currency: str = "INR") -> Dict[str, Any]:
        """Create Razorpay payment order"""
        from database.init_db import get_db_connection
        
        try:
            order_data = {
                "amount": amount * 100,
                "currency": currency,
                "receipt": f"order_{user_id}_{int(datetime.now().timestamp())}",
                "payment_capture": 1
            }
            
            order = self.client.order.create(data=order_data)
            
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO payment_transactions (user_id, order_id, amount, currency, status, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                    (user_id, order["id"], amount, currency, "created")
                )
                conn.commit()
            finally:
                conn.close()
            
            return {
                "success": True,
                "order_id": order["id"],
                "amount": amount,
                "currency": currency,
                "razorpay_key": self.app.config.get("RAZORPAY_KEY_ID")
            }
        except Exception as e:
            logger.error(f"Payment order creation failed: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def verify_payment(self, payment_id: str, user_id: int) -> Dict[str, Any]:
        """Verify payment with Razorpay"""
        from database.init_db import get_db_connection
        
        try:
            payment_details = self.client.payment.fetch(payment_id)
            
            if payment_details["status"] == "captured":
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    order_id = payment_details.get("order_id")
                    amount = payment_details["amount"] / 100
                    
                    cursor.execute(
                        "UPDATE payment_transactions SET status = %s, payment_id = %s, updated_at = NOW() WHERE user_id = %s AND order_id = %s",
                        ("completed", payment_id, user_id, order_id)
                    )
                    
                    cursor.execute(
                        "INSERT INTO wallet_balance (user_id, balance, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (user_id) DO UPDATE SET balance = wallet_balance.balance + %s, updated_at = NOW()",
                        (user_id, amount, amount)
                    )
                    
                    conn.commit()
                finally:
                    conn.close()
                
                return {
                    "success": True,
                    "amount": amount,
                    "payment_id": payment_id,
                    "status": "completed"
                }
            return {"success": False, "error": "Payment not captured"}
        except Exception as e:
            logger.error(f"Payment verification failed: {str(e)}")
            return {"success": False, "error": str(e)}
