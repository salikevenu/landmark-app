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
        """Return the permanent Auth Token from environment variables."""
        # ✅ No need to fetch a new token. Use the permanent one.
        return self.auth_token
    
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

            # ✅ CHANGE FROM requests.get TO requests.post
            response = requests.post(
                url,
                params=params,
                headers=headers,
                timeout=20
            )

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
        try:
            if self.debug_mode:
                print(f"\n🔴🔴🔴 DEBUG MODE - Verifying OTP {otp} for ID {verification_id} 🔴🔴🔴\n")
                return True, {"debug": True}

            auth_token = self._get_auth_token()
            if not auth_token:
                return False, {"error": "Failed to get auth token"}

            url = "https://cpaas.messagecentral.com/verification/v3/validateOtp"
            
            # ✅ FINAL PARAMS: Includes flowType and customerId
            params = {
                "verificationId": verification_id,
                "code": otp,
                "flowType": "SMS",                  # ✅ Crucial addition
                "customerId": self.customer_id      # ✅ Crucial addition
            }
            
            headers = {"authToken": auth_token}

            response = requests.post(url, params=params, headers=headers, timeout=20)

            if response.status_code == 200:
                data = response.json()
                logger.info(f"OTP verified successfully for ID {verification_id}")
                return True, data

            logger.error(f"Verification failed: {response.status_code} - {response.text}")
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