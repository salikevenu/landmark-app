# routes/payment.py
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from datetime import datetime
import hmac
import hashlib
import os
from sqlalchemy import text

from extensions import get_razorpay_client
from config.payment_config import PLAN_PRICES, RAZORPAY_KEY_ID
from services.payment_service import verify_payment_service
from database.init_db import get_db
from services.referral_commission import process_referral_commission

payment_bp = Blueprint("payment", __name__)

# ================================
# Create Razorpay Order
# ================================
@payment_bp.route("/create-order", methods=["POST"])
#@jwt_required(optional=True)  # Make it optional for testing
user_id = "test-user-123" # Temporary hardcoded ID
def create_order():
    try:
        # Get user ID (may be None if not authenticated)
        user_id = get_jwt_identity()
        
        # For testing, allow without auth
        if not user_id:
            user_id = request.json.get("user_id", "test_user_001")
            print(f"⚠️ No JWT token, using test user: {user_id}")
        
        data = request.json
        plan = data.get("plan")
        
        # Make sure PLAN_PRICES is defined
        PLAN_PRICES = {
            "Business Basic": 49900,   # ₹499 in paise
            "Business Premium": 199900  # ₹1999 in paise
        }
        
        if plan not in PLAN_PRICES:
            return jsonify({"error": f"Invalid plan: {plan}. Allowed: {list(PLAN_PRICES.keys())}"}), 400
        
        amount = PLAN_PRICES[plan]
        
        # Get Razorpay client
        from extensions import get_razorpay_client
        client = get_razorpay_client()
        
        if not client:
            return jsonify({"error": "Razorpay client not initialized. Check API keys."}), 500
        
        # Create order
        order = client.order.create({
            "amount": amount,
            "currency": "INR",
            "payment_capture": 1
        })
        
        # Save to database
        from database.init_db import get_db
        from sqlalchemy import text
        from datetime import datetime
        
        conn = get_db()
        conn.execute(text("""
            INSERT INTO payments (user_id, payment_id, amount, status, created_at)
            VALUES (:user_id, :payment_id, :amount, :status, :created_at)
        """), {
            "user_id": user_id,
            "payment_id": order["id"],
            "amount": amount,
            "status": "created",
            "created_at": datetime.utcnow()
        })
        conn.commit()
        
        # Get key ID from environment
        import os
        razorpay_key_id = os.getenv("RAZORPAY_KEY_ID")
        
        return jsonify({
            "order_id": order["id"],
            "key": razorpay_key_id,
            "amount": amount,
            "currency": "INR"
        })
        
    except Exception as e:
        print(f"❌ Order creation error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "type": type(e).__name__}), 500

@payment_bp.route("/create-order-debug", methods=["POST"])
def create_order_debug():
    """Debug endpoint - no JWT required"""
    try:
        data = request.json
        plan = data.get("plan")
        
        PLAN_PRICES = {
            "Business Basic": 49900,
            "Business Premium": 199900
        }
        
        if plan not in PLAN_PRICES:
            return jsonify({"error": f"Invalid plan: {plan}"}), 400
        
        amount = PLAN_PRICES[plan]
        
        client = get_razorpay_client()
        if not client:
            return jsonify({"error": "Razorpay client not initialized"}), 500
        
        order = client.order.create({
            "amount": amount,
            "currency": "INR",
            "payment_capture": 1
        })
        
        return jsonify({
            "order_id": order["id"],
            "key": os.getenv("RAZORPAY_KEY_ID"),
            "amount": amount,
            "currency": "INR"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================================
# Wallet Balance
# ================================
@payment_bp.route("/wallet", methods=["GET"])
@jwt_required()
def wallet_balance():
    user_id = get_jwt_identity()
    conn = get_db()
    row = conn.execute(
        text("SELECT balance FROM wallet_balance WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchone()
    balance = row._mapping["balance"] if row else 0
    return jsonify({"wallet_balance": balance})


# ================================
# Wallet Transactions
# ================================
@payment_bp.route("/wallet-transactions", methods=["GET"])
@jwt_required()
def wallet_transactions():
    user_id = get_jwt_identity()
    conn = get_db()
    rows = conn.execute(text("""
        SELECT * FROM wallet_transactions
        WHERE user_id = :uid
        ORDER BY created_at DESC
    """), {"uid": user_id}).fetchall()
    return jsonify([dict(r._mapping) for r in rows])


# ================================
# Verify Payment - FIXED
# ================================
@payment_bp.route("/verify-payment", methods=["POST"])
@jwt_required()
def verify_payment():
    user_id = get_jwt_identity()  # ← GET USER ID FROM JWT
    data = request.json
    result = verify_payment_service(data, user_id)  # ← PASS USER ID
    return jsonify(result)  # ← ALWAYS RETURN JSON


# ================================
# Razorpay Webhook
# ================================
WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

@payment_bp.route("/razorpay/webhook", methods=["POST"])
def razorpay_webhook():
    payload = request.data
    signature = request.headers.get("X-Razorpay-Signature")

    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return {"error": "Invalid webhook signature"}, 400

    data = request.json
    try:
        payment_entity = data["payload"]["payment"]["entity"]
        payment_id = payment_entity["id"]
        amount = payment_entity["amount"] / 100
        phone = payment_entity.get("contact")
    except Exception:
        return {"error": "Invalid payload"}, 400

    from services.payment_service import process_payment

    conn = get_db()
    user_row = conn.execute(
        text("SELECT id FROM users WHERE phone = :phone"),
        {"phone": phone}
    ).fetchone()
    if not user_row:
        return {"error": "User not found"}, 404

    user_id = user_row._mapping["id"]
    result = process_payment(user_id, payment_id, amount)

    if payment_entity.get("status") == "captured":
        process_referral_commission(user_id, amount)

    return {"status": result.get("status", "error")}


# ================================
# Submit Manual Payment Proof
# ================================
@payment_bp.route("/submit-payment-proof", methods=["POST"])
def submit_payment_proof():
    data = request.json
    phone = data.get("phone")
    plan = data.get("plan")
    reference_id = data.get("reference_id")

    if plan not in PLAN_PRICES:
        return {"error": "Invalid plan"}, 400

    conn = get_db()
    conn.execute(text("""
        INSERT INTO payments (phone, plan, amount, status, payment_method, reference_id)
        VALUES (:phone, :plan, :amount, :status, :payment_method, :reference_id)
    """), {
        "phone": phone,
        "plan": plan,
        "amount": PLAN_PRICES[plan],
        "status": "pending",
        "payment_method": "manual",
        "reference_id": reference_id
    })
    conn.commit()

    return {
        "status": "Payment proof submitted",
        "message": "Waiting for admin approval"
    }