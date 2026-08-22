# routes/orchestration_routes.py
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from master_agent import MasterAgent
from middleware.admin_required import admin_required
import logging

orchestration_bp = Blueprint('orchestration', __name__, url_prefix='/api/orchestrate')
master_agent = MasterAgent()

logger = logging.getLogger(__name__)

@orchestration_bp.route('/workflow/subscription', methods=['POST'])
@jwt_required()
def execute_subscription_workflow():
    """Disabled: must not activate subscriptions outside canonical payment verify."""
    return jsonify({
        "success": False,
        "error": "This endpoint is disabled. Use POST /api/payment/verify-payment",
        "canonical": "/api/payment/verify-payment",
    }), 410

@orchestration_bp.route('/workflow/fraud-check', methods=['POST'])
@admin_required
def execute_fraud_check():
    return jsonify({"success": False, "error": "This endpoint is disabled"}), 410

@orchestration_bp.route('/workflow/daily-maintenance', methods=['POST'])
@admin_required
def execute_daily_maintenance():
    return jsonify({"success": False, "error": "This endpoint is disabled"}), 410

@orchestration_bp.route('/agents/status', methods=['GET'])
@admin_required
def get_agents_status():
    """Get status of all agents"""
    status = {}
    for name, agent in master_agent.agents.items():
        status[name] = {
            'active': True,
            'type': agent.__class__.__name__
        }
    
    return jsonify(status), 200