from flask import Blueprint, render_template, request, jsonify
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
def my_services():
    return jsonify({
        "success": False,
        "error": "This endpoint is disabled",
    }), 410
