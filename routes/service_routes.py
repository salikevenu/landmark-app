from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text
from database.init_db import get_db
from datetime import datetime
from routes.decorators import requires_active_plan

service_bp = Blueprint('service', __name__, url_prefix='/service')

@service_bp.route('/add', methods=['GET', 'POST'])
@requires_active_plan('service_provider')
def add_service():
    if request.method == 'POST':
        title = request.form.get('title')
        description = request.form.get('description')
        category = request.form.get('category')
        price = request.form.get('price')
        city = request.form.get('city')
        # ... validation ...

        user_id = get_jwt_identity()
        conn = get_db()
        conn.execute(text("""
            INSERT INTO services 
            (user_id, title, description, category, price, city, created_at) 
            VALUES (:user_id, :title, :description, :category, :price, :city, :created_at)
        """), {
            "user_id": user_id,
            "title": title,
            "description": description,
            "category": category,
            "price": price,
            "city": city,
            "created_at": datetime.utcnow()
        })
        conn.commit()
        flash('Service added successfully!', 'success')
        return redirect(url_for('service.my_services'))

    return render_template('services/add_service.html')

@service_bp.route('/my-services')
@requires_active_plan('service_provider')
@jwt_required()
def my_services():
    user_id = get_jwt_identity()
    conn = get_db()
    rows = conn.execute(text(
        "SELECT * FROM services WHERE user_id = :uid ORDER BY created_at DESC"
    ), {"uid": user_id}).fetchall()
    # Convert to dicts for template compatibility (if template uses dict access)
    services = [dict(r._mapping) for r in rows]
    return render_template('services/my_services.html', services=services)