"""PWA installability: manifest validity, icon assets, service worker
scope, and the legitimate (browser-native) install-prompt mechanism.

Diagnosed root cause (see conversation/report): the manifest, icons, and
HTTPS setup were already fully correct -- Chrome's own installability
check reported zero errors before this change. Two real, separate gaps
were found and fixed here:
  1. The service worker registered from /static/sw.js, which defaults its
     scope to /static/ (the script's own directory), not "/" as
     manifest.json declares -- so it never actually controlled any real
     page. Fixed by serving the same file from a new /sw.js route.
  2. Nothing in the app ever invited the user to install (no
     beforeinstallprompt handling anywhere) -- installing a PWA is a
     separate, browser-gated action from registering/logging in, and no
     browser allows a site to silently add a home-screen icon. Fixed by
     adding a small, hidden-until-eligible "Install LANDMARK App" button
     that only ever triggers the browser's own native prompt.
"""
import json
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


class ManifestValidityTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "static" / "manifest.json").read_text(encoding="utf-8"))

    def test_required_fields_present(self):
        for field in ("name", "short_name", "start_url", "display", "icons"):
            self.assertIn(field, self.manifest, f"manifest missing required field: {field}")

    def test_start_url_and_scope_are_site_root(self):
        self.assertEqual(self.manifest["start_url"], "/")
        self.assertEqual(self.manifest.get("scope"), "/")

    def test_display_is_installable_mode(self):
        self.assertIn(self.manifest["display"], ("standalone", "fullscreen", "minimal-ui"))

    def test_has_192_and_512_any_purpose_icons(self):
        any_sizes = {i["sizes"] for i in self.manifest["icons"] if i.get("purpose", "any") == "any"}
        self.assertIn("192x192", any_sizes)
        self.assertIn("512x512", any_sizes)

    def test_every_referenced_icon_file_exists_on_disk(self):
        for icon in self.manifest["icons"]:
            rel = icon["src"].lstrip("/")
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"manifest references missing icon file: {icon['src']}")


class IconAssetTests(unittest.TestCase):
    """Verify the existing icons rather than assuming/replacing them."""

    def _dims(self, path):
        from PIL import Image
        with Image.open(path) as img:
            return img.size, img.format

    def test_icon_192_matches_declared_size_and_is_png(self):
        size, fmt = self._dims(ROOT / "static" / "images" / "icon-192.png")
        self.assertEqual(size, (192, 192))
        self.assertEqual(fmt, "PNG")

    def test_icon_512_matches_declared_size_and_is_png(self):
        size, fmt = self._dims(ROOT / "static" / "images" / "icon-512.png")
        self.assertEqual(size, (512, 512))
        self.assertEqual(fmt, "PNG")

    def test_maskable_icons_exist_and_match_declared_size(self):
        size192, fmt192 = self._dims(ROOT / "static" / "images" / "icon-192-maskable.png")
        size512, fmt512 = self._dims(ROOT / "static" / "images" / "icon-512-maskable.png")
        self.assertEqual(size192, (192, 192))
        self.assertEqual(size512, (512, 512))
        self.assertEqual(fmt192, "PNG")
        self.assertEqual(fmt512, "PNG")


class ServiceWorkerScopeTests(unittest.TestCase):
    """Regression guard for the confirmed scope bug: registering from
    /static/sw.js defaults to scope /static/, never matching
    manifest.json's scope "/". Both layouts must register from the new
    root-served /sw.js with an explicit scope."""

    def test_sw_file_has_a_fetch_handler(self):
        sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        self.assertIn("addEventListener('fetch'", sw)

    def test_public_layout_registers_root_scoped_service_worker(self):
        html = (ROOT / "templates" / "layouts" / "layout_public.html").read_text(encoding="utf-8")
        self.assertIn("navigator.serviceWorker.register('/sw.js', { scope: '/' })", html)
        self.assertNotIn("navigator.serviceWorker.register('/static/sw.js')", html)

    def test_app_layout_registers_root_scoped_service_worker(self):
        """layout_app.html (authenticated pages) previously never called
        register() at all -- it only worked by inheriting a registration
        made earlier from a public page in the same browser. Now it
        registers directly too, so it doesn't depend on page-visit order."""
        html = (ROOT / "templates" / "layouts" / "layout_app.html").read_text(encoding="utf-8")
        self.assertIn("navigator.serviceWorker.register('/sw.js', { scope: '/' })", html)

    def test_root_sw_route_exists_in_app_py(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("@app.route('/sw.js')", src)
        self.assertIn("Service-Worker-Allowed", src)


class ServiceWorkerRouteLiveTests(unittest.TestCase):
    """Exercise the real Flask route (not just source text) to confirm
    /sw.js actually serves the existing static/sw.js content correctly."""

    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def test_sw_js_route_returns_200_js_content_type_and_allowed_header(self):
        res = self.client.get("/sw.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("javascript", res.headers.get("Content-Type", ""))
        self.assertEqual(res.headers.get("Service-Worker-Allowed"), "/")

    def test_sw_js_route_serves_the_same_file_as_static_sw_js(self):
        root_res = self.client.get("/sw.js")
        static_res = self.client.get("/static/sw.js")
        self.assertEqual(root_res.data, static_res.data)


class InstallPromptTests(unittest.TestCase):
    """Registering/logging in and installing the PWA are different browser
    mechanisms. This only ever surfaces the browser's own native install
    flow (beforeinstallprompt -> prompt()) -- it must never fabricate or
    force an install."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "pwa-install.js").read_text(encoding="utf-8")

    def test_listens_for_real_browser_install_event(self):
        self.assertIn("beforeinstallprompt", self.js)
        self.assertIn("event.preventDefault()", self.js)

    def test_uses_the_browsers_own_prompt_not_a_fake_install(self):
        self.assertIn(".prompt()", self.js)
        # Must not attempt to fabricate installation via any other API.
        self.assertNotIn("navigator.serviceWorker.register", self.js)

    def test_hidden_by_default_until_browser_says_its_eligible(self):
        self.assertIn("hidden = false", self.js)

    def test_listens_for_appinstalled_to_stop_re_offering(self):
        self.assertIn("appinstalled", self.js)

    def test_install_button_and_script_present_in_both_layouts(self):
        public_layout = (ROOT / "templates" / "layouts" / "layout_public.html").read_text(encoding="utf-8")
        app_layout = (ROOT / "templates" / "layouts" / "layout_app.html").read_text(encoding="utf-8")
        for html in (public_layout, app_layout):
            self.assertIn('id="pwaInstallBtn"', html)
            self.assertIn("hidden", html)
            self.assertIn("pwa-install.js", html)


class LayoutLinkageRegressionTests(unittest.TestCase):
    """Confirm the pre-existing manifest/apple-touch-icon linkage (from an
    earlier fix) is still intact and untouched by this change."""

    def test_manifest_and_apple_touch_icon_still_linked_in_both_layouts(self):
        public_layout = (ROOT / "templates" / "layouts" / "layout_public.html").read_text(encoding="utf-8")
        app_layout = (ROOT / "templates" / "layouts" / "layout_app.html").read_text(encoding="utf-8")
        for html in (public_layout, app_layout):
            self.assertIn('rel="manifest"', html)
            self.assertIn('rel="apple-touch-icon"', html)


if __name__ == "__main__":
    unittest.main()
