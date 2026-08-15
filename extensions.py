# extensions.py
import os
import razorpay
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

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

    # 2. Razorpay client - DIRECT INITIALIZATION
    key_id = os.environ.get('RAZORPAY_KEY_ID')
    key_secret = os.environ.get('RAZORPAY_KEY_SECRET')
    
    print(f"DEBUG: RAZORPAY_KEY_ID exists: {bool(key_id)}")
    print(f"DEBUG: RAZORPAY_KEY_SECRET exists: {bool(key_secret)}")
    
    if key_id and key_secret:
        razor_client = razorpay.Client(auth=(key_id, key_secret))
        print(f"Razorpay initialized with key: {key_id[:15]}...")
    else:
        razor_client = None
        print("ERROR: Razorpay keys missing! Check Render environment variables.")
    
    return limiter, razor_client

def get_razorpay_client():
    key_id = os.environ.get('RAZORPAY_KEY_ID')
    key_secret = os.environ.get('RAZORPAY_KEY_SECRET')
    if key_id and key_secret:
        return razorpay.Client(auth=(key_id, key_secret))
    return None
