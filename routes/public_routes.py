from flask import Blueprint, render_template

public_bp = Blueprint("public", __name__)


@public_bp.route("/public/login")
def auth_login_page():
    return render_template("public/login.html")


@public_bp.route("/register", methods=["GET"])
def register_page():
    return render_template("public/register.html")