"""
Unified SMS Service for Message Central (VerifyNow API)
- Handles OTP generation and validation using Message Central's built-in flow
"""
import os
import logging
import requests
from typing import Tuple, Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

class MessageCentralSMS:
    """Message Central SMS Service using permanent Auth Token"""
    
    def __init__(self):
        self.customer_id = os.environ.get('MESSAGE_CENTRAL_CUSTOMER_ID')
        self.auth_token = os.environ.get('MESSAGE_CENTRAL_AUTH_TOKEN')
        self.country = os.environ.get('MESSAGE_CENTRAL_COUNTRY', '91')
        self.debug_mode = os.environ.get('DEBUG_SMS', 'False').lower() == 'true'
        
        # ✅ Fail fast if required environment variables are missing
        if not self.customer_id:
            raise RuntimeError("MESSAGE_CENTRAL_CUSTOMER_ID is missing from environment variables")
        if not self.auth_token:
            raise RuntimeError("MESSAGE_CENTRAL_AUTH_TOKEN is missing from environment variables")
        
        # ✅ Use a persistent session for connection reuse and performance
        self.session = requests.Session()
        
        # ✅ Add retry strategy for temporary network failures
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        logger.info(f"SMS Service initialized - Debug mode: {self.debug_mode}")
    
    def _get_auth_token(self) -> Optional[str]:
        """Return the permanent Auth Token from environment variables."""
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
        try:
            full_phone, raw_phone = self._format_phone(phone)

            if self.debug_mode:
                print(f"\n🔴🔴🔴 DEBUG MODE - OTP requested for {full_phone} 🔴🔴🔴\n")
                return True, {"debug": True}, "debug_verification_id"

            auth_token = self._get_auth_token()
            if not auth_token:
                return False, {"error": "Failed to get auth token"}, None

            url = "https://cpaas.messagecentral.com/verification/v3/send"
            
            # ✅ EXACTLY AS POSTMAN
            params = {
                "customerId": self.customer_id,          # C-609BF84D41E340C
                "countryCode": self.country,             # 91
                "flowType": "SMS",                       # SMS
                "mobileNumber": raw_phone,               # 9959543954
                "otpLength": 6                           # 6
            }
            
            headers = {"authToken": auth_token}

            response = self.session.post(url, params=params, headers=headers, timeout=20)

            if response.status_code == 200:
                data = response.json()
                verification_id = (
                    data.get("data", {})
                        .get("verificationId")
                )
                if not verification_id:
                    logger.error(data)
                    return False, data, None
                logger.info(f"OTP sent successfully to {full_phone} (Verification ID: {verification_id})")
                return True, data, verification_id

            logger.error(f"Status: {response.status_code} - Response: {response.text}")
            return False, {"error": response.text}, None

        except Exception as e:
            logger.exception("send_otp failed")
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
            
            # ✅ EXACTLY AS POSTMAN
            params = {
                "verificationId": verification_id,  # Keep as string (Postman sends it as string)
                "code": otp,                         # Keep as string
                "flowType": "SMS"                    # Required
            }
            
            headers = {
                "authToken": auth_token,
                "Accept": "application/json"
            }

            response = self.session.post(url, params=params, headers=headers, timeout=20)

            # ... rest of your existing logging and error handling ...


# ============================================
# SINGLETON INSTANCE
# ============================================
_sms_service = None

def get_sms_service() -> MessageCentralSMS:
    global _sms_service
    if _sms_service is None:
        _sms_service = MessageCentralSMS()
    return _sms_service