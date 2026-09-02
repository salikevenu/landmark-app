"""Auth persistence across app/browser/device restarts.

A full-page GET to a JWT-protected route (e.g. /analytics/, the admin
panel) must not force a fresh OTP login just because the short-lived
access-token cookie expired or was never present this process — cookies
survive restarts, and a valid refresh-token cookie should be enough to
silently keep the user signed in (mirrors what static/js/session.js already
does for AJAX calls). Only a genuinely dead/blocked/revoked credential, or
an explicit logout, should ever land the user back on the login screen.
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask_jwt_extended import create_access_token, create_refresh_token

HTML_ACCEPT = {"Accept": "text/html,application/xhtml+xml"}


def _conn(row_mapping):
    class Conn:
        def execute(self, query, params=None):
            res = MagicMock()
            res.fetchone.return_value = (
                SimpleNamespace(_mapping=row_mapping) if row_mapping is not None else None
            )
            return res

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Conn()


ACTIVE_USER = {"id": 42, "role": "free", "phone": "9876543210", "is_blocked": 0, "is_active": 1}
BLOCKED_USER = {"id": 42, "role": "free", "phone": "9876543210", "is_blocked": 1, "is_active": 1}


class SilentRefreshOnPageLoadTests(unittest.TestCase):
    """The core fix: page navigations get the same silent-refresh treatment AJAX already had."""

    def setUp(self):
        from app import app as flask_app
        self.app = flask_app
        self.app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def _issue_refresh_cookie(self, remember_me=False):
        with self.app.app_context():
            token = create_refresh_token(
                identity="42", additional_claims={"remember_me": remember_me}
            )
        self.client.set_cookie("refresh_token", token)

    def test_expired_access_token_is_silently_refreshed_not_logged_out(self):
        """Simulates: user closes the app for hours/reboots the device, access
        cookie is gone/expired but the refresh cookie is still good — the
        protected page must load normally, not bounce to login."""
        self._issue_refresh_cookie()
        with patch("services.jwt_session.get_db_connection", return_value=_conn(ACTIVE_USER)), patch(
            "database.init_db.get_db_connection", return_value=_conn(ACTIVE_USER)
        ):
            res = self.client.get("/analytics/", headers=HTML_ACCEPT, follow_redirects=True)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.history), 2)  # -> /api/refresh/silent -> /analytics/
        self.assertIsNotNone(self.client.get_cookie("access_token"))

    def test_missing_access_token_cookie_is_silently_refreshed(self):
        """App restart: access cookie never got set again yet, refresh cookie persisted."""
        self._issue_refresh_cookie(remember_me=True)
        with patch("services.jwt_session.get_db_connection", return_value=_conn(ACTIVE_USER)), patch(
            "database.init_db.get_db_connection", return_value=_conn(ACTIVE_USER)
        ):
            res = self.client.get("/analytics/", headers=HTML_ACCEPT, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIsNotNone(self.client.get_cookie("access_token"))

    def test_no_credentials_at_all_reaches_login(self):
        res = self.client.get("/analytics/", headers=HTML_ACCEPT, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"login", res.data.lower())
        self.assertIsNone(self.client.get_cookie("access_token"))

    def test_blocked_user_is_not_silently_refreshed(self):
        """A genuinely revoked/blocked credential must still force login."""
        self._issue_refresh_cookie()
        with patch("services.jwt_session.get_db_connection", return_value=_conn(BLOCKED_USER)):
            res = self.client.get("/analytics/", headers=HTML_ACCEPT, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"login", res.data.lower())
        self.assertIsNone(self.client.get_cookie("access_token"))

    def test_db_outage_during_silent_refresh_does_not_force_login(self):
        """Server/DB temporarily unavailable must not be treated as a logout."""
        self._issue_refresh_cookie()
        with patch("services.jwt_session.get_db_connection", return_value=_conn(ACTIVE_USER)), patch(
            "database.init_db.get_db_connection", side_effect=RuntimeError("db down")
        ):
            res = self.client.get("/analytics/", headers=HTML_ACCEPT, follow_redirects=False)
            follow = self.client.get(res.headers["Location"])
        self.assertEqual(follow.status_code, 503)
        # Refresh cookie must survive a transient outage — not cleared like a real logout.
        self.assertIsNotNone(self.client.get_cookie("refresh_token"))

    def test_ajax_request_still_gets_json_401_not_a_redirect(self):
        """Regression guard: authFetch's own client-side refresh-and-retry must be untouched."""
        res = self.client.get("/analytics/", headers={"Accept": "application/json"})
        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.get_json().get("success"))

    def test_silent_refresh_rejects_open_redirect_target(self):
        self._issue_refresh_cookie()
        with patch("services.jwt_session.get_db_connection", return_value=_conn(ACTIVE_USER)), patch(
            "database.init_db.get_db_connection", return_value=_conn(ACTIVE_USER)
        ):
            res = self.client.get(
                "/api/refresh/silent?next=" + quote("//evil.example.com"), follow_redirects=False
            )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["Location"], "/")

    def test_silent_refresh_with_no_refresh_cookie_goes_to_login(self):
        res = self.client.get("/api/refresh/silent?next=%2Fanalytics%2F", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/api/auth/public/login", res.headers["Location"])


class SafeRelativePathTests(unittest.TestCase):
    def setUp(self):
        from app import _safe_relative_path
        self.fn = _safe_relative_path

    def test_relative_path_preserved(self):
        self.assertEqual(self.fn("/admin/dashboard"), "/admin/dashboard")
        self.assertEqual(self.fn("/pricing?page_type=business"), "/pricing?page_type=business")

    def test_absolute_and_protocol_relative_urls_rejected(self):
        self.assertEqual(self.fn("http://evil.com"), "/")
        self.assertEqual(self.fn("https://evil.com/x"), "/")
        self.assertEqual(self.fn("//evil.com"), "/")
        self.assertEqual(self.fn("/\\evil.com"), "/")
        self.assertEqual(self.fn(""), "/")
        self.assertEqual(self.fn(None), "/")


if __name__ == "__main__":
    unittest.main()
