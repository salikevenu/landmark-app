from flask import Blueprint, jsonify, render_template
from flask_jwt_extended import jwt_required

promotions_bp = Blueprint('promotions', __name__, url_prefix='/promotions')


@promotions_bp.route('/')
@jwt_required()
def promotions():
    """Self-serve paid promotions are not a live product.

    Nearby 'sponsored' ranking is an admin-granted unpaid flag only.
    """
    return render_template("promotions/index.html")


@promotions_bp.route("/api/promotions/onboard", methods=["POST"])
@jwt_required()
def create_promotion():
    """Disabled: must not create sponsored ads without a captured payment."""
    return jsonify({
        "success": False,
        "error": "This endpoint is disabled",
    }), 410
