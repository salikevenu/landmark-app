# extensions.py
import os
import razorpay

# Global clients
limiter = None
razor_client = None

def init_extensions(app):
    global limiter, razor_client
    
    # 1. Rate limiter - DISABLED
    class DummyLimiter:
        def limit(self, *args, **kwargs):
            return lambda x: x
        def init_app(self, app):
            pass
    
    limiter = DummyLimiter()
    print("WARNING: Rate limiting DISABLED for testing")
    
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