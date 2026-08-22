# extensions.py
import razorpay
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config.payment_config import get_razorpay_key_pair, log_razorpay_config

# Global clients
limiter = None
razor_client = None


def init_extensions(app):
    global limiter, razor_client

    # In-memory storage: counters reset on process restart and are not shared
    # across workers. Move to Redis (storage_uri="redis://...") later —
    # same caveat as otp_storage in auth/otp_service.py.
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://",
        strategy="fixed-window",
    )

    key_id, key_secret = get_razorpay_key_pair()
    log_razorpay_config()
    if key_id and key_secret:
        razor_client = razorpay.Client(auth=(key_id, key_secret))
        logger = __import__("logging").getLogger(__name__)
        logger.info("Razorpay client initialized")
    else:
        razor_client = None
        print("WARNING: Razorpay keys missing (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET). Payments disabled.")

    return limiter, razor_client


def get_razorpay_client():
    key_id, key_secret = get_razorpay_key_pair()
    if key_id and key_secret:
        return razorpay.Client(auth=(key_id, key_secret))
    return None
