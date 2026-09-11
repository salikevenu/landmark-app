"""Post-OTP "Install LANDMARK App" screen.

Complements, and does NOT duplicate, tests/test_pwa_installability.py
(manifest/icon/service-worker contracts, 20 tests already covering those)
and tests/test_critical_contracts.py (route/file existence guards). This
file covers the new /install route and its dedicated install screen,
inserted into the flow as: OTP verified -> Install App -> Dashboard.
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

    # 6. appinstalled event
    def test_appinstalled_is_handled_and_navigates_to_dashboard(self):
        fn = self.html.split("addEventListener('appinstalled'")[1].split("});")[0]
        self.assertIn("goToDashboard", fn)

    # 7. already-installed detection
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

    # 16. Continue to LANDMARK exists
    def test_continue_link_always_present_as_an_escape_hatch(self):
        self.assertIn('id="continueLink"', self.html)
        self.assertIn("Continue to LANDMARK", self.html)
        self.assertIn('href="/dashboard?onboarded=1"', self.html)

    # 17. dashboard is the final destination (both the plain <a> fallback
    # and the JS-driven goToDashboard() agree on the same target). The
    # "?onboarded=1" is a one-shot signal the dashboard route reads to
    # trust this exact hop -- not a persisted/session marker (see
    # DashboardEntryGateTests for why that distinction matters).
    def test_dashboard_is_the_single_final_destination(self):
        self.assertIn('href="/dashboard?onboarded=1"', self.html)
        self.assertIn("window.location.replace('/dashboard?onboarded=1')", self.html)


class InstallRouteAuthGateTests(unittest.TestCase):
    """Authenticated session must remain valid throughout, and the route
    must only ever read the existing auth check -- never mutate cookies,
    JWT config, or CSRF behavior."""

    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.app = flask_app
        self.client = flask_app.test_client()

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    def test_unauthenticated_install_redirects_to_login(self):
        res = self.client.get("/install", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/api/auth/public/login", res.headers.get("Location", ""))

    def test_authenticated_install_renders_the_screen(self):
        self.client.set_cookie("access_token", self._token(1, role="free"))
        res = self.client.get("/install")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Install LANDMARK App", res.data)

    def test_same_session_reaches_dashboard_after_install_screen(self):
        """Dashboard loads after the install screen, using the same
        cookie -- no re-authentication required. Follows the same
        "?onboarded=1" hop the real install.html sends the browser to, and
        confirms it lands on the actual dashboard (not the entry gate)."""
        token = self._token(2, role="free")
        self.client.set_cookie("access_token", token)
        install_res = self.client.get("/install")
        self.assertEqual(install_res.status_code, 200)
        dash_res = self.client.get("/dashboard?onboarded=1", follow_redirects=True)
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn(b"LANDMARK-HUB", dash_res.data)

    def test_dashboard_without_the_onboarded_hop_renders_the_entry_gate_not_the_dashboard(self):
        """A plain /dashboard visit (fresh tab, bookmark, sidebar link --
        anything other than the one-shot hop from /install) must still go
        through the gate, even with a perfectly valid session. Visiting
        /install once must never grant a lasting bypass."""
        token = self._token(5, role="free")
        self.client.set_cookie("access_token", token)
        self.client.get("/install")
        dash_res = self.client.get("/dashboard", follow_redirects=True)
        self.assertEqual(dash_res.status_code, 200)
        self.assertNotIn(b"LANDMARK-HUB", dash_res.data)
        self.assertIn(b"isStandalone", dash_res.data)

    def test_expired_token_does_not_crash_install_route(self):
        with self.app.app_context():
            token = create_access_token(identity="1", expires_delta=timedelta(seconds=-5))
        self.client.set_cookie("access_token", token)
        res = self.client.get("/install")
        self.assertIn(res.status_code, (200, 302, 401, 422))


class PostOtpNavigationTests(unittest.TestCase):
    """OTP verified -> Install App -> Dashboard for regular users; admin
    is unchanged (straight to /admin/dashboard)."""

    def test_register_navigates_to_install_after_success(self):
        html = (ROOT / "templates" / "public" / "register.html").read_text(encoding="utf-8")
        self.assertIn('window.location.replace("/install")', html)
        self.assertNotIn('window.location.href = "/dashboard"', html)

    def test_login_navigates_regular_user_to_install_admin_unchanged(self):
        html = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        self.assertIn("'/admin/dashboard' : '/install'", html)

    def test_admin_login_still_routes_to_admin_dashboard_not_install(self):
        """Administrators must never be routed through the PWA install
        screen -- same assertion as above, named for direct traceability
        to that specific requirement."""
        html = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        navigation_line = html.split("window.location.replace(")[1].split(";")[0]
        self.assertIn("role === 'admin'", navigation_line)
        self.assertIn("'/admin/dashboard'", navigation_line)


class DashboardEntryGateTests(unittest.TestCase):
    """Universal authenticated PWA onboarding: every authenticated regular
    user who has not installed the app (not running standalone) must see
    /install before the dashboard, on every visit -- never silently
    bypassed by having merely visited /install once before.

    Architecture: routes/user_routes.py's user_dashboard() never renders
    the real dashboard.html unless the request carries "?onboarded=1" -- a
    one-shot, per-navigation signal, not a persisted marker. Otherwise it
    renders dashboard_gate.html, a blank interstitial whose only job is to
    run the standalone check (in <head>, before any dashboard content
    exists) and either continue straight to the real dashboard (if
    already standalone) or send the browser to /install. No dashboard
    markup is ever sent to the browser ahead of that decision, and no
    sessionStorage/localStorage/server-side "installed" flag is used
    anywhere, so revisiting /install can never grant a lasting bypass."""

    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.app = flask_app
        self.client = flask_app.test_client()
        self.gate_html = (ROOT / "templates" / "users" / "dashboard_gate.html").read_text(encoding="utf-8")
        self.route_src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        self.dashboard_fn = self.route_src.split("def user_dashboard")[1].split("\n@user_bp.route")[0]

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    # 1 / 6. The real dashboard template is never rendered without the
    # one-shot signal -- the standalone check runs first, in a page with
    # no dashboard content at all.
    def test_dashboard_route_renders_the_blank_gate_not_the_dashboard_by_default(self):
        self.assertIn('"users/dashboard_gate.html"', self.dashboard_fn)
        self.assertIn('request.args.get("onboarded") == "1"', self.dashboard_fn)
        self.assertNotIn("LANDMARK-HUB", self.gate_html)
        self.assertNotIn("walletBalance", self.gate_html)

    def test_gate_performs_standalone_check_before_any_body_content(self):
        head, _, body = self.gate_html.partition("<body>")
        self.assertIn("display-mode: standalone", head)
        self.assertIn("navigator.standalone", head)
        self.assertEqual(body.split("</body>")[0].strip(), "")

    # 7. The gate's own redirects are loop-free: non-standalone goes to
    # /install; standalone goes home via the one-shot signal, never back
    # to the gate.
    def test_gate_sends_non_standalone_to_install_and_standalone_home(self):
        self.assertIn("'/install'", self.gate_html)
        self.assertIn("/dashboard?onboarded=1", self.gate_html)

    # 2 / 3 / 4. No persisted marker anywhere in this flow -- visiting
    # /install (or reloading/reopening the dashboard) can never grant an
    # indefinite bypass the way a sessionStorage/localStorage flag would.
    def test_no_persisted_marker_anywhere_in_the_onboarding_flow(self):
        install_html = (ROOT / "templates" / "public" / "install.html").read_text(encoding="utf-8")
        dashboard_html = (ROOT / "templates" / "users" / "dashboard.html").read_text(encoding="utf-8")
        for name, html in (
            ("install.html", install_html),
            ("dashboard.html", dashboard_html),
            ("dashboard_gate.html", self.gate_html),
        ):
            self.assertNotIn("sessionStorage", html, name)
            self.assertNotIn("localStorage", html, name)

    # 5. No server-side "installed" flag was invented -- the route only
    # ever branches on the one-shot request parameter, nothing DB-backed.
    def test_no_server_side_installed_flag_was_added(self):
        for needle in ("is_installed", "pwa_installed", "installed_at", "installed = "):
            self.assertNotIn(needle, self.dashboard_fn)

    # A plain, un-onboarded dashboard visit (authenticated) resolves to
    # the gate over HTTP, and the gate carries no dashboard content.
    def test_authenticated_plain_dashboard_request_gets_the_gate(self):
        self.client.set_cookie("access_token", self._token(6, role="free"))
        res = self.client.get("/api/user/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(b"LANDMARK-HUB", res.data)

    # The one-shot signal must actually survive the /dashboard ->
    # /api/user/dashboard redirect hop in app.py, or install.html's exit
    # would silently land back on the gate instead of the dashboard.
    def test_app_dashboard_redirect_preserves_the_onboarded_query_string(self):
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        fn = app_src.split("def redirect_dashboard")[1].split("\n@app.route")[0]
        self.assertIn("request.query_string", fn)

    # 8. This is purely a full-page-navigation concern -- JSON API routes
    # are untouched, and the gate/guard machinery lives only in the
    # dashboard entry point, never the shared layout used by AJAX-driven
    # authenticated pages.
    def test_gate_machinery_is_scoped_to_dashboard_entry_only(self):
        layout_html = (ROOT / "templates" / "layouts" / "layout_app.html").read_text(encoding="utf-8")
        self.assertNotIn("dashboard_gate", layout_html)
        pwa_install_js = (ROOT / "static" / "js" / "pwa-install.js").read_text(encoding="utf-8")
        self.assertNotIn("dashboard_gate", pwa_install_js)

    # 9. Admin is unaffected -- separate route, separate template, no
    # gate/onboarding reference of any kind.
    def test_admin_dashboard_template_has_no_gate_or_onboarding_reference(self):
        admin_html = (ROOT / "templates" / "admin" / "admin_dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("dashboard_gate", admin_html)
        self.assertNotIn("onboarded", admin_html)


class OnboardingGuardRegressionSafetyTests(unittest.TestCase):
    """The new dashboard gate must not disturb any existing, already-
    proven behavior: /install's own standalone detection, OTP->/install
    navigation, and unauthenticated /install access denial."""

    # /install's own standalone detection is untouched by this change.
    def test_install_screen_standalone_detection_still_intact(self):
        install_html = (ROOT / "templates" / "public" / "install.html").read_text(encoding="utf-8")
        self.assertIn("display-mode: standalone", install_html)
        self.assertIn("navigator.standalone", install_html)
        self.assertIn("goToDashboard()", install_html)

    # Post-OTP navigation to /install (register + regular-user login)
    # remains exactly as it was.
    def test_post_otp_navigation_to_install_remains_intact(self):
        login_html = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        register_html = (ROOT / "templates" / "public" / "register.html").read_text(encoding="utf-8")
        self.assertIn("'/admin/dashboard' : '/install'", login_html)
        self.assertIn('window.location.replace("/install")', register_html)

    # Unauthenticated access to /install is still denied -- the dashboard
    # gate does not touch, weaken, or duplicate this server-side auth gate.
    def test_unauthenticated_user_is_still_denied_install_access(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        client = flask_app.test_client()
        res = client.get("/install", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/api/auth/public/login", res.headers.get("Location", ""))


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
