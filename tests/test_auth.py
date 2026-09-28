"""Pre-launch audit, section 1: Auth (OTP login and registration).

One test class per checkbox in the "LANDMARK Pre-Launch Audit" doc.

What is real and what is mocked
-------------------------------
* The Flask app is the real one (``app.py``) with the real blueprints.
* SMS is mocked: ``FakeSms`` stands in for Message Central, issues its own
  6-digit codes and verification ids, and never touches the network.
* Redis is fakeredis (JWT blocklist). The rate limiter uses in-memory
  storage because REDIS_URL is forced empty.
* OTP rows, users and admin_settings live in a THROWAWAY local PostgreSQL
  database, because the auth code relies on PostgreSQL-only SQL
  (NOW(), make_interval, ON CONFLICT, the UNIQUE index on users.phone).
  "Time passing" is simulated by moving created_at/expires_at back in the
  test database, which is exactly what PostgreSQL's NOW() comparisons see.

Running
-------
    set TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/landmark_test
    pytest tests/test_auth.py -v

TEST_DATABASE_URL must point at localhost/127.0.0.1/::1 -- anything else is
refused before a single query runs. Its tables are TRUNCATEd on every test,
so never point it at a database you care about. Without TEST_DATABASE_URL
the database-backed tests are skipped; the production-cookie test still
runs.

.env is never read by this module (python-dotenv is disabled before the
app is imported), so no live key, live database or live SMS credential can
leak into a test run from it.
"""
import os
import random
import subprocess
import sys
import textwrap
import threading
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_local_url(url):
    try:
        return (urlparse(url).hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


TEST_DB_URL = (os.getenv("TEST_DATABASE_URL") or "").strip()
if TEST_DB_URL and not _is_local_url(TEST_DB_URL):
    raise RuntimeError(
        "TEST_DATABASE_URL must point at a local PostgreSQL (localhost/127.0.0.1); "
        "refusing to run auth tests against a remote database."
    )

# --- Environment isolation (must happen before any project import) -------
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False  # never read .env in this module

os.environ["DATABASE_URL"] = TEST_DB_URL or "postgresql://test:test@127.0.0.1:5432/test"
os.environ["REDIS_URL"] = ""
os.environ["DEBUG_SMS"] = "false"
os.environ.pop("RENDER", None)
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")

import fakeredis  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

import database.init_db as dbmod  # noqa: E402

# If another test module imported the app first with a non-local DATABASE_URL,
# stop here rather than risk a query against it.
if not _is_local_url(str(dbmod.DATABASE_URL or "")):
    raise RuntimeError("database.init_db was imported with a non-local DATABASE_URL; refusing to run.")

# app MUST be imported before routes.auth_routes: the OTP rate limits are bound
# at import time and only exist if extensions.init_extensions() ran first
# (exactly what gunicorn's `app:app` does in production).
from app import app as flask_app  # noqa: E402
import redis_client  # noqa: E402
import routes.auth_routes as auth_routes  # noqa: E402
from services import jwt_blocklist  # noqa: E402

# Another test module (e.g. test_otp_expiry.py) may have imported
# routes.auth_routes directly, before any limiter existed. The decorators are
# then permanently unlimited in this process -- a test-ordering artefact, not
# production behaviour -- so the rate-limit tests skip instead of lying.
_LIMITER_BOUND = auth_routes.limiter is not None
needs_limiter = pytest.mark.skipif(
    not _LIMITER_BOUND,
    reason="routes.auth_routes was imported before the app in this pytest process; "
           "run tests/test_auth.py on its own to exercise rate limits",
)

needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="set TEST_DATABASE_URL to a local test PostgreSQL")

HTML = {"Accept": "text/html"}
JSON = {"Accept": "application/json"}


