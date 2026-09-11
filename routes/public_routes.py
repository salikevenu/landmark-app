from flask import Blueprint, redirect, render_template, request

public_bp = Blueprint("public", __name__)


@public_bp.route("/public/login")
def auth_login_page():
    return render_template("public/login.html")


@public_bp.route("/register", methods=["GET"])
def register_page():
    """Capture ?ref=CODE into the session the moment this page is first
    loaded — not just when the phone/OTP form is later submitted. This is
    the same validated helper /, /join, and /download-app already use
    (routes.auth_routes.cache_landing_referral_code): unknown/invalid
    codes are silently ignored (existing behavior, unchanged), and a
    missing ref never touches or clears an already-cached one. Needed
    because the PWA manifest's start_url is a fixed "/" — if a user
    installs the app from this page before ever submitting their phone
    number, a later launch from the home-screen icon carries no ?ref= at
    all, and only this session cache (now durable, see
    cache_landing_referral_code) lets the referral still be attributed
    once they do register."""
    ref = (request.args.get("ref") or "").strip()
    if ref:
        from routes.auth_routes import cache_landing_referral_code
        cache_landing_referral_code(ref)
    return render_template("public/register.html")


@public_bp.route("/install", methods=["GET"])
def install_app_page():
    """Post-OTP "Install LANDMARK App" step. Only ever reachable with a
    currently-valid session -- reuses the exact same check already used
    by /admin/login and /api/auth/public/login
    (routes.auth_routes._current_request_is_authenticated_user), so an
    unauthenticated visit is redirected to login rather than shown this
    page. No JWT/cookie/CSRF logic is touched or duplicated here; this
    only reads the existing check."""
    from routes.auth_routes import _current_request_is_authenticated_user
    if not _current_request_is_authenticated_user():
        return redirect("/api/auth/public/login")
    return render_template("public/install.html")