from flask import Blueprint, render_template
from flask_jwt_extended import jwt_required
from database.init_db import get_db

review_bp = Blueprint('reviews', __name__, url_prefix='/reviews')

@review_bp.route('/')
@jwt_required()
def reviews():
    return render_template('reviews/index.html')