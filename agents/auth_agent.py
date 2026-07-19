# agents/auth_agent.py
"""Authentication Agent - Handles OTP, JWT, User Verification"""

from flask import current_app
from flask_jwt_extended import create_access_token
import logging
from typing import Dict, Any
from datetime import datetime, timedelta
import phonenumbers
import secrets

logger = logging.getLogger(__name__)

class AuthAgent:
    """Handles all authentication and authorization operations"""
    
    def __init__(self, app=None):
        self.app = app
        self.otp_cache = {}
    
    def verify_user(self, user_id: int) -> Dict[str, Any]:
        """Verify user exists and is active"""
        from database.init_db import get_db_connection
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, phone, role, is_active, is_blocked FROM users WHERE id = %s AND is_active = true AND is_blocked = false",
                (user_id,)
            )
            user = cursor.fetchone()
            
            if user:
                return {
                    "success": True,
                    "user_id": user[0],
                    "phone": user[1],
                    "role": user[2],
                    "is_active": user[3],
                    "is_blocked": user[4]
                }
            return {"success": False, "error": "User not found or blocked"}
        except Exception as e:
            logger.error(f"User verification failed: {str(e)}")
            return {"success": False, "error": str(e)}
        finally:
            conn.close()
    
    def generate_otp(self, phone: str) -> Dict[str, Any]:
        """Generate OTP for phone number"""
        try:
            parsed = phonenumbers.parse(phone, "IN")
            if not phonenumbers.is_valid_number(parsed):
                return {"success": False, "error": "Invalid phone number"}
            
            otp = str(secrets.randbelow(900000) + 100000)
            self.otp_cache[phone] = {
                "otp": otp,
                "created_at": datetime.now(),
                "expires_at": datetime.now() + timedelta(minutes=5)
            }
            
            logger.info(f"OTP generated for {phone}: {otp}")
            return {"success": True, "otp": otp, "expires_in": 300}
        except Exception as e:
            logger.error(f"OTP generation failed: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def verify_otp(self, phone: str, otp: str) -> Dict[str, Any]:
        """Verify OTP and generate JWT"""
        from database.init_db import get_db_connection
        
        try:
            if phone not in self.otp_cache:
                return {"success": False, "error": "OTP not found or expired"}
            
            otp_data = self.otp_cache[phone]
            
            if datetime.now() > otp_data["expires_at"]:
                del self.otp_cache[phone]
                return {"success": False, "error": "OTP expired"}
            
            if otp_data["otp"] != otp:
                return {"success": False, "error": "Invalid OTP"}
            
            del self.otp_cache[phone]
            
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT id, role FROM users WHERE phone = %s", (phone,))
                user = cursor.fetchone()
                
                if not user:
                    cursor.execute(
                        "INSERT INTO users (phone, role, plan, created_at) VALUES (%s, %s, %s, NOW()) RETURNING id, role",
                        (phone, "free", "free")
                    )
                    user = cursor.fetchone()
                    conn.commit()
                
                user_id = user[0]
                role = user[1]
                
                access_token = create_access_token(
                    identity=str(user_id),
                    additional_claims={"role": role, "phone": phone}
                )
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "role": role,
                    "access_token": access_token,
                    "phone": phone
                }
            finally:
                conn.close()
                
        except Exception as e:
            logger.error(f"OTP verification failed: {str(e)}")
            return {"success": False, "error": str(e)}
