# app.py - LANDMARK Main Application with Multi-Agent System
from flask import Flask, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager
import logging
from datetime import timedelta
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize extensions
jwt = JWTManager()

def create_app(config=None):
    """Application factory pattern"""
    app = Flask(__name__)
    
    # Configuration
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')
    app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'jwt-dev-secret')
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)
    app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
    app.config['DATABASE_URL'] = os.getenv('DATABASE_URL', 'postgresql://localhost/landmark')
    app.config['RAZORPAY_KEY_ID'] = os.getenv('RAZORPAY_KEY_ID')
    app.config['RAZORPAY_KEY_SECRET'] = os.getenv('RAZORPAY_KEY_SECRET')
    app.config['SATURDAY_PAYOUT_SECRET'] = os.getenv('SATURDAY_PAYOUT_SECRET')
    app.config['BASE_URL'] = os.getenv('BASE_URL', 'http://localhost:5000')
    
    # Initialize extensions
    CORS(app)
    jwt.init_app(app)
    
    # Initialize Master Agent
    from master_agent import MasterAgent
    master_agent = MasterAgent(app)
    app.master_agent = master_agent
    
    # Health check endpoint
    @app.route('/health')
    def health():
        return jsonify({
            'status': 'healthy',
            'agents': app.master_agent.get_agent_status()
        })
    
    # Agent status endpoint
    @app.route('/api/agents/status')
    def agent_status():
        return jsonify(app.master_agent.get_agent_status())
    
    logging.info("LANDMARK Application initialized with Multi-Agent System")
    return app

# Create app instance
app = create_app()

if __name__ == '__main__':
    # Start scheduler if available
    try:
        scheduler = app.master_agent.agents.get('scheduler')
        if scheduler and hasattr(scheduler, 'start'):
            result = scheduler.start()
            logging.info(f"Scheduler started: {result}")
        else:
            logging.warning("Scheduler agent not available or missing start() method")
    except Exception as e:
        logging.error(f"Failed to start scheduler: {e}")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
