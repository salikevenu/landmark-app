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
        from database.init_db import get_db_connection
        
        try:
            suffix = secrets.token_hex(2).upper()
            code = f"LM{user_id}{suffix}"
            
            conn = get_db_connection()
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
        """LEGACY / DISABLED. Old ₹100 credit. Live path: referral_commission."""
        logger.error(
            "LEGACY DISABLED: ReferralAgent.process_referral_reward is not the live commission path"
        )
        return {"success": False, "error": "legacy_referral_agent_disabled"}
