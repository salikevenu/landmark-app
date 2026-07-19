from flask import Blueprint, render_template
from flask_jwt_extended import jwt_required
from database.init_db import get_db_connection

promo_bp = Blueprint('promotions', __name__, url_prefix='/promotions')

@promo_bp.route('/')
@jwt_required()
def promotions():
    return render_template('promotions/index.html')