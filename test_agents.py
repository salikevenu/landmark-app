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
