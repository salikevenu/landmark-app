# extensions.py
import os
import logging
import razorpay
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config.payment_config import get_razorpay_key_pair, log_razorpay_config

# Global clients
limiter = None
razor_client = None
logger = logging.getLogger(__name__)


def limiter_storage_uri():
    """Pick Flask-Limiter storage. Redis is optional and never blocks boot."""
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if not redis_url:
        return "memory://"
    try:
        import redis as redis_lib
        client = redis_lib.from_url(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=False,
        )
        client.ping()
        return redis_url
    except Exception:
        logger.warning(
            "REDIS_URL set but Redis is unreachable; rate limiter using in-memory storage"
        )
        return "memory://"


def init_extensions(app):
    global limiter, razor_client

    storage_uri = limiter_storage_uri()
    try:
        limiter = Limiter(
            key_func=get_remote_address,
            app=app,
            default_limits=[],
            storage_uri=storage_uri,
            strategy="fixed-window",
        )
    except Exception:
        logger.warning("rate limiter storage failed; using in-memory storage")
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
