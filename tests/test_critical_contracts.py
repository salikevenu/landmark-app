"""LANDMARK Critical Contracts / Regression Guards.

Lightweight, structural tests that catch a future change silently
breaking core behavior. This is NOT a new testing framework and does NOT
duplicate the existing, already-passing suites -- it fills specific gaps
they don't cover:
  - tests/test_auth_session.py, test_silent_refresh.py,
    test_referral_attribution.py, test_security_stage*.py already prove
    the full authentication contract (cookies, silent refresh, CSRF,
    admin/user login redirects, banned-user rejection, ...).
  - tests/test_pwa_installability.py already proves the full PWA contract
    (manifest validity, icon files/dimensions, service worker scope,
    install-prompt correctness) with 20 tests.

What's here instead: cheap "did this critical thing silently disappear"
checks -- a deleted route, a removed authFetch call, a renamed file, a
network error accidentally treated as a logout -- the class of regression
none of the above suites happens to assert directly.

See docs/LANDMARK_CRITICAL_CONTRACTS.md for the full policy this file
protects and how future changes (by a human or an AI coding agent) should
be validated against it.
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------
# APPLICATION CONTRACT: required routes must not silently disappear.
# ---------------------------------------------------------------------
CRITICAL_ROUTES = [
    "/api/auth/send-otp",
    "/api/auth/verify-otp",
    "/api/auth/public/login",
    "/api/refresh",
    "/api/refresh/silent",
    "/logout",
    "/admin/login",
    "/register",
    "/dashboard",
    "/sw.js",
    "/admin/dashboard",
]


class CriticalRoutesExistTests(unittest.TestCase):
    """A route disappearing (renamed, blueprint not registered, a typo in
    a decorator) is one of the easiest regressions to introduce and one
    of the cheapest to catch. This only checks the URL is still wired to
    something -- see the dedicated auth/PWA suites for behavior."""

    @classmethod
    def setUpClass(cls):
        from app import app as flask_app
        cls.paths = {str(r).split(" ")[0] for r in flask_app.url_map.iter_rules()}

    def test_every_critical_route_is_still_registered(self):
        missing = [p for p in CRITICAL_ROUTES if p not in self.paths]
        self.assertEqual(missing, [], f"critical route(s) disappeared: {missing}")


class ProtectedRouteRejectsUnauthenticatedTests(unittest.TestCase):
    """A protected route silently losing its auth decorator is exactly
    the kind of change that looks like a harmless simplification but
    breaks security. This proves the gate is still there without
    re-deriving full auth behavior (see test_auth_session.py for that)."""

    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def test_admin_dashboard_rejects_a_request_with_no_session(self):
        res = self.client.get("/admin/dashboard", follow_redirects=False)
        self.assertIn(res.status_code, (302, 401, 403))

    def test_admin_stats_api_rejects_a_request_with_no_session(self):
        res = self.client.get("/api/admin/stats")
        self.assertIn(res.status_code, (302, 401, 403))


# ---------------------------------------------------------------------
# AUTHENTICATION CONTRACT: network failure must never look like logout.
# ---------------------------------------------------------------------
class NetworkFailureIsNotLogoutTests(unittest.TestCase):
    """"Network failure must never be interpreted as logout." authFetch's
    login-redirect must only be reachable after a real HTTP 401 response
    -- never from a rejected/thrown fetch (an actual network error), which
    must propagate to the caller instead of being swallowed."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "session.js").read_text(encoding="utf-8")

    def _auth_fetch_body(self):
        return self.js.split("async function authFetch")[1].split("\n  global.LandmarkSession")[0]

    def test_authfetch_has_no_catch_block(self):
        # A real network error must reject the returned promise, not be
        # caught here and turned into a login redirect.
        self.assertNotIn("catch", self._auth_fetch_body())

    def test_redirect_to_login_is_reached_only_through_a_401_check(self):
        fn = self._auth_fetch_body()
        before, _, after = fn.partition("res.status === 401")
        self.assertNotIn("redirectToLogin", before)
        self.assertIn("redirectToLogin", after)


class BannedUserHandlingTests(unittest.TestCase):
    """Blocked/inactive users must keep failing closed at the JWT
    user-lookup layer (services/jwt_session.py), not just at whichever
    route happens to call it today."""

    def test_lookup_jwt_user_still_rejects_blocked_and_inactive(self):
        src = (ROOT / "services" / "jwt_session.py").read_text(encoding="utf-8")
        fn = src.split("def lookup_jwt_user")[1].split("\ndef ")[0]
        self.assertIn('"blocked"', fn)
        self.assertIn('"inactive"', fn)
        self.assertIn("return None", fn)


# ---------------------------------------------------------------------
# AUTHENTICATION CONTRACT: authenticated pages must keep using authFetch.
# ---------------------------------------------------------------------
# Deliberately an explicit allowlist, not a blanket scan of every
# templates/users/*.html file: browse.html and pricing.html intentionally
# serve public content and correctly call /api/ WITHOUT authentication --
# including them here would be a false regression, not a real one.
KNOWN_AUTHENTICATED_USER_PAGES = [
    "dashboard.html",
    "wallet.html",
    "rank.html",
    "invite.html",
    "profile.html",
    "my_listings.html",
    "create_listing.html",
    "edit_listing.html",
]


class AuthenticatedPageMustUseAuthFetchTests(unittest.TestCase):
    """Mirrors the existing admin-page scan in test_auth_session.py
    (test_no_admin_page_bypasses_authfetch_for_admin_api_calls) for the
    regular-user authenticated pages already confirmed to use authFetch
    today."""

    def test_known_authenticated_pages_still_use_authfetch_for_their_api_calls(self):
        offenders = []
        for name in KNOWN_AUTHENTICATED_USER_PAGES:
            content = (ROOT / "templates" / "users" / name).read_text(encoding="utf-8")
            if "/api/" in content and "LandmarkSession.authFetch" not in content:
                offenders.append(name)
        self.assertEqual(offenders, [])


# ---------------------------------------------------------------------
# APPLICATION CONTRACT: critical files must not be deleted or renamed.
# ---------------------------------------------------------------------
CRITICAL_FILES = [
    "templates/layouts/layout_public.html",
    "templates/layouts/layout_app.html",
    "templates/public/login.html",
    "templates/public/register.html",
    "templates/admin/admin_login.html",
    "templates/users/dashboard.html",
    "static/js/session.js",
    "static/js/pwa-install.js",
    "static/sw.js",
    "static/manifest.json",
    "static/images/icon-192.png",
    "static/images/icon-512.png",
    "static/images/icon-192-maskable.png",
    "static/images/icon-512-maskable.png",
]


class CriticalFilesExistTests(unittest.TestCase):
    def test_no_critical_file_has_been_deleted_or_renamed(self):
        missing = [p for p in CRITICAL_FILES if not (ROOT / p).is_file()]
        self.assertEqual(missing, [], f"critical file(s) missing: {missing}")


if __name__ == "__main__":
    unittest.main()
