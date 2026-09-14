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
    missing ref never touches or clears an already-cached one.

    An already-authenticated visitor is sent straight to /dashboard --
    same check as /api/auth/public/login and /install -- rather than
    shown the registration form again; referral capture never applies to
    an existing session, so it's skipped in that branch too.
    """
    from routes.auth_routes import _current_request_is_authenticated_user
    if _current_request_is_authenticated_user():
        return redirect("/dashboard")
    ref = (request.args.get("ref") or "").strip()
    if ref:
        from routes.auth_routes import cache_landing_referral_code
        cache_landing_referral_code(ref)
    return render_template("public/register.html")


@public_bp.route("/install", methods=["GET"])
def install_app_page():
    """Pre-authentication "Install LANDMARK App" step -- the landing point
    for a shared link/QR code, reached BEFORE register/login/OTP. Captures
    ?ref=CODE the same way /register does (same validated helper, unknown/
    invalid codes silently ignored) so a referral survives this stop even
    though it comes before the phone number is ever collected.

    An already-authenticated visitor is redirected straight to /dashboard
    -- same check /api/auth/public/login and /register use -- so this
    page never becomes a recurring interruption for someone who already
    has a valid session (e.g. an old bookmark/QR reused after signup).
    Unauthenticated visitors see the install screen, then continue to
    /register from there (see install.html) -- no separate auth mechanism
    is introduced here.
    """
    from routes.auth_routes import _current_request_is_authenticated_user
    if _current_request_is_authenticated_user():
        return redirect("/dashboard")
    ref = (request.args.get("ref") or "").strip()
    if ref:
        from routes.auth_routes import cache_landing_referral_code
        cache_landing_referral_code(ref)
    return render_template("public/install.html")