import random
import requests

otp_storage = {}  # TEMP (use Redis/DB in production)

API_KEY = "YOUR_FAST2SMS_API_KEY"

def send_otp(phone):
    otp = str(random.randint(100000, 999999))

    url = "https://www.fast2sms.com/dev/bulkV2"

    headers = {
        'authorization': API_KEY,
        'Content-Type': "application/x-www-form-urlencoded"
    }

    data = {
        'variables_values': otp,
        'route': 'otp',
        'numbers': phone,
    }

    requests.post(url, data=data, headers=headers)

    # store OTP
    otp_storage[phone] = otp

    return True


def verify_otp(phone, user_otp):
    real_otp = otp_storage.get(phone)

    if real_otp and real_otp == user_otp:
        del otp_storage[phone]  # delete after success
        return True
    return False