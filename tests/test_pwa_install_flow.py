"""Pre-authentication "Install LANDMARK App" screen, and the single real
/dashboard entry point.

Complements, and does NOT duplicate, tests/test_pwa_installability.py
(manifest/icon/service-worker contracts) and tests/test_critical_contracts.py
(route/file existence guards). This file covers /install and its screen, and
the /dashboard route, wired into the flow as:

    QR/link -> /install -> Register/Login -> OTP -> /dashboard

An already-authenticated visitor to /install, /register, or / is sent
straight to /dashboard rather than shown any of those screens again --
/install is a pre-authentication onboarding step, never a recurring
interruption, and is never visited again after OTP.
"""
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask_jwt_extended import create_access_token


class InstallScreenSourceTests(unittest.TestCase):
    """The install screen's own beforeinstallprompt/appinstalled logic."""

    def setUp(self):
        self.html = (ROOT / "templates" / "public" / "install.html").read_text(encoding="utf-8")

    # 1. beforeinstallprompt capture
    def test_captures_beforeinstallprompt(self):
        self.assertIn("addEventListener('beforeinstallprompt'", self.html)
        self.assertIn("event.preventDefault()", self.html)

    # 2. install button visibility
    def test_install_button_hidden_by_default_and_revealed_on_prompt(self):
        self.assertIn('id="installBtn"', self.html)
        self.assertIn("hidden", self.html.split('id="installBtn"')[1].split(">")[0])
        fn = self.html.split("addEventListener('beforeinstallprompt'")[1].split("});")[0]
        self.assertIn("installBtn.hidden = false", fn)

    # 10. preventDefault is used (checked as its own item, distinct from
    # "beforeinstallprompt is captured" above)
    def test_prevent_default_is_called_on_the_captured_event(self):
        fn = self.html.split("addEventListener('beforeinstallprompt'")[1].split("});")[0]
        self.assertIn("event.preventDefault()", fn)

    # 3 / 11. install prompt invocation, only from the install action
    def test_uses_the_real_prompt_method(self):
        self.assertIn(".prompt()", self.html)
        self.assertIn("userChoice", self.html)

    def test_prompt_is_only_ever_invoked_from_the_install_buttons_click_handler(self):
        # .prompt() must not appear anywhere outside the click handler --
        # e.g. never called automatically on page load or from the
        # beforeinstallprompt listener itself (that would violate "no
        # automatic prompt without a user gesture").
        before_click, _, after_click = self.html.partition("installBtn.addEventListener('click'")
        self.assertNotIn(".prompt()", before_click)
        self.assertIn(".prompt()", after_click)

    # 4. accepted installation
    def test_accepted_outcome_waits_for_appinstalled_not_immediate_claim(self):
        click_fn = self.html.split("installBtn.addEventListener('click'")[1]
        self.assertIn("Installing", click_fn)
        # The status text itself (what the user actually sees) must be an
        # in-progress message ("Installing...") never a completion claim
        # ("Installed") -- only the real appinstalled event may say that.
        installing_branch = click_fn.split("} else {")[1].split("}")[0]
        status_assignment = installing_branch.split("statusEl.textContent = ")[1].split(";")[0]
        self.assertNotIn("Installed", status_assignment)

    # 5. dismissed installation
    def test_dismissed_outcome_shows_fallback_not_a_fake_error(self):
        click_fn = self.html.split("installBtn.addEventListener('click'")[1]
        self.assertIn("dismissed", click_fn)
        dismissed_branch = click_fn.split("'dismissed'")[1].split("} else {")[0]
        self.assertIn("fallbackHint.hidden = false", dismissed_branch)

    # 6. appinstalled event continues the session onward (to registration --
    # never a fresh, second authentication mechanism).
    def test_appinstalled_is_handled_and_continues_the_session(self):
        fn = self.html.split("addEventListener('appinstalled'")[1].split("});")[0]
        self.assertIn("goToDashboard", fn)
        continue_fn = self.html.split("function goToDashboard")[1].split("\n  }")[0]
        self.assertIn("/register", continue_fn)

    # 7. already-running-standalone (installed, not currently signed in)
    # skips straight to the same next step as a normal "Continue".
    def test_detects_standalone_display_mode_and_skips_screen(self):
        self.assertIn("display-mode: standalone", self.html)
        self.assertIn("navigator.standalone", self.html)
        block = self.html.split("isStandalone")[-1]
        self.assertIn("goToDashboard()", block.split("}")[0])

    # 8 / 15. fallback when beforeinstallprompt is unavailable
    def test_fallback_shown_when_prompt_never_fires(self):
        self.assertIn('id="fallbackHint"', self.html)
        self.assertIn("Chrome", self.html)
        self.assertIn("Add to Home screen", self.html)
        timeout_block = self.html.split("setTimeout(function ()")[1].split("}, 1500)")[0]
        self.assertIn("fallbackHint.hidden = false", timeout_block)

    def test_fallback_instructions_are_a_numbered_step_by_step_list(self):
        fallback_block = self.html.split('id="fallbackHint"')[1].split("</div>")[0]
        self.assertIn("<ol", fallback_block)
        self.assertIn("⋮", fallback_block)  # the literal ⋮ (Chrome's menu icon)
        self.assertIn("Install app", fallback_block)
        self.assertIn("Add to Home screen", fallback_block)
        self.assertIn("Confirm installation", fallback_block)

    def test_never_fabricates_or_forces_installation(self):
        self.assertNotIn("localStorage", self.html)
        self.assertNotIn("sessionStorage", self.html)

    # 16. Continue to LANDMARK exists, and (post-install-page redesign)
    # leads onward to registration -- this screen is pre-authentication,
    # so there is no session yet to land a dashboard visit on.
    def test_continue_link_always_present_as_an_escape_hatch(self):
        self.assertIn('id="continueLink"', self.html)
        self.assertIn("Continue to LANDMARK", self.html)
        self.assertIn('href="/register"', self.html)

    # 17. /register is the single agreed destination (both the plain <a>
    # fallback and the JS-driven goToDashboard() target it) -- never
    # /dashboard, since a visitor who is actually authenticated never sees
    # this rendered screen at all (routes/public_routes.py redirects them
    # away before it renders).
    def test_register_is_the_single_next_destination(self):
        self.assertIn('href="/register"', self.html)
        self.assertIn("window.location.replace('/register')", self.html)