# =========================================================================
# Fakes
# =========================================================================
class FakeSms:
    """Message Central stand-in.

    send_otp issues a new verification id + 6-digit code per call, unless
    ``reuse_pending`` is set, in which case it mimics Message Central's
    responseCode 506 REQUEST_ALREADY_EXISTS: same verification id, no new
    code. verify_otp succeeds once per verification id (a used id is dead).
    """

    def __init__(self):
        self.codes = {}          # verification_id -> code
        self.by_phone = {}       # full phone -> latest verification_id
        self.used = set()
        self.sent = []
        self.reuse_pending = False
        self._n = 0

    def send_otp(self, full_phone):
        self.sent.append(full_phone)
        if self.reuse_pending and full_phone in self.by_phone:
            vid = self.by_phone[full_phone]
            return True, {"responseCode": 506, "message": "REQUEST_ALREADY_EXISTS"}, vid
        self._n += 1
        vid = f"vid-{self._n}"
        self.codes[vid] = f"{random.randint(0, 999999):06d}"
        self.by_phone[full_phone] = vid
        return True, {"data": {"verificationId": vid}}, vid

    def verify_otp(self, verification_id, otp):
        if verification_id in self.used:
            return False, {"message": "already used"}
        if self.codes.get(verification_id) == otp:
            self.used.add(verification_id)
            return True, {"data": {"verificationStatus": "VERIFICATION_COMPLETED"}}
        return False, {"message": "WRONG_OTP"}

    def code_for(self, phone10):
        return self.codes[self.by_phone["91" + phone10]]

    def wrong_code_for(self, phone10):
        right = self.code_for(phone10)
        return "000000" if right != "000000" else "111111"


# =========================================================================
# Fixtures
# =========================================================================
@pytest.fixture(scope="module")
def pg_engine():
    if not TEST_DB_URL:
        pytest.skip("set TEST_DATABASE_URL to a local test PostgreSQL")
    eng = create_engine(TEST_DB_URL)
    with eng.connect() as conn:
        dbmod._init_db_body(conn)
        conn.commit()
    yield eng
    eng.dispose()


@pytest.fixture
def sms(monkeypatch):
    fake = FakeSms()
    monkeypatch.setattr(auth_routes, "get_sms_service", lambda: fake)
    return fake


@pytest.fixture
def fake_redis(monkeypatch):
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(redis_client, "_redis_client", r)
    jwt_blocklist.reset_memory_for_tests()
    yield r
    jwt_blocklist.reset_memory_for_tests()


@pytest.fixture
def db(pg_engine, monkeypatch):
    """Point every auth code path at the throwaway DB and wipe it."""
    monkeypatch.setattr(dbmod, "engine", pg_engine)
    monkeypatch.setattr(auth_routes, "engine", pg_engine)
    with pg_engine.connect() as conn:
        conn.execute(text(
            "TRUNCATE users, otp_verifications, pending_referrals RESTART IDENTITY CASCADE"
        ))
        # Put the OTP settings back to exactly what init_db seeds, so every
        # test sees the out-of-the-box configuration.
        for key, val in _seeded_admin_defaults():
            if key.startswith("otp_"):
                conn.execute(
                    text("UPDATE admin_settings SET value = :v WHERE key = :k"), {"k": key, "v": val}
                )
        conn.commit()
    return pg_engine


@pytest.fixture
def client(db, sms, fake_redis):
    flask_app.config["TESTING"] = True
    if auth_routes.limiter is not None:
        auth_routes.limiter.reset()
    ip = f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
    return flask_app.test_client(), ip


# =========================================================================
# Helpers
# =========================================================================
def _phone():
    return f"9{random.randint(100000000, 999999999)}"


def _post(c, ip, path, body, headers=None):
    return c.post(path, json=body, headers=headers or JSON, environ_base={"REMOTE_ADDR": ip})


def send(c, ip, phone):
    return _post(c, ip, "/api/auth/send-otp", {"phone": phone})


def resend(c, ip, phone):
    return _post(c, ip, "/api/auth/resend-otp", {"phone": phone})


def verify(c, ip, phone, otp, remember_me=False):
    return _post(c, ip, "/api/auth/verify-otp", {"phone": phone, "otp": otp, "remember_me": remember_me})


def login(c, ip, sms, phone, remember_me=False):
    r = send(c, ip, phone)
    assert r.status_code == 200, r.get_json()
    r = verify(c, ip, phone, sms.code_for(phone), remember_me=remember_me)
    assert r.status_code == 200, r.get_json()
    return r


