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

    # 1. Rate limiter - COMPLETELY DISABLED FOR TESTING
    # Create a dummy limiter that does nothing
    class DummyLimiter:
        def limit(self, *args, **kwargs):
            return lambda x: x
        
        def init_app(self, app):
            pass
    
    limiter = DummyLimiter()
    print("⚠️ Rate limiting DISABLED for testing")

    # 2. Razorpay client
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