# auth/otp_service.py
"""LEGACY / DISABLED. Live OTP is routes.auth_routes + services.sms_service."""
import logging

logger = logging.getLogger(__name__)

otp_storage = {}


def send_otp(phone):
    logger.error("LEGACY DISABLED: auth.otp_service.send_otp must not send SMS")
    return False


def verify_otp(phone, user_otp):
    logger.error("LEGACY DISABLED: auth.otp_service.verify_otp is not the live OTP path")
    return False