def age_otp(engine, phone10, seconds):
    """Move the OTP row `seconds` into the past (as if that time had passed)."""
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE otp_verifications
               SET created_at = created_at - make_interval(secs => :s),
                   expires_at = expires_at - make_interval(secs => :s)
             WHERE phone = :p
        """), {"s": seconds, "p": "91" + phone10})
        conn.commit()


def otp_row(engine, phone10):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM otp_verifications WHERE phone = :p"), {"p": "91" + phone10}
        ).fetchone()
    return dict(row._mapping) if row else None


def user_count(engine, phone10):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM users WHERE phone = :p"), {"p": phone10}
        ).scalar()


def set_setting(engine, key, value):
    with engine.connect() as conn:
        conn.execute(text("UPDATE admin_settings SET value = :v WHERE key = :k"), {"k": key, "v": str(value)})
        conn.commit()


def set_cookie_header(resp):
    return resp.headers.getlist("Set-Cookie")


# =========================================================================
# 1. New number registers and logs in
# =========================================================================
@needs_db
class TestNewNumberRegisters:
    def test_new_number_creates_account_and_sets_session_cookies(self, client, sms):
        c, ip = client
        phone = _phone()
        r = login(c, ip, sms, phone)
        body = r.get_json()
        assert body["success"] is True
        assert body["data"]["status"] == "new"
        assert body["data"]["user"]["phone"] == phone
        cookies = " ".join(set_cookie_header(r))
        assert "access_token=" in cookies and "refresh_token=" in cookies
        assert user_count(client_db(), phone) == 1

    def test_sms_goes_to_country_code_number_and_is_mocked(self, client, sms):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        assert sms.sent == ["91" + phone]

    def test_new_session_can_call_protected_api(self, client, sms):
        c, ip = client
        phone = _phone()
        login(c, ip, sms, phone)
        r = c.get("/api/auth/me", headers=JSON)
        assert r.status_code == 200
        assert r.get_json()["phone"] == phone


# =========================================================================
# 2. Existing number logs in (no duplicate user created)
# =========================================================================
@needs_db
class TestNoDuplicateUsers:
    def test_second_login_reuses_same_account(self, client, sms, db):
        c, ip = client
        phone = _phone()
        first = login(c, ip, sms, phone).get_json()["data"]
        age_otp(db, phone, 61)
        second = login(c, ip, sms, phone).get_json()["data"]
        assert first["status"] == "new"
        assert second["status"] == "existing"
        assert first["user"]["id"] == second["user"]["id"]
        assert user_count(db, phone) == 1

    def test_different_phone_formats_map_to_one_account(self, client, sms, db):
        c, ip = client
        phone = _phone()
        ids = set()
        for fmt in (phone, "+91 " + phone[:5] + " " + phone[5:], "0" + phone):
            r = send(c, ip, fmt)
            assert r.status_code == 200, r.get_json()
            r = verify(c, ip, fmt, sms.code_for(phone))
            assert r.status_code == 200, r.get_json()
            ids.add(r.get_json()["data"]["user"]["id"])
            age_otp(db, phone, 61)
        assert len(ids) == 1
        assert user_count(db, phone) == 1

    def test_concurrent_first_logins_create_one_user(self, db):
        phone = _phone()
        results, errors = [], []

        def worker():
            try:
                with flask_app.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
                    results.append(auth_routes.get_or_create_user(phone)[0]["id"])
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert len(set(results)) == 1
        assert user_count(db, phone) == 1

    def test_users_phone_has_unique_index(self, db):
        with db.connect() as conn:
            rows = conn.execute(text("""
                SELECT indexdef FROM pg_indexes
                 WHERE tablename = 'users' AND indexdef ILIKE '%UNIQUE%' AND indexdef ILIKE '%(phone)%'
            """)).fetchall()
        assert rows, "users.phone has no UNIQUE index -- duplicate accounts are possible"


# =========================================================================
# 3. Wrong OTP rejected
# =========================================================================
@needs_db
class TestWrongOtpRejected:
    def test_wrong_otp_is_401_sets_no_cookie_and_counts_attempt(self, client, sms, db):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        r = verify(c, ip, phone, sms.wrong_code_for(phone))
        assert r.status_code == 401
        assert r.get_json()["success"] is False
        assert not any("access_token=" in h and "access_token=;" not in h for h in set_cookie_header(r))
        assert otp_row(db, phone)["attempts"] == 1
        assert user_count(db, phone) == 0

    def test_malformed_otp_is_400(self, client, sms):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        assert verify(c, ip, phone, "12ab").status_code == 400


# =========================================================================
# 4. 3 wrong OTPs lock the number for a cooldown period
# =========================================================================
@needs_db
class TestLockoutAfterThreeWrongOtps:
    def test_default_max_attempts_is_three(self):
        assert auth_routes.MAX_OTP_ATTEMPTS == 3

    def test_seeded_admin_setting_is_three(self):
        seeded = dict(
            (k, v) for k, v in _seeded_admin_defaults() if k == "otp_max_attempts"
        )
        assert seeded.get("otp_max_attempts") == "3"

    def test_correct_otp_after_three_wrong_is_refused(self, client, sms):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        for _ in range(3):
            verify(c, ip, phone, sms.wrong_code_for(phone))
        r = verify(c, ip, phone, sms.code_for(phone))
        assert r.status_code == 429, r.get_json()

    def test_number_stays_locked_for_new_otp_requests(self, client, sms, db):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        for _ in range(3):
            verify(c, ip, phone, sms.wrong_code_for(phone))
        lockout = getattr(auth_routes, "OTP_LOCKOUT_SECONDS", None)
        assert lockout, "no lockout period: a locked number can request a fresh OTP immediately"
        age_otp(db, phone, 61)  # past the resend cooldown, still inside the lockout
        assert send(c, ip, phone).status_code == 429
        assert resend(c, ip, phone).status_code == 429

    def test_lock_expires_after_cooldown(self, client, sms, db):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        for _ in range(3):
            verify(c, ip, phone, sms.wrong_code_for(phone))
        lockout = getattr(auth_routes, "OTP_LOCKOUT_SECONDS", None)
        assert lockout
        age_otp(db, phone, lockout + 1)
        assert send(c, ip, phone).status_code == 200
        assert verify(c, ip, phone, sms.code_for(phone)).status_code == 200

    def test_resend_of_same_pending_code_does_not_reset_attempts(self, client, sms, db):
        """Message Central answers a resend with 506 + the SAME verification
        id while the first code is still live. The wrong-attempt counter on
        that same code must survive the resend, or the lock can be dodged."""
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        verify(c, ip, phone, sms.wrong_code_for(phone))
        verify(c, ip, phone, sms.wrong_code_for(phone))
        sms.reuse_pending = True
        age_otp(db, phone, 61)
        assert resend(c, ip, phone).status_code == 200
        assert otp_row(db, phone)["attempts"] == 2


# =========================================================================
# 5. Expired OTP (older than 5 min) rejected
# =========================================================================
@needs_db
class TestOtpExpiry:
    def test_default_validity_is_at_most_five_minutes(self):
        assert auth_routes.VERIFICATION_EXPIRY_SECONDS <= 300

    def test_stored_row_expires_within_five_minutes(self, client, sms, db):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        row = otp_row(db, phone)
        assert (row["expires_at"] - row["created_at"]).total_seconds() <= 300

    def test_otp_older_than_five_minutes_is_rejected(self, client, sms, db):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        age_otp(db, phone, 301)
        r = verify(c, ip, phone, sms.code_for(phone))
        assert r.status_code == 401
        assert user_count(db, phone) == 0

    def test_admin_setting_cannot_raise_validity_above_five_minutes(self, db):
        set_setting(db, "otp_verification_expiry_seconds", 3600)
        assert auth_routes._verification_expiry_seconds() <= 300


# =========================================================================
# 6. Used OTP cannot be reused
# =========================================================================
@needs_db
class TestOtpSingleUse:
    def test_same_code_cannot_log_in_twice(self, client, sms, db):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        code = sms.code_for(phone)
        assert verify(c, ip, phone, code).status_code == 200
        r = verify(c, ip, phone, code)
        assert r.status_code == 401
        assert otp_row(db, phone) is None


# =========================================================================
# 7. Resend works and invalidates the previous OTP
# =========================================================================
@needs_db
class TestResend:
    def test_resend_sends_new_code_and_old_code_stops_working(self, client, sms, db):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        old_code = sms.code_for(phone)
        age_otp(db, phone, 61)
        r = resend(c, ip, phone)
        assert r.status_code == 200
        new_code = sms.code_for(phone)
        assert len(sms.sent) == 2
        assert verify(c, ip, phone, old_code).status_code == 401
        assert verify(c, ip, phone, new_code).status_code == 200

    def test_resend_inside_cooldown_is_refused(self, client, sms):
        c, ip = client
        phone = _phone()
        send(c, ip, phone)
        assert resend(c, ip, phone).status_code == 429
        assert len(sms.sent) == 1


# =========================================================================
# 8. More than 5 OTP requests per number per hour blocked (+ per-IP limit)
# =========================================================================
# Send and resend are alternated so no single endpoint reaches its own
# "5 per minute" per-IP limit: the 429 must come from the per-number limit.
@needs_db
@needs_limiter
class TestOtpRequestRateLimits:
    def test_sixth_otp_request_for_a_number_in_an_hour_is_blocked(self, client, sms, db):
        c, ip = client
        phone = _phone()
        codes = []
        for path in (send, resend, send, resend, send, resend):
            codes.append(path(c, ip, phone).status_code)
            age_otp(db, phone, 61)  # clear the 60 s resend cooldown each time
        assert codes == [200] * 5 + [429], codes
        assert len(sms.sent) == 5

    def test_limit_is_shared_between_send_and_resend(self, client, sms, db):
        c, ip = client
        phone = _phone()
        codes = []
        for path in (send, resend, resend, send, resend, send):
            codes.append(path(c, ip, phone).status_code)
            age_otp(db, phone, 61)
        assert codes == [200] * 5 + [429], codes

    def test_per_number_limit_applies_across_ips(self, client, sms, db):
        c, _ = client
        phone = _phone()
        codes = []
        for n in range(6):
            codes.append(send(c, f"10.9.9.{n + 1}", phone).status_code)
            age_otp(db, phone, 61)
        assert codes == [200] * 5 + [429], codes

    def test_per_ip_limit_blocks_many_numbers_from_one_ip(self, client, sms):
        c, ip = client
        codes = [send(c, ip, _phone()).status_code for _ in range(6)]
        assert codes[:5] == [200] * 5
        assert codes[5] == 429

    def test_rate_limiting_is_enabled(self):
        assert flask_app.config.get("RATELIMIT_ENABLED", True) is not False


# =========================================================================
# 9. JWT cookie is Secure and HttpOnly on production
# =========================================================================
_PROD_PROBE = textwrap.dedent(r"""
    import os, sys, json
    sys.path.insert(0, sys.argv[1])
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    os.environ.update({
        "RENDER": "true",
        "SECRET_KEY": "probe", "JWT_SECRET_KEY": "probe-jwt-secret-key-32-bytes-long!",
        # closed local port: init_db fails fast, nothing remote is contacted
        "DATABASE_URL": "postgresql://probe:probe@127.0.0.1:1/probe",
        "REDIS_URL": "", "BASE_URL": "https://example.invalid",
    })
    import database.init_db as d
    d.INIT_DB_STARTUP_TIMEOUT_SECONDS = 5
    import app as m
    from flask import jsonify
    from flask_jwt_extended import create_access_token, create_refresh_token, set_access_cookies, set_refresh_cookies
    with m.app.test_request_context("/"):
        r = jsonify(ok=True)
        set_access_cookies(r, create_access_token(identity="1"))
        set_refresh_cookies(r, create_refresh_token(identity="1"))
        headers = r.headers.getlist("Set-Cookie")
    print("PROBE" + json.dumps({
        "JWT_COOKIE_SECURE": m.app.config["JWT_COOKIE_SECURE"],
        "JWT_COOKIE_HTTPONLY": m.app.config.get("JWT_COOKIE_HTTPONLY", True),
        "SESSION_COOKIE_SECURE": m.app.config["SESSION_COOKIE_SECURE"],
        "headers": headers,
    }))
