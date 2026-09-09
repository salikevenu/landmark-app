"""SMS provider timeout/retry contract (services/sms_service.py).

Regression guard for the confirmed production incident: send_otp()'s POST
to Message Central had no bounded timeout and was eligible for automatic
retries on the same non-idempotent call, letting one request block the
single sync Gunicorn worker for well over its 120s timeout (Render then
SIGKILLed the worker; the browser saw a generic "Network error").

Fixed here: the POST is no longer retryable at all, and both send_otp()
and verify_otp() use an explicit (connect, read) timeout tuple instead of
a single float. verify_otp()'s GET retries are intentionally left intact
(idempotent, genuinely useful) -- only its timeout became explicit.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAKE_ENV = {
    "MESSAGE_CENTRAL_CUSTOMER_ID": "test-customer-id",
    "MESSAGE_CENTRAL_AUTH_TOKEN": "test-auth-token",
    "RENDER": "false",
    "DEBUG_SMS": "false",
}


def _make_service():
    from services.sms_service import MessageCentralSMS
    with patch.dict(os.environ, FAKE_ENV, clear=False):
        return MessageCentralSMS()


class RetryPolicyTests(unittest.TestCase):
    """Proves the actual, real Retry object mounted on the session --
    not a re-implementation of it -- no longer allows the send-otp POST
    to be retried, while verify_otp's GET still is."""

    def setUp(self):
        self.svc = _make_service()
        self.retry = self.svc.session.get_adapter("https://cpaas.messagecentral.com").max_retries

    def test_post_is_not_retryable_on_5xx(self):
        self.assertFalse(self.retry.is_retry("POST", 503))
        self.assertFalse(self.retry.is_retry("POST", 500))

    def test_get_remains_retryable_on_5xx(self):
        self.assertTrue(self.retry.is_retry("GET", 503))

    def test_total_retry_budget_is_unchanged_for_get(self):
        # Only the allowed methods changed -- backoff/total for the GET
        # path (verify_otp) is intentionally untouched.
        self.assertEqual(self.retry.total, 3)
        self.assertEqual(self.retry.backoff_factor, 1)


class SendOtpTimeoutAndRetryTests(unittest.TestCase):
    """send_otp() itself: exactly one HTTP attempt, an explicit bounded
    timeout tuple, and a clean application-level failure (not a raised
    exception) when that attempt times out."""

    def setUp(self):
        self.svc = _make_service()

    def test_send_otp_passes_explicit_bounded_timeout_tuple(self):
        mock_post = MagicMock()
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"data": {"verificationId": "vid-1"}}
        with patch.object(self.svc.session, "post", mock_post):
            self.svc.send_otp("9876543210")
        self.assertEqual(mock_post.call_count, 1, "no duplicate SMS-send retry may occur")
        self.assertEqual(mock_post.call_args.kwargs.get("timeout"), (5, 15))

    def test_send_otp_read_timeout_becomes_clean_application_error(self):
        import requests
        mock_post = MagicMock(side_effect=requests.exceptions.ReadTimeout("timed out"))
        with patch.object(self.svc.session, "post", mock_post):
            success, response, verification_id = self.svc.send_otp("9876543210")
        self.assertEqual(mock_post.call_count, 1, "a read timeout must not trigger an internal retry loop")
        self.assertFalse(success)
        self.assertIsNone(verification_id)
        self.assertIn("error", response)

    def test_send_otp_connect_timeout_becomes_clean_application_error(self):
        import requests
        mock_post = MagicMock(side_effect=requests.exceptions.ConnectTimeout("timed out"))
        with patch.object(self.svc.session, "post", mock_post):
            success, response, verification_id = self.svc.send_otp("9876543210")
        self.assertEqual(mock_post.call_count, 1)
        self.assertFalse(success)
        self.assertIsNone(verification_id)

    def test_send_otp_still_succeeds_on_the_happy_path(self):
        mock_post = MagicMock()
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"data": {"verificationId": "vid-2"}}
        with patch.object(self.svc.session, "post", mock_post):
            success, response, verification_id = self.svc.send_otp("9876543210")
        self.assertTrue(success)
        self.assertEqual(verification_id, "vid-2")


class VerifyOtpRemainsFunctionalTests(unittest.TestCase):
    """verify_otp() keeps working exactly as before -- only its timeout
    became an explicit tuple, not a functional/behavioral change."""

    def setUp(self):
        self.svc = _make_service()

    def test_verify_otp_passes_explicit_bounded_timeout_tuple(self):
        mock_get = MagicMock()
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = "{}"
        mock_get.return_value.json.return_value = {"verified": True}
        mock_get.return_value.request.headers = {}
        mock_get.return_value.request.url = "https://cpaas.messagecentral.com/verification/v3/validateOtp"
        mock_get.return_value.request.method = "GET"
        with patch.object(self.svc.session, "get", mock_get):
            self.svc.verify_otp("vid-1", "123456")
        self.assertEqual(mock_get.call_args.kwargs.get("timeout"), (5, 15))

    def test_verify_otp_success_path_unchanged(self):
        mock_get = MagicMock()
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = "{}"
        mock_get.return_value.json.return_value = {"verified": True}
        mock_get.return_value.request.headers = {}
        mock_get.return_value.request.url = "https://cpaas.messagecentral.com/verification/v3/validateOtp"
        mock_get.return_value.request.method = "GET"
        with patch.object(self.svc.session, "get", mock_get):
            success, data = self.svc.verify_otp("vid-1", "123456")
        self.assertTrue(success)
        self.assertEqual(data, {"verified": True})

    def test_verify_otp_failure_path_returns_clean_error_not_exception(self):
        import requests
        mock_get = MagicMock(side_effect=requests.exceptions.ReadTimeout("timed out"))
        with patch.object(self.svc.session, "get", mock_get):
            success, data = self.svc.verify_otp("vid-1", "123456")
        self.assertFalse(success)
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
