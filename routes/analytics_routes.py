from flask import Blueprint, render_template
from flask_jwt_extended import jwt_required
from database.init_db import get_db_connection

analytics_bp = Blueprint('analytics', __name__, url_prefix='/analytics')

@analytics_bp.route('/')
@jwt_required()
def analytics():
    return render_template('analytics/index.html')