""")


@pytest.fixture(scope="module")
def prod_probe(tmp_path_factory):
    import json

    script = tmp_path_factory.mktemp("probe") / "probe.py"
    script.write_text(_PROD_PROBE)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_URL", "REDIS_URL"))}
    out = subprocess.run(
        [sys.executable, str(script), str(ROOT)], capture_output=True, text=True, timeout=120, env=env
    )
    line = next((l for l in out.stdout.splitlines() if l.startswith("PROBE")), None)
    assert line, f"production probe failed:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}"
    return json.loads(line[len("PROBE"):])


class TestProductionCookieFlags:
    def test_production_config_sets_secure_and_httponly(self, prod_probe):
        assert prod_probe["JWT_COOKIE_SECURE"] is True
        assert prod_probe["JWT_COOKIE_HTTPONLY"] is True
        assert prod_probe["SESSION_COOKIE_SECURE"] is True

    def test_production_token_cookies_carry_both_flags(self, prod_probe):
        token_cookies = [
            h for h in prod_probe["headers"]
            if h.startswith(("access_token=", "refresh_token="))
        ]
        assert len(token_cookies) == 2
        for h in token_cookies:
            assert "Secure" in h, h
            assert "HttpOnly" in h, h

    def test_csrf_cookies_are_secure_but_readable_by_js(self, prod_probe):
        csrf = [h for h in prod_probe["headers"] if h.startswith("csrf_")]
        assert csrf
        for h in csrf:
            assert "Secure" in h, h
            assert "HttpOnly" not in h, h  # the double-submit token must be JS-readable


# =========================================================================
# 10. Logout invalidates the session (old cookie gets 401)
# =========================================================================
@needs_db
class TestLogoutRevokes:
    def _login_and_capture(self, c, ip, sms, remember_me=False):
        phone = _phone()
        login(c, ip, sms, phone, remember_me=remember_me)
        access = c.get_cookie("access_token")
        refresh = _find_cookie(c, "refresh_token")
        csrf_refresh = c.get_cookie("csrf_refresh_token")
        assert access and refresh and csrf_refresh
        self.refresh_path = refresh.path
        return phone, access.value, refresh.value, csrf_refresh.value

    @pytest.mark.parametrize("logout_path", ["/api/auth/logout", "/api/user/logout"])
    def test_old_access_cookie_is_rejected_after_logout(self, client, sms, fake_redis, logout_path):
        c, ip = client
        _, access, _, _ = self._login_and_capture(c, ip, sms)
        assert c.post(logout_path, headers=JSON).status_code == 200
        assert c.get_cookie("access_token") is None  # cookie cleared ...
        c.set_cookie("access_token", access)          # ... attacker replays it
        r = c.get("/api/auth/me", headers=JSON)
        assert r.status_code == 401
        assert fake_redis.keys("jwt_blocklist:*"), "revocation must go to the shared store (Redis)"

    def test_logout_page_revokes_access_cookie(self, client, sms):
        c, ip = client
        _, access, _, _ = self._login_and_capture(c, ip, sms)
        assert c.get("/logout", headers=HTML).status_code == 200
        c.set_cookie("access_token", access)
        assert c.get("/api/auth/me", headers=JSON).status_code == 401

    def test_old_refresh_cookie_cannot_mint_new_session_after_logout(self, client, sms):
        c, ip = client
        _, _, refresh, csrf_refresh = self._login_and_capture(c, ip, sms)
        assert c.post("/api/auth/logout", headers=JSON).status_code == 200
        assert _find_cookie(c, "refresh_token") is None  # browser copy cleared ...
        c.set_cookie("refresh_token", refresh, path=self.refresh_path)  # ... attacker replays it
        c.set_cookie("csrf_refresh_token", csrf_refresh)
        r = c.post("/api/refresh", headers={**JSON, "X-CSRF-TOKEN": csrf_refresh})
        assert r.status_code == 401, "refresh token still valid after logout"

    def test_refreshed_access_token_stays_short_lived_with_remember_me(self, client, sms):
        """A long-lived access token can't be withdrawn once the in-memory
        blocklist is lost (restart/deploy without Redis)."""
        from flask_jwt_extended import decode_token

        c, ip = client
        _, _, refresh, csrf_refresh = self._login_and_capture(c, ip, sms, remember_me=True)
        r = c.post("/api/refresh", headers={**JSON, "X-CSRF-TOKEN": csrf_refresh})
        assert r.status_code == 200, r.get_json()
        with flask_app.app_context():
            claims = decode_token(c.get_cookie("access_token").value)
        assert claims["exp"] - claims["iat"] <= auth_routes.ACCESS_TOKEN_TTL.total_seconds()


# =========================================================================
# Remember-me session length: refresh token lasts 30 days (7 without it)
# =========================================================================
class TestRememberMeLifetime:
    USER = {"id": 1, "role": "free", "phone": "9876543210"}

    def _refresh_lifetime(self, token):
        from flask_jwt_extended import decode_token

        with flask_app.app_context():
            claims = decode_token(token)
        return claims["exp"] - claims["iat"]

    def test_remember_me_refresh_token_expires_in_30_days(self):
        with flask_app.app_context():
            _, refresh, _, refresh_expires = auth_routes.generate_jwt_tokens(self.USER, remember_me=True)
        assert refresh_expires == timedelta(days=30)
        assert self._refresh_lifetime(refresh) == timedelta(days=30).total_seconds()

    def test_normal_refresh_token_still_expires_in_7_days(self):
        with flask_app.app_context():
            _, refresh, _, refresh_expires = auth_routes.generate_jwt_tokens(self.USER, remember_me=False)
        assert refresh_expires == timedelta(days=7)
        assert self._refresh_lifetime(refresh) == timedelta(days=7).total_seconds()

    @needs_db
    def test_remember_me_login_sets_30_day_refresh_cookie(self, client, sms):
        c, ip = client
        r = login(c, ip, sms, _phone(), remember_me=True)
        refresh_header = next(
            h for h in r.headers.getlist("Set-Cookie") if h.startswith("refresh_token=")
        )
        assert f"Max-Age={int(timedelta(days=30).total_seconds())}" in refresh_header, refresh_header
        assert self._refresh_lifetime(_find_cookie(c, "refresh_token").value) == timedelta(days=30).total_seconds()


# =========================================================================
# 11. Protected pages redirect to login when logged out
# =========================================================================
@needs_db
class TestProtectedPagesRedirect:
    @pytest.mark.parametrize("path", ["/dashboard", "/api/user/dashboard"])
    def test_logged_out_visitor_ends_on_login(self, client, path):
        c, _ = client
        r = c.get(path, headers=HTML, follow_redirects=True)
        assert r.request.path == auth_routes.LOGIN_PATH, [h.request.path for h in r.history]

    def test_after_logout_dashboard_redirects_to_login(self, client, sms):
        c, ip = client
        login(c, ip, sms, _phone())
        assert c.get("/dashboard", headers=HTML).status_code == 200
        c.post("/api/auth/logout", headers=JSON)
        r = c.get("/dashboard", headers=HTML, follow_redirects=True)
        assert r.request.path == auth_routes.LOGIN_PATH

    @pytest.mark.parametrize("api", ["/api/user/profile/data", "/api/wallet/overview", "/api/user/api/invite"])
    def test_page_shells_get_no_data_when_logged_out(self, client, api):
        """/profile, /wallet, /invite render a static shell for everyone; their
        data comes from these APIs, and the shell's authFetch sends the
        browser to login on the 401."""
        c, _ = client
        assert c.get(api, headers=JSON).status_code == 401

    def test_logged_out_api_call_gets_json_401(self, client):
        c, _ = client
        r = c.get("/api/auth/me", headers=JSON)
        assert r.status_code == 401


# =========================================================================
# small helpers used above
# =========================================================================
def _find_cookie(c, name):
    """Cookie by name whatever its path (werkzeug's get_cookie needs the path)."""
    for path in ("/", "/api/refresh"):
        ck = c.get_cookie(name, path=path)
        if ck is not None:
            return ck
    return None


def client_db():
    return dbmod.engine


def _seeded_admin_defaults():
    """(key, value) pairs init_db seeds into admin_settings, read from a scratch connection."""
    captured = []

    class _Rec:
        def execute(self, stmt, params=None):
            if params and "key" in params and "val" in params and "admin_settings" in str(stmt):
                captured.append((params["key"], params["val"]))
            return self

        def fetchone(self):
            return None

        def fetchall(self):
            return []

        def scalar(self):
            return None

        def commit(self):
            pass

    try:
        dbmod._init_db_body(_Rec())
    except Exception:
        pass
    return captured
