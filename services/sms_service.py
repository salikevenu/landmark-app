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
        self.debug_mode = os.getenv('DEBUG_SMS', 'false').lower() == 'true'
        
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
            params = {
                "customerId": self.customer_id,
                "countryCode": self.country,
                "flowType": "SMS",
                "mobileNumber": raw_phone,
                "otpLength": 6
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
            # ✅ This is the missing block
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
            
            params = {
                "verificationId": verification_id,
                "code": otp,
                "flowType": "SMS"
            }
            
            headers = {
                "authToken": auth_token,
                "Accept": "application/json"
            }

            response = self.session.get(url, params=params, headers=headers, timeout=20)
            logger.info(f"Raw response from Message Central: {response.text}")

            safe_headers = dict(response.request.headers)
            if "authToken" in safe_headers:
                safe_headers["authToken"] = "***REDACTED***"

            logger.info("Request URL: %s", response.request.url)
            logger.info("Request Method: %s", response.request.method)
            logger.info("Request Headers: %s", safe_headers)
            logger.info("Response Status: %s", response.status_code)
            logger.info("Response Body: %s", response.text)

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    logger.error("Invalid JSON returned by Message Central on verify_otp")
                    return False, {"error": "Invalid response from Message Central"}

                logger.info(f"OTP verified successfully for ID {verification_id}")
                return True, data

            try:
                error_data = response.json()
            except ValueError:
                error_data = {"error": response.text}

            logger.error("Verification failed: %s - %s", response.status_code, error_data)
            return False, error_data

        except Exception as e:
            logger.exception("verify_otp failed")
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