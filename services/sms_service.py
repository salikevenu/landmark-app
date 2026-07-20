"""
Unified SMS Service for Message Central
- Single source of truth for all SMS operations
- Handles both OTP and general SMS
"""
import os
import logging
import random
import requests
from typing import Tuple, Optional, Dict, Any

logger = logging.getLogger(__name__)

class MessageCentralSMS:
    """Message Central SMS Service - Unified"""
    
    def __init__(self):
        self.customer_id = os.environ.get('MESSAGE_CENTRAL_CUSTOMER_ID')
        self.auth_token = os.environ.get('MESSAGE_CENTRAL_AUTH_TOKEN')
        self.country = os.environ.get('MESSAGE_CENTRAL_COUNTRY', '91')
        self.debug_mode = os.environ.get('DEBUG_SMS', 'False').lower() == 'true'
        
        logger.info(f"SMS Service initialized - Debug mode: {self.debug_mode}")
    
    def _get_auth_token(self) -> Optional[str]:
        """Get authentication token from Message Central"""
        if self.auth_token:
            return self.auth_token
        
        # Fallback: fetch fresh token
        url = "https://cpaas.messagecentral.com/auth/v1/authentication/token"
        params = {
            "customerId": self.customer_id,
            "key": os.getenv("MESSAGE_CENTRAL_KEY"),
            "scope": "NEW",
            "country": self.country,
            "email": os.getenv("MESSAGE_CENTRAL_EMAIL"),
        }
        headers = {"accept": "*/*"}
        
        try:
            response = requests.get(url, params=params, headers=headers, timeout=10)
            if response.status_code == 200:
                token = response.json().get("data", {}).get("token")
                if token:
                    logger.info("Message Central token obtained successfully")
                    return token
                logger.error("No token in Message Central response")
                return None
            logger.error(f"Message Central token error: {response.status_code} - {response.text}")
            return None
        except Exception as e:
            logger.error(f"Message Central token exception: {e}")
            return None
    
    def _format_phone(self, phone: str) -> Tuple[str, str]:
        """Format phone number - returns (full_phone, raw_phone)"""
        # Remove non-numeric
        phone = ''.join(filter(str.isdigit, phone))
        
        if len(phone) == 10:
            return self.country + phone, phone
        elif len(phone) == 12 and phone.startswith(self.country):
            return phone, phone[-10:]
        else:
            return phone, phone[-10:] if len(phone) >= 10 else phone
    
    def send_sms(self, phone: str, message: str) -> tuple[bool, dict]:
        try:
            full_phone, raw_phone = self._format_phone(phone)

            if self.debug_mode:
                print(f"\n🔴🔴🔴 DEBUG MODE - SMS to {full_phone}: {message} 🔴🔴🔴\n")
                return True, {"debug": True}

            auth_token = self._get_auth_token()
            if not auth_token:
                return False, {"error": "Failed to get auth token"}

            # ✅ TRY primary endpoint
            url = "https://cpaas.messagecentral.com/api/v1/send-sms"
            payload = {
                "flowType": "SMS",
                "type": "OTP",
                "country": self.country,
                "mobileNumber": raw_phone,
                "message": message,
            }
            headers = {"authToken": auth_token, "Content-Type": "application/json"}

            # Use a short timeout so it fails fast
            response = requests.post(url, json=payload, headers=headers, timeout=10)

            if response.status_code == 200:
                logger.info(f"SMS sent successfully to {full_phone}")
                return True, response.json()

            # If primary fails, try the backup endpoint
            logger.warning(f"Primary endpoint failed (HTTP {response.status_code}), trying backup...")
            backup_url = "https://api.messagecentral.com/v1/sms/send"
            backup_response = requests.post(backup_url, json=payload, headers=headers, timeout=10)

            if backup_response.status_code == 200:
                logger.info(f"SMS sent successfully via backup endpoint to {full_phone}")
                return True, backup_response.json()

            # Both failed
            logger.error(f"Both endpoints failed: Primary {response.status_code}, Backup {backup_response.status_code}")
            return False, {"error": "Both SMS endpoints failed"}

        except requests.exceptions.Timeout:
            logger.error("SMS request timed out")
            return False, {"error": "Request timeout"}
        except requests.exceptions.ConnectionError as e:
            logger.error(f"SMS connection error: {e}")
            return False, {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            logger.error(f"SMS error: {e}")
            return False, {"error": str(e)}
    
    def send_otp(self, phone: str, otp: str = None):
        """Send OTP via SMS"""
        if not otp:
            otp = str(random.randint(100000, 999999))
        
        # Debug mode
        if self.debug_mode:
            print(f"\n🔴🔴🔴 DEBUG MODE - OTP for {phone}: {otp} 🔴🔴🔴\n")
            return True, {"debug": True}, otp   # ✅ Always return a 3-tuple
        
        # Real SMS logic (must return 3 values)
        try:
            # ... your real SMS code here ...
            success, response = self.send_sms(phone, f"Your OTP is {otp}")
            return success, response, otp   # ✅ Always return a 3-tuple
        except Exception as e:
            return False, {"error": str(e)}, otp  # ✅ Always return a 3-tuple
# ============================================
# SINGLETON INSTANCE (Add this before get_sms_service)
# ============================================
_sms_service = None  # <--- THIS LINE IS MISSING!

def get_sms_service() -> MessageCentralSMS:
    """Get SMS service instance (singleton)"""
    global _sms_service
    if _sms_service is None:
        _sms_service = MessageCentralSMS()
    return _sms_service