class InstallRouteAuthGateTests(unittest.TestCase):
    """/install is a pre-authentication onboarding step: an unauthenticated
    visitor sees the screen; an already-authenticated visitor is sent
    straight to /dashboard so this page never becomes a recurring
    interruption. The route only ever reads the existing auth check --
    never mutates cookies, JWT config, or CSRF behavior."""

    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.app = flask_app
        self.client = flask_app.test_client()

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    def test_unauthenticated_visitor_sees_the_install_screen(self):
        res = self.client.get("/install")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Install LANDMARK App", res.data)

    def test_authenticated_visitor_is_redirected_to_dashboard(self):
        self.client.set_cookie("access_token", self._token(1, role="free"))
        res = self.client.get("/install", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/dashboard", res.headers.get("Location", ""))

    def test_expired_token_does_not_crash_install_route(self):
        with self.app.app_context():
            token = create_access_token(identity="1", expires_delta=timedelta(seconds=-5))
        self.client.set_cookie("access_token", token)
        res = self.client.get("/install")
        self.assertIn(res.status_code, (200, 302, 401, 422))

    def test_install_captures_ref_code_for_unauthenticated_visitor(self):
        """The install page is now the first stop for a shared QR/link, so
        it must capture ?ref=CODE itself (same validated helper /register
        already uses) -- otherwise a referral entering via /install would
        be silently lost before the user ever reaches /register."""
        src = (ROOT / "routes" / "public_routes.py").read_text(encoding="utf-8")
        fn = src.split("def install_app_page")[1]
        self.assertIn("cache_landing_referral_code", fn)
        self.assertIn('request.args.get("ref")', fn)


class PostOtpNavigationTests(unittest.TestCase):
    """OTP verified -> Dashboard directly for regular users; admin is
    unchanged (straight to /admin/dashboard). /install must never appear
    after OTP -- it is a pre-authentication step only."""

    def test_register_navigates_to_dashboard_after_success(self):
        html = (ROOT / "templates" / "public" / "register.html").read_text(encoding="utf-8")
        self.assertIn('window.location.replace("/dashboard")', html)
        self.assertNotIn('window.location.replace("/install")', html)

    def test_login_navigates_regular_user_to_dashboard_admin_unchanged(self):
        html = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        self.assertIn("'/admin/dashboard' : '/dashboard'", html)

    def test_admin_login_still_routes_to_admin_dashboard_not_regular_dashboard(self):
        """Administrators must never be routed through the regular-user
        dashboard -- same assertion as above, named for direct
        traceability to that specific requirement."""
        html = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        navigation_line = html.split("window.location.replace(")[1].split(";")[0]
        self.assertIn("role === 'admin'", navigation_line)
        self.assertIn("'/admin/dashboard'", navigation_line)

    def test_neither_post_otp_navigation_ever_targets_install(self):
        login_html = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        register_html = (ROOT / "templates" / "public" / "register.html").read_text(encoding="utf-8")
        self.assertNotIn("/install", login_html)
        self.assertNotIn("/install", register_html)


class DashboardRouteTests(unittest.TestCase):
    """/dashboard (app.py) is the single real, authenticated dashboard
    entry point and the PWA's start_url. /api/user/dashboard is kept only
    as a compatibility redirect for existing internal links (404/500 pages,
    payment-flow pages, admin impersonation) -- it renders nothing itself."""

    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.app = flask_app
        self.client = flask_app.test_client()

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    def test_unauthenticated_dashboard_request_does_not_render_private_content(self):
        res = self.client.get("/dashboard", follow_redirects=False)
        self.assertNotEqual(res.status_code, 200)
        self.assertNotIn(b"LANDMARK-HUB", res.data)

    def test_authenticated_dashboard_request_renders_the_real_dashboard(self):
        self.client.set_cookie("access_token", self._token(2, role="free"))
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"LANDMARK-HUB", res.data)

    def test_dashboard_route_is_jwt_protected_not_a_bare_redirect(self):
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        fn_block = app_src.split('@app.route("/dashboard")')[1].split("\n@app.route")[0]
        self.assertIn("@jwt_required()", fn_block)
        self.assertIn('"users/dashboard.html"', fn_block)

    def test_api_user_dashboard_redirects_to_dashboard_and_renders_nothing_itself(self):
        src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        fn = src.split("def user_dashboard")[1].split("\n@user_bp.route")[0]
        self.assertIn('redirect("/dashboard")', fn)
        self.assertNotIn("render_template", fn)

    def test_api_user_dashboard_forwards_an_authenticated_session(self):
        self.client.set_cookie("access_token", self._token(3, role="free"))
        res = self.client.get("/api/user/dashboard", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"LANDMARK-HUB", res.data)

    def test_no_onboarded_query_param_dependency_remains(self):
        for path in ("app.py", "routes/user_routes.py"):
            src = (ROOT / path).read_text(encoding="utf-8")
            self.assertNotIn("onboarded", src, path)

    def test_dashboard_gate_template_no_longer_exists(self):
        self.assertFalse((ROOT / "templates" / "users" / "dashboard_gate.html").exists())


