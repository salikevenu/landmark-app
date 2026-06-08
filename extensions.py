# extensions.py
import os
import razorpay
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Global limiter (to be initialized later)
limiter = None
razor_client = None

def init_extensions(app):
    global limiter, razor_client

    # 1. Rate limiter (Redis or memory)
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        limiter = Limiter(
            get_remote_address,
            storage_uri=redis_url,
            default_limits=["200 per day", "50 per hour"]
        )
    else:
#        limiter = Limiter(
#            get_remote_address,
#            default_limits=["200 per day", "50 per hour"]
#        )
#    limiter.init_app(app)

    # 2. Razorpay
    razor_client = razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))

    return limiter, razor_client

def get_razorpay_client():
    return razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))