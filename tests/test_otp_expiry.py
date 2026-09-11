"""OTP expiry/resend-cooldown regression tests for routes/auth_routes.py.

Everything DB-facing is a small controllable fake (no real Postgres) and
no test ever sleeps: boundary behavior is exercised by directly
controlling what get_verification()'s query would return at a given
instant (seconds_since_created / whether a row exists at all) -- exactly
what PostgreSQL's own NOW()-based computation produces, just without
waiting for real time to pass.

Covers the OTP-expiry bug (store_verification() hardcoded a 60s window
that ignored VERIFICATION_EXPIRY_SECONDS, so a genuinely fresh OTP could
be rejected as "already used or expired") and the resend-cooldown fix
(previously identical to OTP validity -- a resend was blocked for the
OTP's entire lifetime instead of a short, separate cooldown).
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager

import routes.auth_routes as auth_routes


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    """Records every statement executed against it (SQL text + bind
    params) and answers every query with the same scripted row -- each
    test only ever needs one simulated DB state per assertion, matching
    how send_otp()/verify_otp() each call get_verification() at most once
    before branching."""

    def __init__(self, fetchone_result=None):
        self.executed = []
        self.fetchone_result = fetchone_result

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        return _FakeResult(self.fetchone_result)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, fetchone_result=None):
        self.conn = _FakeConnection(fetchone_result)

    def connect(self):
        return self.conn


def _otp_row(seconds_since_created, verification_id="verif-1", attempts=0):
    """What get_verification() returns for a row PostgreSQL's own
    `expires_at > NOW()` filter still considers live -- `None` (passed as
    the engine's fetchone_result directly) represents a row the filter
    has excluded (expired or never existed), matching what a real expired
    row looks like from get_verification()'s caller's point of view."""
    return SimpleNamespace(_mapping={
        "verification_id": verification_id,
        "attempts": attempts,
        "expires_at": None,
        "created_at": None,
        "seconds_since_created": seconds_since_created,
    })


def _make_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
        JWT_TOKEN_LOCATION=["cookies", "headers"],
        JWT_COOKIE_SECURE=False,
        JWT_COOKIE_SAMESITE="Lax",
        JWT_COOKIE_HTTPONLY=True,
        JWT_COOKIE_CSRF_PROTECT=False,
    )
    JWTManager(app)
    app.register_blueprint(auth_routes.auth_bp, url_prefix="/api/auth")
    return app


class _FakeSmsService:
    def __init__(self, verify_result=(True, {"data": {}})):
        self.sent = []
        self.verified = []
        self._verify_result = verify_result

    def send_otp(self, phone):
        self.sent.append(phone)
        return True, {"data": {"verificationId": "verif-1"}}, "verif-1"

    def verify_otp(self, verification_id, otp):
        self.verified.append((verification_id, otp))
        return self._verify_result


class OtpExpiryAndCooldownTests(unittest.TestCase):
    def setUp(self):
        # send-otp/resend-otp are IP-rate-limited via the single global
        # extensions.limiter shared across every test in this process
        # (routes.auth_routes._limit() captured its own reference to it at
        # first import). Reset it so this class's own calls can't be
        # starved by unrelated tests -- e.g. test_referral_attribution.py
        # -- that happen to hit the same real send-otp/resend-otp routes
        # earlier in the same pytest run. Test-isolation only; no product
        # behavior changes.
        if getattr(auth_routes, "limiter", None) is not None:
            auth_routes.limiter.reset()
        self.app = _make_app()
        self.client = self.app.test_client()

        # Referral/user-account plumbing is unrelated to this bug (already
        # DB-heavy and separately testable) -- stub it so these tests stay
        # focused on OTP expiry/cooldown, matching this file's own
        # docstring scope.
        for name, value in (
            ("persist_referral_for_phone", (True, None)),
            ("resolve_referrer_id_for_signup", (None, None)),
            ("get_or_create_user", (
                {
                    "id": 1,
                    "phone": "919876543210",
                    "role": "free",
                    "referral_code": "ABCDEFGH",
                    "referred_by": None,
                },
                "existing",
            )),
        ):
            patcher = patch.object(auth_routes, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = patch.object(auth_routes, "clear_pending_referral", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _verify(self, engine, sms_service, otp="123456"):
        with patch.object(auth_routes, "engine", engine), \
             patch.object(auth_routes, "get_sms_service", return_value=sms_service):
            return self.client.post(
                "/api/auth/verify-otp",
                json={"phone": "9876543210", "otp": otp},
            )

    def _send(self, engine, sms_service):
        with patch.object(auth_routes, "engine", engine), \
             patch.object(auth_routes, "get_sms_service", return_value=sms_service):
            return self.client.post(
                "/api/auth/send-otp",
                json={"phone": "9876543210"},
            )

    # --- correct OTP -----------------------------------------------------

    def test_correct_otp_succeeds_within_validity_window(self):
        engine = _FakeEngine(_otp_row(seconds_since_created=5))
        sms = _FakeSmsService()
        res = self._verify(engine, sms)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

    def test_correct_otp_succeeds_just_before_configured_expiry(self):
        # 1 second of margin -- still inside PostgreSQL's expires_at > NOW()
        # filter, so get_verification() must still return the row.
        engine = _FakeEngine(
            _otp_row(seconds_since_created=auth_routes.VERIFICATION_EXPIRY_SECONDS - 1)
        )
        sms = _FakeSmsService()
        res = self._verify(engine, sms)
        self.assertEqual(res.status_code, 200)

    # --- expired OTP -------------------------------------------------------

    def test_expired_otp_fails_with_the_expired_message_not_a_false_reject(self):
        # Once expires_at <= NOW(), PostgreSQL's own WHERE filter in
        # get_verification() excludes the row -- fetchone() returns None,
        # exactly like a genuinely expired (or never-requested) row would.
        engine = _FakeEngine(None)
        sms = _FakeSmsService()
        res = self._verify(engine, sms)
        self.assertEqual(res.status_code, 401)
        body = res.get_json()
        self.assertEqual(body["reason"], "ALREADY_CONSUMED")
        self.assertIn("expired", body["message"])
        self.assertEqual(
            sms.verified, [],
            "an expired/missing row must never even reach the SMS provider",
        )

    # --- wrong OTP (must not be confused with expiry) -----------------------

    def test_wrong_otp_within_validity_window_is_incorrect_not_expired(self):
        engine = _FakeEngine(_otp_row(seconds_since_created=5))
        sms = _FakeSmsService(verify_result=(False, {"error": "mismatch"}))
        res = self._verify(engine, sms, otp="000000")
        self.assertEqual(res.status_code, 401)
        body = res.get_json()
        self.assertEqual(body["message"], "Incorrect OTP. Please try again.")
        self.assertNotIn("reason", body)

    # --- resend cooldown, separate from OTP validity ------------------------

    def test_resend_before_cooldown_elapsed_returns_429(self):
        engine = _FakeEngine(_otp_row(seconds_since_created=5))
        sms = _FakeSmsService()
        res = self._send(engine, sms)
        self.assertEqual(res.status_code, 429)
        self.assertIn(str(auth_routes.RESEND_COOLDOWN_SECONDS), res.get_json()["message"])
        self.assertEqual(
            sms.sent, [], "a cooldown-blocked resend must never call the SMS provider",
        )

    def test_resend_does_not_overwrite_the_existing_row_while_in_cooldown(self):
        engine = _FakeEngine(_otp_row(seconds_since_created=1))
        self._send(engine, _FakeSmsService())
        self.assertTrue(
            all("INSERT INTO otp_verifications" not in sql for sql, _ in engine.conn.executed),
            "store_verification() must only run once the cooldown gate passes",
        )

    def test_resend_after_cooldown_but_before_expiry_sends_a_fresh_otp(self):
        # Halfway between the cooldown and full validity: the pre-fix code
        # tied resend-blocking to validity, not cooldown, and would have
        # rejected this as "still valid" -- decoupling the two is exactly
        # this fix.
        midpoint = (auth_routes.RESEND_COOLDOWN_SECONDS + auth_routes.VERIFICATION_EXPIRY_SECONDS) / 2
        engine = _FakeEngine(_otp_row(seconds_since_created=midpoint))
        sms = _FakeSmsService()
        res = self._send(engine, sms)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(sms.sent), 1)

    def test_resend_cooldown_is_shorter_than_otp_validity(self):
        """Regression guard for the exact bug being fixed: cooldown and
        validity must be independently configured, and cooldown must be
        the strictly shorter of the two (otherwise a legitimate resend
        would be blocked for the OTP's entire lifetime again)."""
        self.assertLess(
            auth_routes.RESEND_COOLDOWN_SECONDS,
            auth_routes.VERIFICATION_EXPIRY_SECONDS,
        )

    # --- VERIFICATION_EXPIRY_SECONDS is the real, single source of truth ---

    def test_verification_expiry_seconds_constant_is_actually_used(self):
        engine = _FakeEngine(None)
        self._send(engine, _FakeSmsService())
        insert_calls = [
            params for sql, params in engine.conn.executed
            if "INSERT INTO otp_verifications" in sql
        ]
        self.assertEqual(len(insert_calls), 1)
        self.assertEqual(
            insert_calls[0]["expiry_seconds"], auth_routes.VERIFICATION_EXPIRY_SECONDS,
        )

    def test_expiry_is_computed_by_postgres_make_interval_not_a_hardcoded_literal(self):
        engine = _FakeEngine(None)
        self._send(engine, _FakeSmsService())
        insert_sql = next(
            sql for sql, _ in engine.conn.executed if "INSERT INTO otp_verifications" in sql
        )
        self.assertIn("NOW() + make_interval(secs => :expiry_seconds)", insert_sql)
        self.assertNotIn(
            "INTERVAL '", insert_sql,
            "no hardcoded interval literal should remain in store_verification()",
        )


if __name__ == "__main__":
    unittest.main()