class RootAndRegisterAuthenticatedRedirectTests(unittest.TestCase):
    """An already-authenticated visitor to / or /register is sent straight
    to /dashboard rather than shown the public homepage or the
    registration form again. An unauthenticated visitor is unaffected."""

    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.app = flask_app
        self.client = flask_app.test_client()

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    def test_unauthenticated_root_sees_the_public_homepage(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Get Started", res.data)

    def test_authenticated_root_is_redirected_to_dashboard(self):
        self.client.set_cookie("access_token", self._token(4, role="free"))
        res = self.client.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/dashboard", res.headers.get("Location", ""))

    def test_unauthenticated_register_sees_the_registration_form(self):
        res = self.client.get("/register")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Create Account", res.data)

    def test_authenticated_register_is_redirected_to_dashboard(self):
        self.client.set_cookie("access_token", self._token(5, role="free"))
        res = self.client.get("/register", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/dashboard", res.headers.get("Location", ""))


class InstallPageInheritsPwaContractTests(unittest.TestCase):
    """install.html extends layout_public.html, so it inherits the exact
    manifest link, apple-touch-icon, and root-scoped service-worker
    registration already proven in test_pwa_installability.py -- this only
    confirms install.html did not opt out of that inheritance, and that
    its own dedicated button doesn't visually collide with the generic
    site-wide one."""

    def setUp(self):
        self.html = (ROOT / "templates" / "public" / "install.html").read_text(encoding="utf-8")

    def test_extends_layout_public_html(self):
        self.assertIn('{% extends "layouts/layout_public.html" %}', self.html)

    def test_suppresses_the_generic_floating_button_on_this_page_only(self):
        self.assertIn("#pwaInstallBtn { display: none !important; }", self.html)
        # layout_public.html itself must be untouched by this.
        layout = (ROOT / "templates" / "layouts" / "layout_public.html").read_text(encoding="utf-8")
        self.assertIn('<button id="pwaInstallBtn" type="button" hidden>', layout)


if __name__ == "__main__":
    unittest.main()
