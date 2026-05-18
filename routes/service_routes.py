from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from database.init_db import get_db
from datetime import datetime
from routes.decorators import requires_active_plan   # ✅   

service_bp = Blueprint('service', __name__, url_prefix='/service')

@service_bp.route('/add', methods=['GET', 'POST'])
@requires_active_plan('service_provider')    # <-- only service providers
def add_service():
    if request.method == 'POST':
        # Get form data
        title = request.form.get('title')
        description = request.form.get('description')
        category = request.form.get('category')
        price = request.form.get('price')
        city = request.form.get('city')
        # ... validation ...

        user_id = get_jwt_identity()
        conn = get_db()
        conn.execute(
            """INSERT INTO services 
            (user_id, title, description, category, price, city, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, title, description, category, price, city, datetime.utcnow())
        )
        conn.commit()
        flash('Service added successfully!', 'success')
        return redirect(url_for('service.my_services'))

    return render_template('services/add_service.html')

@service_bp.route('/my-services')
@requires_active_plan('service_provider')    # <-- also protect viewing
@jwt_required()
def my_services():
    user_id = get_jwt_identity()
    conn = get_db()
    services = conn.execute(
        "SELECT * FROM services WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    return render_template('services/my_services.html', services=services)