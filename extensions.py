# extensions.py

import os
import razorpay
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ✅ GLOBAL VARIABLES
limiter = Limiter(key_func=get_remote_address)
razor_client = None   # ✅ ADD THIS

def init_extensions(app):
    global razor_client

    limiter.init_app(app)

    razor_client = razorpay.Client(auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    ))

    return limiter, razor_client