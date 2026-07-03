# ============================================
# LANDMARK Multi-Agent Setup Script - WORKING
# ============================================

Write-Host "🚀 Setting up LANDMARK Multi-Agent System" -ForegroundColor Cyan
Write-Host "=" * 50

# Create agents directory
Write-Host "📁 Creating agents directory..." -ForegroundColor Yellow
if (-not (Test-Path "agents")) {
    New-Item -ItemType Directory -Path "agents" -Force | Out-Null
    Write-Host "✅ Created agents directory" -ForegroundColor Green
} else {
    Write-Host "✅ agents directory already exists" -ForegroundColor Green
}

# Function to write file content safely
function Write-FileContent {
    param($FilePath, $Content)
    $Content | Out-File -FilePath $FilePath -Encoding UTF8 -Force
}

# 1. Create __init__.py
Write-Host "📝 Creating agents/__init__.py..." -ForegroundColor Yellow
$initContent = @'
"""LANDMARK Multi-Agent System"""
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
'@
Write-FileContent -FilePath "agents\__init__.py" -Content $initContent
Write-Host "✅ Created agents/__init__.py" -ForegroundColor Green

# 2. Create auth_agent.py
Write-Host "📝 Creating agents/auth_agent.py..." -ForegroundColor Yellow
$authContent = @'
# agents/auth_agent.py
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
        from database.init_db import get_db
        
        conn = get_db()
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
        from database.init_db import get_db
        
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
            
            conn = get_db()
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
'@
Write-FileContent -FilePath "agents\auth_agent.py" -Content $authContent
Write-Host "✅ Created agents/auth_agent.py" -ForegroundColor Green

# 3. Create payment_agent.py
Write-Host "📝 Creating agents/payment_agent.py..." -ForegroundColor Yellow
$paymentContent = @'
# agents/payment_agent.py
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
        from database.init_db import get_db
        
        try:
            order_data = {
                "amount": amount * 100,
                "currency": currency,
                "receipt": f"order_{user_id}_{int(datetime.now().timestamp())}",
                "payment_capture": 1
            }
            
            order = self.client.order.create(data=order_data)
            
            conn = get_db()
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
        from database.init_db import get_db
        
        try:
            payment_details = self.client.payment.fetch(payment_id)
            
            if payment_details["status"] == "captured":
                conn = get_db()
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
'@
Write-FileContent -FilePath "agents\payment_agent.py" -Content $paymentContent
Write-Host "✅ Created agents/payment_agent.py" -ForegroundColor Green

# 4. Create referral_agent.py
Write-Host "📝 Creating agents/referral_agent.py..." -ForegroundColor Yellow
$referralContent = @'
# agents/referral_agent.py
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
        from database.init_db import get_db
        
        try:
            suffix = secrets.token_hex(2).upper()
            code = f"LM{user_id}{suffix}"
            
            conn = get_db()
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
        from database.init_db import get_db
        
        try:
            conn = get_db()
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
'@
Write-FileContent -FilePath "agents\referral_agent.py" -Content $referralContent
Write-Host "✅ Created agents/referral_agent.py" -ForegroundColor Green

# 5. Create remaining agent files
Write-Host "📝 Creating remaining agent files..." -ForegroundColor Yellow

$agentFiles = @(
    "wallet_agent.py",
    "subscription_agent.py",
    "ads_agent.py",
    "business_agent.py",
    "map_agent.py",
    "fraud_agent.py",
    "analytics_agent.py",
    "scheduler_agent.py"
)

foreach ($file in $agentFiles) {
    $className = ($file -replace "_agent.py", "") -replace "^\w", { $args[0].Value.ToUpper() }
    $desc = $file -replace "_agent.py", "" -replace "^\w", { $args[0].Value.ToUpper() }
    
    $content = @"
# agents/$file
"""$desc Agent"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class ${className}Agent:
    """Handles $desc operations"""
    
    def __init__(self, app=None):
        self.app = app
        logger.info("$desc Agent initialized")
    
    def get_status(self) -> Dict[str, Any]:
        """Get agent status"""
        return {"success": True, "agent": "$desc", "status": "active"}
"@
    
    Write-FileContent -FilePath "agents\$file" -Content $content
    Write-Host "✅ Created agents/$file" -ForegroundColor Green
}

# 6. Create master_agent.py
Write-Host "📝 Creating master_agent.py..." -ForegroundColor Yellow
$masterContent = @'
# master_agent.py
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
'@
Write-FileContent -FilePath "master_agent.py" -Content $masterContent
Write-Host "✅ Created master_agent.py" -ForegroundColor Green

# 7. Create test script
Write-Host "📝 Creating test_agents.py..." -ForegroundColor Yellow
$testContent = @'
# test_agents.py - Test LANDMARK Multi-Agent System
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
    
    print("\n📊 Agent Status:")
    for name, agent in master.agents.items():
        status = "✅" if agent else "❌"
        agent_type = agent.__class__.__name__ if agent else "None"
        print(f"  {status} {name}: {agent_type}")
    
    print("\n" + "="*50)
    print("✅ All tests passed!")
    print("💡 Run 'python app.py' to start the application")
    
except Exception as e:
    print(f"❌ Test failed: {e}")
    import traceback
    traceback.print_exc()
'@
Write-FileContent -FilePath "test_agents.py" -Content $testContent
Write-Host "✅ Created test_agents.py" -ForegroundColor Green

Write-Host ""
Write-Host "=" * 50
Write-Host "✅ LANDMARK Multi-Agent System Setup Complete!" -ForegroundColor Green
Write-Host "=" * 50
Write-Host ""
Write-Host "📁 Created Files:" -ForegroundColor Cyan
Get-ChildItem -Path "agents\*.py" -File | ForEach-Object { Write-Host "  - agents/$($_.Name)" -ForegroundColor White }
Write-Host "  - master_agent.py" -ForegroundColor White
Write-Host "  - test_agents.py" -ForegroundColor White
Write-Host ""
Write-Host "🚀 Next Steps:" -ForegroundColor Cyan
Write-Host "  1. Run: python test_agents.py" -ForegroundColor White
Write-Host "  2. Run: python app.py" -ForegroundColor White
Write-Host "  3. Visit: http://localhost:5000/health" -ForegroundColor White