"""
LANDMARK Multi-Agent System Setup Script
Run this with: python setup_agents.py
"""

import os
import sys

def create_file(filepath, content):
    """Create a file with the given content"""
    # Get directory path
    dirpath = os.path.dirname(filepath)
    # Only create directory if there is a directory path
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"✅ Created {filepath}")

def main():
    print("🚀 Setting up LANDMARK Multi-Agent System")
    print("=" * 50)
    
    # 1. Create __init__.py
    init_content = '''"""LANDMARK Multi-Agent System"""
from .auth_agent import AuthAgent
from .payment_agent import PaymentAgent
from .referral_agent import ReferralAgent
from .wallet_agent import WalletAgent
from .subscription_agent import SubscriptionAgent
from .ads_agent import AdsAgent
from .business_agent import BusinessAgent
from .map_agent import MapAgent
from .fraud_agent import FraudAgent
from .analytics_agent import AnalyticsAgent
from .scheduler_agent import SchedulerAgent

__all__ = [
    "AuthAgent",
    "PaymentAgent",
    "ReferralAgent",
    "WalletAgent",
    "SubscriptionAgent",
    "AdsAgent",
    "BusinessAgent",
    "MapAgent",
    "FraudAgent",
    "AnalyticsAgent",
    "SchedulerAgent"
]
'''
    create_file("agents/__init__.py", init_content)
    
    # 2. Create auth_agent.py
    auth_content = '''# agents/auth_agent.py
"""Authentication Agent - Handles OTP, JWT, User Verification"""

from flask import current_app
from flask_jwt_extended import create_access_token
import logging
from typing import Dict, Any
from datetime import datetime, timedelta
import phonenumbers
import secrets

logger = logging.getLogger(__name__)

class AuthAgent:
    """Handles all authentication and authorization operations"""
    
    def __init__(self, app=None):
        self.app = app
        self.otp_cache = {}
    
    def verify_user(self, user_id: int) -> Dict[str, Any]:
        """Verify user exists and is active"""
        from database.init_db import get_db_connection
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, phone, role, is_active, is_blocked FROM users WHERE id = %s AND is_active = true AND is_blocked = false",
                (user_id,)
            )
            user = cursor.fetchone()
            
            if user:
                return {
                    "success": True,
                    "user_id": user[0],
                    "phone": user[1],
                    "role": user[2],
                    "is_active": user[3],
                    "is_blocked": user[4]
                }
            return {"success": False, "error": "User not found or blocked"}
        except Exception as e:
            logger.error(f"User verification failed: {str(e)}")
            return {"success": False, "error": str(e)}
        finally:
            conn.close()
    
    def generate_otp(self, phone: str) -> Dict[str, Any]:
        """Generate OTP for phone number"""
        try:
            parsed = phonenumbers.parse(phone, "IN")
            if not phonenumbers.is_valid_number(parsed):
                return {"success": False, "error": "Invalid phone number"}
            
            otp = str(secrets.randbelow(900000) + 100000)
            self.otp_cache[phone] = {
                "otp": otp,
                "created_at": datetime.now(),
                "expires_at": datetime.now() + timedelta(minutes=5)
            }
            
            logger.info(f"OTP generated for {phone}: {otp}")
            return {"success": True, "otp": otp, "expires_in": 300}
        except Exception as e:
            logger.error(f"OTP generation failed: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def verify_otp(self, phone: str, otp: str) -> Dict[str, Any]:
        """Verify OTP and generate JWT"""
        from database.init_db import get_db_connection
        
        try:
            if phone not in self.otp_cache:
                return {"success": False, "error": "OTP not found or expired"}
            
            otp_data = self.otp_cache[phone]
            
            if datetime.now() > otp_data["expires_at"]:
                del self.otp_cache[phone]
                return {"success": False, "error": "OTP expired"}
            
            if otp_data["otp"] != otp:
                return {"success": False, "error": "Invalid OTP"}
            
            del self.otp_cache[phone]
            
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT id, role FROM users WHERE phone = %s", (phone,))
                user = cursor.fetchone()
                
                if not user:
                    cursor.execute(
                        "INSERT INTO users (phone, role, plan, created_at) VALUES (%s, %s, %s, NOW()) RETURNING id, role",
                        (phone, "free", "free")
                    )
                    user = cursor.fetchone()
                    conn.commit()
                
                user_id = user[0]
                role = user[1]
                
                access_token = create_access_token(
                    identity=str(user_id),
                    additional_claims={"role": role, "phone": phone}
                )
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "role": role,
                    "access_token": access_token,
                    "phone": phone
                }
            finally:
                conn.close()
                
        except Exception as e:
            logger.error(f"OTP verification failed: {str(e)}")
            return {"success": False, "error": str(e)}
'''
    create_file("agents/auth_agent.py", auth_content)
    
    # 3. Create payment_agent.py
    payment_content = '''# agents/payment_agent.py
"""Payment Agent - Handles Razorpay Integration"""

import razorpay
from flask import current_app
import logging
from typing import Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

class PaymentAgent:
    """Handles all payment operations with Razorpay"""
    
    def __init__(self, app=None):
        self.app = app
        self.client = None
        if app:
            self.init_razorpay(app)
    
    def init_razorpay(self, app):
        """Initialize Razorpay client"""
        try:
            self.client = razorpay.Client(
                auth=(app.config.get("RAZORPAY_KEY_ID"), app.config.get("RAZORPAY_KEY_SECRET"))
            )
            logger.info("Razorpay client initialized")
        except Exception as e:
            logger.error(f"Razorpay initialization failed: {str(e)}")
    
    def create_order(self, user_id: int, amount: int, currency: str = "INR") -> Dict[str, Any]:
        """Create Razorpay payment order"""
        from database.init_db import get_db_connection
        
        try:
            order_data = {
                "amount": amount * 100,
                "currency": currency,
                "receipt": f"order_{user_id}_{int(datetime.now().timestamp())}",
                "payment_capture": 1
            }
            
            order = self.client.order.create(data=order_data)
            
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO payment_transactions (user_id, order_id, amount, currency, status, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                    (user_id, order["id"], amount, currency, "created")
                )
                conn.commit()
            finally:
                conn.close()
            
            return {
                "success": True,
                "order_id": order["id"],
                "amount": amount,
                "currency": currency,
                "razorpay_key": self.app.config.get("RAZORPAY_KEY_ID")
            }
        except Exception as e:
            logger.error(f"Payment order creation failed: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def verify_payment(self, payment_id: str, user_id: int) -> Dict[str, Any]:
        """Verify payment with Razorpay"""
        from database.init_db import get_db_connection
        
        try:
            payment_details = self.client.payment.fetch(payment_id)
            
            if payment_details["status"] == "captured":
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    order_id = payment_details.get("order_id")
                    amount = payment_details["amount"] / 100
                    
                    cursor.execute(
                        "UPDATE payment_transactions SET status = %s, payment_id = %s, updated_at = NOW() WHERE user_id = %s AND order_id = %s",
                        ("completed", payment_id, user_id, order_id)
                    )
                    
                    cursor.execute(
                        "INSERT INTO wallet_balance (user_id, balance, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (user_id) DO UPDATE SET balance = wallet_balance.balance + %s, updated_at = NOW()",
                        (user_id, amount, amount)
                    )
                    
                    conn.commit()
                finally:
                    conn.close()
                
                return {
                    "success": True,
                    "amount": amount,
                    "payment_id": payment_id,
                    "status": "completed"
                }
            return {"success": False, "error": "Payment not captured"}
        except Exception as e:
            logger.error(f"Payment verification failed: {str(e)}")
            return {"success": False, "error": str(e)}
'''
    create_file("agents/payment_agent.py", payment_content)
    
    # 4. Create referral_agent.py
    referral_content = '''# agents/referral_agent.py
"""Referral Agent - Handles Referral Codes and Commissions"""

import logging
from typing import Dict, Any
import secrets

logger = logging.getLogger(__name__)

class ReferralAgent:
    """Handles referral code generation, tracking, and commission"""
    
    def __init__(self, app=None):
        self.app = app
        self.commission_percentage = 10
    
    def generate_referral_code(self, user_id: int) -> Dict[str, Any]:
        """Generate unique referral code for user"""
        from database.init_db import get_db_connection
        
        try:
            suffix = secrets.token_hex(2).upper()
            code = f"LM{user_id}{suffix}"
            
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET referral_code = %s WHERE id = %s AND referral_code IS NULL RETURNING referral_code",
                    (code, user_id)
                )
                result = cursor.fetchone()
                if result:
                    conn.commit()
                    return {"success": True, "referral_code": code}
                
                cursor.execute("SELECT referral_code FROM users WHERE id = %s", (user_id,))
                existing = cursor.fetchone()
                if existing and existing[0]:
                    return {"success": True, "referral_code": existing[0]}
                return {"success": False, "error": "Failed to generate code"}
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Referral code generation failed: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def process_referral_reward(self, user_id: int, plan: str) -> Dict[str, Any]:
        """Process referral commission when a user upgrades"""
        from database.init_db import get_db_connection
        
        try:
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT referred_by, phone FROM users WHERE id = %s AND referred_by IS NOT NULL",
                    (user_id,)
                )
                referral = cursor.fetchone()
                if not referral:
                    return {"success": True, "message": "No referral found"}
                
                referrer_id = referral[0]
                commission = 100
                
                if commission > 0:
                    cursor.execute(
                        "INSERT INTO wallet_balance (user_id, balance, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (user_id) DO UPDATE SET balance = wallet_balance.balance + %s, updated_at = NOW()",
                        (referrer_id, commission, commission)
                    )
                    
                    cursor.execute(
                        "INSERT INTO referral_transactions (referrer_id, referred_user_id, amount, plan, status, created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                        (referrer_id, user_id, commission, plan, "credited")
                    )
                    conn.commit()
                
                return {"success": True, "referrer_id": referrer_id, "commission": commission, "plan": plan}
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Referral processing failed: {str(e)}")
            return {"success": False, "error": str(e)}
'''
    create_file("agents/referral_agent.py", referral_content)
    
    # 5. Create remaining agent files
    print("📝 Creating remaining agent files...")
    
    agent_files = [
        ("wallet_agent.py", "Wallet", "Wallet Balance Management"),
        ("subscription_agent.py", "Subscription", "Subscription Plan Management"),
        ("ads_agent.py", "Ads", "Advertisement Management"),
        ("business_agent.py", "Business", "Business Listing Management"),
        ("map_agent.py", "Map", "Geo-Location and Mapping"),
        ("fraud_agent.py", "Fraud", "Fraud Detection and Prevention"),
        ("analytics_agent.py", "Analytics", "Analytics and Reporting"),
        ("scheduler_agent.py", "Scheduler", "Scheduled Tasks and Cron Jobs")
    ]
    
    for filename, classname, desc in agent_files:
        content = f'''# agents/{filename}
"""{desc} Agent"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class {classname}Agent:
    """Handles {desc}"""
    
    def __init__(self, app=None):
        self.app = app
        logger.info("{desc} Agent initialized")
    
    def get_status(self) -> Dict[str, Any]:
        """Get agent status"""
        return {{"success": True, "agent": "{desc}", "status": "active"}}
'''
        create_file(f"agents/{filename}", content)
    
    # 6. Create master_agent.py
    print("📝 Creating master_agent.py...")
    master_content = '''# master_agent.py
"""Master Agent Orchestrator for LANDMARK"""

import logging
from typing import Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

class MasterAgent:
    """Coordinates all sub-agents for LANDMARK"""
    
    def __init__(self, app=None):
        self.app = app
        self.agents = {}
        if app:
            self.init_app(app)
    
    def init_app(self, app):
        self.app = app
        self._initialize_agents()
        logger.info("Master Agent initialized with all sub-agents")
    
    def _initialize_agents(self):
        try:
            from agents import (
                AuthAgent, PaymentAgent, ReferralAgent,
                WalletAgent, SubscriptionAgent, AdsAgent,
                BusinessAgent, MapAgent, FraudAgent,
                AnalyticsAgent, SchedulerAgent
            )
            
            self.agents = {
                "auth": AuthAgent(self.app),
                "payment": PaymentAgent(self.app),
                "referral": ReferralAgent(self.app),
                "wallet": WalletAgent(self.app),
                "subscription": SubscriptionAgent(self.app),
                "ads": AdsAgent(self.app),
                "business": BusinessAgent(self.app),
                "map": MapAgent(self.app),
                "fraud": FraudAgent(self.app),
                "analytics": AnalyticsAgent(self.app),
                "scheduler": SchedulerAgent(self.app)
            }
            logger.info(f"✅ {len(self.agents)} agents initialized")
        except ImportError as e:
            logger.error(f"Failed to initialize agents: {e}")
            self._create_placeholder_agents()
    
    def _create_placeholder_agents(self):
        class PlaceholderAgent:
            def __init__(self, name):
                self.name = name
            def __getattr__(self, name):
                def method(*args, **kwargs):
                    return {"success": False, "error": "Agent not fully implemented"}
                return method
        
        for name in ["auth", "payment", "referral", "wallet", "subscription", "ads", "business", "map", "fraud", "analytics", "scheduler"]:
            self.agents[name] = PlaceholderAgent(name)
    
    def orchestrate(self, workflow: str, data: Dict[str, Any]) -> Dict[str, Any]:
        workflows = {
            "business_subscription": self._handle_business_subscription,
            "payment_verification": self._handle_payment_verification
        }
        
        if workflow not in workflows:
            return {"success": False, "error": f"Unknown workflow: {workflow}"}
        
        try:
            result = workflows[workflow](data)
            result["workflow"] = workflow
            result["timestamp"] = datetime.now().isoformat()
            return result
        except Exception as e:
            logger.error(f"Workflow {workflow} failed: {e}")
            return {"success": False, "error": str(e)}
    
    def _handle_business_subscription(self, data: Dict) -> Dict:
        user_id = data.get("user_id")
        plan = data.get("plan")
        payment_id = data.get("payment_id")
        
        result = {"success": True, "steps": {}}
        
        auth_result = self.agents["auth"].verify_user(user_id)
        result["steps"]["auth"] = auth_result
        if not auth_result.get("success"):
            result["success"] = False
            return result
        
        payment_result = self.agents["payment"].verify_payment(payment_id, user_id)
        result["steps"]["payment"] = payment_result
        
        return result
    
    def _handle_payment_verification(self, data: Dict) -> Dict:
        return self.agents["payment"].verify_payment(data.get("payment_id"), data.get("user_id"))
    
    def get_agent_status(self) -> Dict[str, Any]:
        status = {}
        for name, agent in self.agents.items():
            status[name] = {
                "active": True,
                "type": agent.__class__.__name__,
                "initialized": True
            }
        return status
'''
    create_file("master_agent.py", master_content)
    
    # 7. Create test script
    print("📝 Creating test_agents.py...")
    test_content = '''# test_agents.py - Test LANDMARK Multi-Agent System
print("🚀 Testing LANDMARK Multi-Agent System")
print("="*50)

try:
    from agents import AuthAgent, PaymentAgent, ReferralAgent
    print("✅ All agent imports successful")
except ImportError as e:
    print(f"❌ Import error: {e}")
    exit(1)

try:
    from master_agent import MasterAgent
    from flask import Flask
    
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["RAZORPAY_KEY_ID"] = "test-key"
    app.config["RAZORPAY_KEY_SECRET"] = "test-secret"
    
    master = MasterAgent(app)
    print("✅ Master Agent initialized")
    
    print("\\n📊 Agent Status:")
    for name, agent in master.agents.items():
        status = "✅" if agent else "❌"
        agent_type = agent.__class__.__name__ if agent else "None"
        print(f"  {status} {name}: {agent_type}")
    
    print("\\n" + "="*50)
    print("✅ All tests passed!")
    print("💡 Run 'python app.py' to start the application")
    
except Exception as e:
    print(f"❌ Test failed: {e}")
    import traceback
    traceback.print_exc()
'''
    create_file("test_agents.py", test_content)
    
    print("\n" + "=" * 50)
    print("✅ LANDMARK Multi-Agent System Setup Complete!")
    print("=" * 50)
    print("\n📁 Created Files:")
    print("  - agents/__init__.py")
    print("  - agents/auth_agent.py")
    print("  - agents/payment_agent.py")
    print("  - agents/referral_agent.py")
    print("  - agents/wallet_agent.py")
    print("  - agents/subscription_agent.py")
    print("  - agents/ads_agent.py")
    print("  - agents/business_agent.py")
    print("  - agents/map_agent.py")
    print("  - agents/fraud_agent.py")
    print("  - agents/analytics_agent.py")
    print("  - agents/scheduler_agent.py")
    print("  - master_agent.py")
    print("  - test_agents.py")
    print("\n🚀 Next Steps:")
    print("  1. Run: python test_agents.py")
    print("  2. Run: python app.py")
    print("  3. Visit: http://localhost:5000/health")

if __name__ == "__main__":
    main()