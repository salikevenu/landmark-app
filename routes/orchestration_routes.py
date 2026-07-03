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
    """Execute subscription workflow"""
    user_id = int(get_jwt_identity())
    data = request.get_json()
    
    plan = data.get('plan')
    payment_id = data.get('payment_id')
    
    if not plan or not payment_id:
        return jsonify({'error': 'Missing plan or payment_id'}), 400
    
    result = master_agent.orchestrate_workflow(
        'business_subscription',
        {
            'user_id': user_id,
            'plan': plan,
            'payment_id': payment_id
        }
    )
    
    return jsonify(result), 200 if result['status'] == 'completed' else 400

@orchestration_bp.route('/workflow/fraud-check', methods=['POST'])
@admin_required
def execute_fraud_check():
    """Execute fraud check workflow"""
    data = request.get_json()
    user_id = data.get('user_id')
    
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400
    
    result = master_agent.orchestrate_workflow(
        'fraud_check',
        {'user_id': user_id}
    )
    
    return jsonify(result), 200

@orchestration_bp.route('/workflow/daily-maintenance', methods=['POST'])
@admin_required
def execute_daily_maintenance():
    """Execute daily maintenance workflow"""
    result = master_agent.orchestrate_workflow(
        'daily_maintenance',
        {}
    )
    
    return jsonify(result), 200

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