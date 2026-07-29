"""
Unified SMS Service for Message Central (VerifyNow API)
- Handles OTP generation and validation using Message Central's built-in flow
"""
import os
import logging
import requests
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

class MessageCentralSMS:
    """Message Central SMS Service using VerifyNow API"""
    
    def __init__(self):
        self.customer_id = os.environ.get('MESSAGE_CENTRAL_CUSTOMER_ID')
        self.auth_token = os.environ.get('MESSAGE_CENTRAL_AUTH_TOKEN')
        self.country = os.environ.get('MESSAGE_CENTRAL_COUNTRY', '91')
        self.debug_mode = os.environ.get('DEBUG_SMS', 'False').lower() == 'true'
        
        logger.info(f"SMS Service initialized - Debug mode: {self.debug_mode}")
    
    def _get_auth_token(self) -> Optional[str]:
        """Get a fresh authentication token from Message Central."""
        # Always fetch a fresh token to avoid expiry issues
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
                data = response.json()
                # ✅ The documentation says the token is in 'authToken', not 'token'
                token = data.get("data", {}).get("authToken")
                if token:
                    logger.info("✅ Fresh Message Central authToken obtained successfully")
                    return token
                logger.error("No authToken found in Message Central response")
                return None
            logger.error(f"Message Central token error: {response.status_code} - {response.text}")
            return None
        except Exception as e:
            logger.error(f"Message Central token exception: {e}")
            return None
    
    def _format_phone(self, phone: str) -> Tuple[str, str]:
        """Format phone number - returns (full_phone, raw_phone)"""
        phone = ''.join(filter(str.isdigit, phone))
        
        if len(phone) == 10:
            return self.country + phone, phone
        elif len(phone) == 12 and phone.startswith(self.country):
            return phone, phone[-10:]
        else:
            return phone, phone[-10:] if len(phone) >= 10 else phone

    def send_otp(self, phone: str) -> Tuple[bool, Optional[dict], Optional[str]]:
        """
        Send OTP using Message Central VerifyNow API.
        Returns: (success, response_json, verification_id)
        """
        try:
            full_phone, raw_phone = self._format_phone(phone)

            if self.debug_mode:
                print(f"\n🔴🔴🔴 DEBUG MODE - OTP requested for {full_phone} 🔴🔴🔴\n")
                # In debug mode, we mock a verification_id
                return True, {"debug": True}, "debug_verification_id"

            auth_token = self._get_auth_token()
            if not auth_token:
                return False, {"error": "Failed to get auth token"}, None

            url = "https://cpaas.messagecentral.com/verification/v3/send"
            params = {
                "customerId": self.customer_id,
                "countryCode": self.country,
                "flowType": "SMS",
                "mobileNumber": raw_phone,
                "otpLength": 6
            }
            headers = {"authToken": auth_token}

            response = requests.post(url, params=params, headers=headers, timeout=20)

            if response.status_code == 200:
                data = response.json()
                verification_id = data["data"]["verificationId"]
                logger.info(f"OTP sent successfully to {full_phone} (Verification ID: {verification_id})")
                return True, data, verification_id

            logger.error(f"Status: {response.status_code} - Response: {response.text}")
            return False, {"error": response.text}, None

        except Exception as e:
            logger.error(f"send_otp error: {e}")
            return False, {"error": str(e)}, None

    def verify_otp(self, verification_id: str, otp: str) -> Tuple[bool, Optional[dict]]:
        """
        Validate the OTP using Message Central VerifyNow API.
        Returns: (success, response_json)
        """
        try:
            if self.debug_mode:
                print(f"\n🔴🔴🔴 DEBUG MODE - Verifying OTP {otp} for ID {verification_id} 🔴🔴🔴\n")
                return True, {"debug": True}

            auth_token = self._get_auth_token()
            if not auth_token:
                return False, {"error": "Failed to get auth token"}

            url = "https://cpaas.messagecentral.com/verification/v3/validateOtp"
            
            # ✅ Ensure these parameters are exactly correct
            params = {
                "verificationId": verification_id,
                "code": otp
            }
            
            # ✅ Ensure the header is exactly "authToken", not "Authorization" or anything else
            headers = {
                "authToken": auth_token
            }

            response = requests.post(url, params=params, headers=headers, timeout=20)

            if response.status_code == 200:
                data = response.json()
                logger.info(f"OTP verified successfully for ID {verification_id}")
                return True, data

            logger.error(f"Status: {response.status_code} - Response: {response.text}")
            return False, {"error": response.text}

        except Exception as e:
            logger.error(f"verify_otp error: {e}")
            return False, {"error": str(e)}

# ============================================
# SINGLETON INSTANCE
# ============================================
_sms_service = None

def get_sms_service() -> MessageCentralSMS:
    global _sms_service
    if _sms_service is None:
        _sms_service = MessageCentralSMS()
    return _sms_service