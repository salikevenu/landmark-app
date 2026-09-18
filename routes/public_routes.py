"""Pre-authentication pages: signup, login, install, and the post-OTP
welcome step.

Design note -- why signup and login are one page:

There has never been a register-vs-login distinction on the server.
/api/auth/verify-otp calls get_or_create_user(), which creates the
account when the phone is unknown and signs it in when it isn't, and
returns status "new" or "existing" to say which happened. The two HTML
pages that used to exist were the same form, posting to the same three
endpoints, differing only in wording -- plus one real divergence: the
old /public/login had no already-authenticated redirect, so a logged-in
user landing on it was shown a fresh OTP form and told to sign in again.

So this module now serves ONE template for both /signup and /login, with
`mode` changing only the heading and the footer link. The user never has
to know in advance whether they have an account, which is the whole
point -- the server already doesn't ask.
"""
from urllib.parse import quote

from flask import Blueprint, redirect, render_template, request

from routes.auth_routes import (
    DASHBOARD_PATH,
    LOGIN_PATH,
    SIGNUP_PATH,
    _current_request_is_authenticated_user,
    cache_landing_referral_code,
)

public_bp = Blueprint("public", __name__)


def _with_ref(path):
    """Carry a ?ref code across a redirect, url-encoded."""
    ref = (request.args.get("ref") or "").strip()
    if not ref:
        return path
    return path + "?ref=" + quote(ref, safe="")


def _capture_ref():
    """Validate and session-cache ?ref=CODE if present.

    Same helper /, /join and /download-app use. Unknown or invalid codes
    are silently ignored (existing behaviour, unchanged) and a missing
    ref never clears an already-cached one.
    """
    ref = (request.args.get("ref") or "").strip()
    if ref:
        cache_landing_referral_code(ref)


def _render_auth_page(mode):
    """The one OTP page, in signup or login wording.

    An already-authenticated visitor is never shown a fresh OTP form --
    otherwise a back-button press, a bookmark, or a URL autocomplete hit
    looks exactly like a lost session even when the cookies are still
    perfectly valid. Referral capture is skipped in that branch because a
    referral never applies to an existing session.
    """
    if _current_request_is_authenticated_user():
        return redirect(DASHBOARD_PATH)
    _capture_ref()
    return render_template("public/auth.html", mode=mode)


# =================================
# CANONICAL AUTH PAGES
# =================================

@public_bp.route("/signup", methods=["GET"])
def signup_page():
    """New-user wording. Identical form and identical API calls to /login."""
    return _render_auth_page("signup")


@public_bp.route("/login", methods=["GET"])
def login_page():
    """Returning-user wording. Identical form and API calls to /signup."""
    return _render_auth_page("login")


@public_bp.route("/welcome", methods=["GET"])
def welcome_page():
    """First-run profile step, shown once, immediately after signup.

    This is where the display name is collected -- and, unlike the old
    registration form, where it is actually saved. That form asked for a
    name BEFORE the OTP and then threw it away: neither /api/auth/send-otp
    nor /api/auth/verify-otp ever read the field, and get_or_create_user
    hardcodes `name = ''` on INSERT, so every account ever created is
    nameless.

    Asking after verification is both the fix and the better flow: there
    is a real user row to write to, the phone step stays a single field,
    and a user who drops out here is already signed up rather than lost.

    Unauthenticated visitors go to /login. The page itself is harmless
    without a session -- it only renders a form -- but sending them on is
    clearer than showing a profile form to nobody. Whether the name is
    already set is decided client-side from /api/auth/me, so a user who
    reloads this URL later is simply forwarded to the dashboard.
    """
    if not _current_request_is_authenticated_user():
        return redirect(LOGIN_PATH)
    return render_template("public/welcome.html")


@public_bp.route("/install", methods=["GET"])
def install_app_page():
    """Pre-authentication "Install LANDMARK App" step -- the landing point
    for a shared link/QR code, reached BEFORE signup/login/OTP. Captures
    ?ref=CODE the same way the auth pages do (same validated helper,
    unknown/invalid codes silently ignored) so a referral survives this
    stop even though it comes before the phone number is ever collected.

    An already-authenticated visitor is redirected straight to the
    dashboard, so this page never becomes a recurring interruption for
    someone who already has a valid session (e.g. an old bookmark/QR
    reused after signup). Unauthenticated visitors see the install
    screen, then continue to /signup from there (see install.html) -- no
    separate auth mechanism is introduced here.
    """
    if _current_request_is_authenticated_user():
        return redirect(DASHBOARD_PATH)
    _capture_ref()
    return render_template("public/install.html")


# =================================
# LEGACY URL ALIASES
# =================================
# Kept so printed QR codes, shared links, browser bookmarks, autocomplete
# entries and service-worker cache entries that still point at the old
# URLs keep working. Nothing in the codebase generates these any more.
#
# All 302, never 301: this is a PWA with a service worker, and a
# permanently-cached redirect on an auth URL cannot be undone from the
# server if the flow ever changes again.

@public_bp.route("/register", methods=["GET"])
def register_page_legacy():
    """Old signup URL. The ?ref code is preserved across the hop."""
    return redirect(_with_ref(SIGNUP_PATH))


@public_bp.route("/public/login", methods=["GET"])
def public_login_page_legacy():
    """Old login URL.

    This previously rendered the login page directly with NO
    already-authenticated redirect -- the one genuine behavioural
    divergence between the two old login surfaces. It is now an alias for
    /login, which performs that check.
    """
    return redirect(_with_ref(LOGIN_PATH))
