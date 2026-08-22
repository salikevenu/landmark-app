from flask import Blueprint, render_template, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text
from database.init_db import get_db_connection
from routes.decorators import requires_active_plan

service_bp = Blueprint('service', __name__, url_prefix='/service')

@service_bp.route('/add', methods=['GET', 'POST'])
@requires_active_plan('service_provider')
def add_service():
    if request.method == 'POST':
        return jsonify({
            "success": False,
            "error": "This endpoint is disabled",
        }), 410

    return render_template('services/add_service.html')

@service_bp.route('/my-services')
@requires_active_plan('service_provider')
@jwt_required()
def my_services():
    user_id = get_jwt_identity()
    conn = get_db_connection()
    rows = conn.execute(text(
        "SELECT * FROM services WHERE user_id = :uid ORDER BY created_at DESC"
    ), {"uid": user_id}).fetchall()
    services = [dict(r._mapping) for r in rows]
    return render_template('services/my_services.html', services=services)
