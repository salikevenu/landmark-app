from flask import Blueprint, render_template
from flask_jwt_extended import jwt_required
from database.init_db import get_db_connection

transaction_bp = Blueprint('transactions', __name__, url_prefix='/transactions')

@transaction_bp.route('/')
@jwt_required()
def transactions():
    return render_template('transactions/index.html')