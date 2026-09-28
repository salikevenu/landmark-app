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


# Bounded so a hung (not refused) Redis can't stall a request thread for long.
_REDIS_STORAGE_OPTIONS = {"socket_connect_timeout": 2, "socket_timeout": 2}

# Flask-Limiter's own logger. It logs a WARNING when it falls back to memory
# but only INFO when Redis comes back; both are worth an operator's
# attention, so the recovery line is promoted to WARNING as well.
_LIMITER_LOGGER_NAME = "flask-limiter"
_STORAGE_RECOVERED_MSG = "Rate limit storage recovered"


class _PromoteStorageRecovery(logging.Filter):
    def filter(self, record):
        if record.getMessage() == _STORAGE_RECOVERED_MSG:
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True


def _install_limiter_log_filter():
    lim_logger = logging.getLogger(_LIMITER_LOGGER_NAME)
    if not any(isinstance(f, _PromoteStorageRecovery) for f in lim_logger.filters):
        lim_logger.addFilter(_PromoteStorageRecovery())


def limiter_storage_uri():
    """Pick Flask-Limiter storage: Redis whenever REDIS_URL is set, else memory.

    No connectivity probe: Redis being down at boot and Redis going down
    later are handled the same way, by Flask-Limiter's in-memory fallback
    (in_memory_fallback_enabled in init_extensions). It applies each route's
    own limits against process memory while Redis is unreachable and
    switches back once Redis answers again.
    """
    return (os.getenv("REDIS_URL") or "").strip() or "memory://"


def init_extensions(app):
    global limiter, razor_client

    storage_uri = limiter_storage_uri()
    _install_limiter_log_filter()
    try:
        limiter = Limiter(
            key_func=get_remote_address,
            app=app,
            default_limits=[],
            storage_uri=storage_uri,
            storage_options=_REDIS_STORAGE_OPTIONS if storage_uri != "memory://" else {},
            strategy="fixed-window",
            # Fail open to memory, never to "no limits": NOT swallow_errors,
            # which would skip rate limiting entirely on a storage error.
            in_memory_fallback_enabled=True,
        )
    except Exception:
        # Storage could not even be constructed (bad URL scheme, redis
        # package missing). Connection failures never land here.
        logger.warning("rate limiter storage failed; using in-memory storage", exc_info=True)
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
