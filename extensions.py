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
    print("⚠️ Rate limiting DISABLED for testing")
    
    # 2. Razorpay client - FIXED
    key_id = os.getenv('RAZORPAY_KEY_ID')
    key_secret = os.getenv('RAZORPAY_KEY_SECRET')
    
    if key_id and key_secret:
        razor_client = razorpay.Client(auth=(key_id, key_secret))
        print(f"✅ Razorpay initialized with key: {key_id[:10]}...")
    else:
        razor_client = None
        print("❌ Razorpay keys not found in environment!")
    
    return limiter, razor_client

def get_razorpay_client():
    key_id = os.getenv('RAZORPAY_KEY_ID')
    key_secret = os.getenv('RAZORPAY_KEY_SECRET')
    if key_id and key_secret:
        return razorpay.Client(auth=(key_id, key_secret))
    return None