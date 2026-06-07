# services/payment_service.py

import datetime
from datetime import timedelta
from sqlalchemy import text

from extensions import razor_client
from config.payment_config import PLAN_PRICES
from services.wallet_service import credit_wallet, debit_wallet
from database.init_db import get_db


# =========================
# ACTIVATE SUBSCRIPTION
# =========================
def activate_subscription(phone, plan, days=30):
    expiry_date = (datetime.datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_db()
    conn.execute(text("""
        UPDATE users
        SET role = :plan,
            subscription_status = 'active',
            subscription_expiry = :expiry_date
        WHERE phone = :phone
    """), {
        "plan": plan,
        "expiry_date": expiry_date,
        "phone": phone
    })
    conn.commit()
    return expiry_date


# =========================
# PROCESS PAYMENT (internal helper)
# =========================
def process_payment(user_id, payment_id, amount_in_rupees):
    conn = get_db()
    try:
        conn.execute(text("BEGIN"))
        
        # Check for duplicate payment_id
        existing = conn.execute(
            text("SELECT id FROM payments WHERE payment_id = :payment_id"),
            {"payment_id": payment_id}
        ).fetchone()
        
        if existing:
            conn.execute(text("ROLLBACK"))
            return {"status": "duplicate"}

        # Insert payment record (amount in rupees)
        conn.execute(text("""
            INSERT INTO payments (user_id, payment_id, amount, status, created_at)
            VALUES (:user_id, :payment_id, :amount, :status, :created_at)
        """), {
            "user_id": user_id,
            "payment_id": payment_id,
            "amount": amount_in_rupees,
            "status": "verified",
            "created_at": datetime.datetime.utcnow()
        })
        
        conn.execute(text("COMMIT"))
    except Exception as e:
        conn.execute(text("ROLLBACK"))
        return {"error": str(e)}

    # Credit wallet after successful payment
    credit_wallet(user_id, amount_in_rupees, "Razorpay Payment", payment_id)
    return {"status": "success"}


# =========================
# VERIFY PAYMENT SERVICE
# =========================
def verify_payment_service(data, user_id):
    """
    data contains: razorpay_order_id, razorpay_payment_id, razorpay_signature, plan
    user_id is obtained from JWT token (authenticated user)
    """
    razorpay_order_id = data.get("razorpay_order_id")
    razorpay_payment_id = data.get("razorpay_payment_id")
    razorpay_signature = data.get("razorpay_signature")
    plan = data.get("plan")

    # 1. Required fields validation
    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature, plan]):
        return {"error": "Missing required fields"}

    # 2. Plan validation
    if plan not in PLAN_PRICES:
        return {"error": "Invalid plan selected"}

    expected_amount_paisa = PLAN_PRICES[plan]  # amount in paisa
    expected_amount_rupees = expected_amount_paisa / 100

    # 3. Signature verification
    try:
        razor_client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature
        })
    except Exception as e:
        return {"error": "Payment signature verification failed"}

    # 4. Fetch order from Razorpay
    try:
        order = razor_client.order.fetch(razorpay_order_id)
    except Exception as e:
        return {"error": "Unable to fetch order"}

    # 5. Amount validation (compare in paisa)
    if order["amount"] != expected_amount_paisa:
        return {"error": "Amount mismatch"}

    if order["status"] != "paid":
        return {"error": "Order not paid"}

    # 6. Get user phone (for subscription activation)
    conn = get_db()
    user_row = conn.execute(
        text("SELECT phone FROM users WHERE id = :user_id"),
        {"user_id": user_id}
    ).fetchone()
    
    if not user_row:
        return {"error": "User not found"}
    
    phone = user_row._mapping["phone"]

    # 7. Process payment (insert record + credit wallet)
    result = process_payment(user_id, razorpay_payment_id, expected_amount_rupees)
    
    if result.get("status") == "duplicate":
        return {"status": "success", "message": "Payment already processed"}
    
    if result.get("error"):
        return {"error": result["error"]}

    # 8. Debit wallet for subscription cost
    success = debit_wallet(user_id, expected_amount_rupees, f"{plan} subscription")
    
    if not success:
        return {"error": "Wallet deduction failed"}

    # 9. Activate subscription
    expiry_date = activate_subscription(phone, plan)

    # 10. Final success response
    return {
        "status": "success",
        "message": "Subscription activated ✅",
        "role": plan,
        "expiry": expiry_date,
        "redirect": "/dashboard"
    }