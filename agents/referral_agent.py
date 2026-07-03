# agents/referral_agent.py
"""Referral Agent - Handles Referral Codes and Commissions"""

import logging
from typing import Dict, Any
import secrets

logger = logging.getLogger(__name__)

class ReferralAgent:
    """Handles referral code generation, tracking, and commission"""
    
    def __init__(self, app=None):
        self.app = app
        self.commission_percentage = 10
    
    def generate_referral_code(self, user_id: int) -> Dict[str, Any]:
        """Generate unique referral code for user"""
        from database.init_db import get_db
        
        try:
            suffix = secrets.token_hex(2).upper()
            code = f"LM{user_id}{suffix}"
            
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET referral_code = %s WHERE id = %s AND referral_code IS NULL RETURNING referral_code",
                    (code, user_id)
                )
                result = cursor.fetchone()
                if result:
                    conn.commit()
                    return {"success": True, "referral_code": code}
                
                cursor.execute("SELECT referral_code FROM users WHERE id = %s", (user_id,))
                existing = cursor.fetchone()
                if existing and existing[0]:
                    return {"success": True, "referral_code": existing[0]}
                return {"success": False, "error": "Failed to generate code"}
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Referral code generation failed: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def process_referral_reward(self, user_id: int, plan: str) -> Dict[str, Any]:
        """Process referral commission when a user upgrades"""
        from database.init_db import get_db
        
        try:
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT referred_by, phone FROM users WHERE id = %s AND referred_by IS NOT NULL",
                    (user_id,)
                )
                referral = cursor.fetchone()
                if not referral:
                    return {"success": True, "message": "No referral found"}
                
                referrer_id = referral[0]
                commission = 100
                
                if commission > 0:
                    cursor.execute(
                        "INSERT INTO wallet_balance (user_id, balance, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (user_id) DO UPDATE SET balance = wallet_balance.balance + %s, updated_at = NOW()",
                        (referrer_id, commission, commission)
                    )
                    
                    cursor.execute(
                        "INSERT INTO referral_transactions (referrer_id, referred_user_id, amount, plan, status, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                        (referrer_id, user_id, commission, plan, "credited")
                    )
                    conn.commit()
                
                return {"success": True, "referrer_id": referrer_id, "commission": commission, "plan": plan}
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Referral processing failed: {str(e)}")
            return {"success": False, "error": str(e)}
