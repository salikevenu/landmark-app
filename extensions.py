# extensions.py
import os
import razorpay

razor_client = None

def init_extensions(app):
    global razor_client
    razor_client = razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))
    # Return only the razor client (limiter is now in app.py)
    return razor_client

def get_razorpay_client():
    return razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))