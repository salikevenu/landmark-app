"""Pre-launch audit, section 4: Fraud and Policy layer.

One test class per checkbox in the "LANDMARK Pre-Launch Audit" doc, section 4.

Pass = every abuse attempt is blocked or flagged with a logged reason, from
the policy layer (services/fraud_policy.py), not agent code.

Two layers of evidence
----------------------
* Logic tests run with no database: referral/signup helpers against a small
  in-memory users table (``_Store``); admin SQL captured from a mock connection;
  OTP rate limits in a child process against the real app with the database
  and SMS provider stubbed.
* ``needs_db`` tests run the same scenarios and the policy SQL on a real
  PostgreSQL (the SQL is PostgreSQL-only: NOW(), make_interval, LEFT). They
  are skipped unless TEST_DATABASE_URL points at a local throwaway database:

      set TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/landmark_test

  The users table there is TRUNCATEd on every test; never point it at a
  database you care about.

.env is never read here, and the module refuses to run if DATABASE_URL or
TEST_DATABASE_URL is not a local address.

xfail(strict=True) marks the one gap left open on purpose (device id).
"""
import inspect
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Environment isolation (must happen before any project import) -------
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False  # never read .env in this module

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_local_url(url):
    try:
        return (urlparse(url).hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


TEST_DB_URL = (os.getenv("TEST_DATABASE_URL") or "").strip()
if TEST_DB_URL and not _is_local_url(TEST_DB_URL):
    raise RuntimeError("TEST_DATABASE_URL must point at a local PostgreSQL; refusing to run.")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", TEST_DB_URL or "postgresql://test:test@127.0.0.1:5432/test")
os.environ["REDIS_URL"] = ""
os.environ.pop("RENDER", None)

if not _is_local_url(os.environ["DATABASE_URL"]):
    raise RuntimeError("DATABASE_URL must be a local address for tests/test_fraud_policy.py; refusing to run.")

# app MUST be imported before routes.auth_routes: the OTP rate limits are bound
# at import time and only exist if extensions.init_extensions() ran first.
import app as _app_module  # noqa: E402,F401
from flask import Flask  # noqa: E402
from flask_jwt_extended import JWTManager  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

import database.init_db as dbmod  # noqa: E402
from routes import auth_routes  # noqa: E402
from services import admin_service, fraud_policy  # noqa: E402

needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="set TEST_DATABASE_URL to a local test PostgreSQL")


# =========================================================================
# Helpers
# =========================================================================
def policy_lines(caplog, kind):
    """Messages of fraud_policy log lines of one kind: 'FLAG' or 'BLOCK'."""
    return [
        r.getMessage() for r in caplog.records
        if r.name == "services.fraud_policy" and r.getMessage().startswith(f"fraud_policy {kind}")
    ]


class _Row:
    def __init__(self, mapping):
        self._mapping = mapping

    def __getitem__(self, index):
        return list(self._mapping.values())[index]


class _Result:
    def __init__(self, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [] if self._row is None else [self._row]

    def scalar(self):
        return self._scalar


class _Store:
    """users + pending_referrals, just enough for the referral/signup helpers.
    Every account in it counts as created "just now"."""

    def __init__(self):
        self.users = {}
        self.by_phone = {}
        self.by_code = {}
        self.pending = {}
        self.next_id = 1

    def add_user(self, phone, code, ip=None, referred_by=None):
        user = {
            "id": self.next_id, "phone": phone, "name": "", "role": "free",
            "referral_code": code, "referred_by": referred_by,
            "is_blocked": 0, "is_active": 1, "ip_address": ip,
            "is_flagged": 0, "flag_reason": None,
        }
        self.next_id += 1
        self.users[user["id"]] = user
        self.by_phone[phone] = user
        self.by_code[code] = user
        return user

    @staticmethod
    def row(user):
        return None if user is None else _Row(dict(user))

    def flagged(self):
        return {u["phone"]: u for u in self.users.values() if u["is_flagged"]}


class _Conn:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        p = params or {}
        s = self.store
        # policy queries first: they also contain "from users where phone/..."
        if "select count(*) from users where ip_address" in q:
            return _Result(scalar=sum(1 for u in s.users.values() if u["ip_address"] == p["ip"]))
        if "select count(*) from users where phone like" in q:
            prefix = p["prefix"].rstrip("%")
            return _Result(scalar=sum(1 for u in s.users.values() if u["phone"].startswith(prefix)))
        if "select ip_address from users where id" in q:
            user = s.users.get(p["rid"])
            return _Result(scalar=user["ip_address"] if user else None)
        if q.startswith("update users set is_flagged"):
            user = s.users[p["uid"]]
            user["is_flagged"] = 1
            user["flag_reason"] = p["reason"] if not user["flag_reason"] else user["flag_reason"] + "; " + p["reason"]
            return _Result()
        if "from users where referral_code" in q:
            return _Result(s.row(s.by_code.get(p["code"])))
        if "from users where phone" in q:
            return _Result(s.row(s.by_phone.get(p["phone"])))
        if q.startswith("insert into users"):
            user = s.add_user(p["phone"], p["code"], ip=p.get("ip"), referred_by=p.get("referred_by"))
            return _Result(_Row({"id": user["id"]}))
        if "insert into pending_referrals" in q:
            s.pending[p["phone"]] = {"ref_code": p["ref_code"], "referrer_id": p["referrer_id"]}
            return _Result()
        if "delete from pending_referrals" in q:
            s.pending.pop(p.get("phone"), None)
            return _Result()
        if "from pending_referrals" in q:
            pending = s.pending.get(p.get("phone"))
            return _Result(_Row(dict(pending)) if pending else None)
        return _Result()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Engine:
    def __init__(self, store):
        self.store = store

    def connect(self):
        return _Conn(self.store)


@pytest.fixture
def env(monkeypatch):
    """(store, flask app) with every auth_routes DB call pointed at the store."""
    store = _Store()
    monkeypatch.setattr(auth_routes, "engine", _Engine(store))
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    return store, app


def _apart(n):
    """Phone numbers whose first 8 digits all differ, so only the IP rule can fire."""
    return f"9{n:02d}0000000"


def signup(app, phone, ip, ref=None):
    """The real referral + account-creation helpers, exactly as verify-otp calls them."""
    data = {"ref": ref} if ref else {}
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": ip}):
        ok, err = auth_routes.persist_referral_for_phone(phone, data)
        assert ok, err
        referrer_id, err = auth_routes.resolve_referrer_id_for_signup(phone, data)
        assert not err, err
        return auth_routes.get_or_create_user(phone, ip_address=ip, referrer_id=referrer_id)


# =========================================================================
# 1. Self-referral (own code on own second account) blocked
# =========================================================================
class TestSelfReferral:
    def test_own_code_on_the_same_phone_is_dropped_and_logged(self, env, caplog):
        store, app = env
        store.add_user("9000000001", "AAAA1111", ip="203.0.113.7")
        caplog.set_level(logging.INFO)
        with app.test_request_context("/"):
            ok, err = auth_routes.persist_referral_for_phone("9000000001", {"ref": "AAAA1111"})
            referrer_id, _ = auth_routes.resolve_referrer_id_for_signup("9000000001", {"ref": "AAAA1111"})
        assert (ok, err) == (True, None)  # never blocks login/OTP
        assert referrer_id is None
        assert store.pending == {}
        lines = policy_lines(caplog, "BLOCK")
        assert lines and all("reason=self_referral_same_phone" in m for m in lines)
        assert all("9000000001" not in m and "0001" in m for m in lines)  # phone is masked

    def test_commission_layer_blocks_and_logs_self_referral(self, caplog):
        """Last line of defence in payout code (unchanged), even if a bad referred_by got stored."""
        from services.referral_commission import process_referral_commission

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = SimpleNamespace(
            _mapping={"referred_by": 5, "first_sub_commission_paid": 0}
        )
        caplog.set_level(logging.INFO)
        result = process_referral_commission(5, 999.0, razorpay_payment_id="pay_test_self", conn=conn)
        assert result["reason"] == "self_referral"
        assert result["created"] == []
        assert any("Self-referral blocked" in r.getMessage() for r in caplog.records)

    def test_own_code_on_a_second_account_from_the_same_ip_keeps_the_referral_and_flags_both(self, env, caplog):
        store, app = env
        first = store.add_user("9000000001", "AAAA1111", ip="203.0.113.7")
        caplog.set_level(logging.INFO)
        user, status = signup(app, "9000000002", "203.0.113.7", ref="AAAA1111")
        assert status == "new"
        assert user["referred_by"] == first["id"]  # decision: keep the referral, flag for review
        assert set(store.flagged()) == {"9000000001", "9000000002"}
        assert "referrer_shares_ip" in store.users[user["id"]]["flag_reason"]
        assert "referred_user_shares_ip" in store.users[first["id"]]["flag_reason"]
        assert len(policy_lines(caplog, "FLAG")) == 2

    def test_a_referral_between_different_ips_is_not_flagged(self, env):
        store, app = env
        first = store.add_user("9000000001", "AAAA1111", ip="203.0.113.7")
        user, _ = signup(app, "9000000002", "198.51.100.20", ref="AAAA1111")
        assert user["referred_by"] == first["id"]
        assert store.flagged() == {}


# =========================================================================
# 2. Several signups from one device or IP flagged
# =========================================================================
class TestSignupsFromOneIpOrDevice:
    def test_the_sixth_account_from_one_ip_is_flagged_not_blocked(self, env, caplog):
        store, app = env
        caplog.set_level(logging.INFO)
        statuses = [signup(app, _apart(n), "203.0.113.50")[1] for n in range(6)]
        assert statuses == ["new"] * 6  # nobody is blocked
        assert set(store.flagged()) == {_apart(5)}
        assert "ip_accounts: 6 accounts from ip 203.0.113.50" in store.by_phone[_apart(5)]["flag_reason"]
        assert len(policy_lines(caplog, "FLAG")) == 1

    def test_every_account_after_the_fifth_is_flagged(self, env):
        store, app = env
        for n in range(20):
            signup(app, _apart(n), "203.0.113.50")
        assert len(store.users) == 20
        assert len(store.flagged()) == 15

    def test_accounts_on_different_ips_are_not_flagged(self, env):
        store, app = env
        for n in range(20):
            signup(app, f"97{n:02d}000000", f"203.0.113.{n + 1}")
        assert store.flagged() == {}

    @pytest.mark.xfail(strict=True, reason="audit note 4.2: signup never records a device id (needs a front-end "
                                           "change), so a shared device cannot be detected yet")
    def test_signup_records_a_device_id(self):
        assert "device_id" in inspect.signature(auth_routes.get_or_create_user).parameters


# =========================================================================
# 3. Sequential or look-alike phone number signups flagged
# =========================================================================
class TestSequentialPhoneNumbers:
    def test_third_number_sharing_its_first_eight_digits_is_flagged(self, env, caplog):
        store, app = env
        caplog.set_level(logging.INFO)
        # consecutive AND look-alike numbers, each from a different IP: only the numbers link them
        for n, phone in enumerate(["9876543210", "9876543211", "9876543225", "9876543299"]):
            signup(app, phone, f"203.0.113.{100 + n}")
        assert set(store.flagged()) == {"9876543225", "9876543299"}
        assert "phone_lookalike: 3 accounts starting 98765432" in store.by_phone["9876543225"]["flag_reason"]
        assert len(policy_lines(caplog, "FLAG")) == 2

    def test_numbers_with_different_prefixes_are_not_flagged(self, env):
        store, app = env
        for n in range(10):
            signup(app, f"9{n}76543210", f"203.0.113.{100 + n}")
        assert store.flagged() == {}


# =========================================================================
# Policy behaviours the boxes rely on
# =========================================================================
class TestPolicyBehaviour:
    def test_a_failing_rule_never_breaks_signup(self, caplog):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("database went away")
        caplog.set_level(logging.INFO)
        assert fraud_policy.evaluate_signup(conn, 1, "9876543210", "203.0.113.9") == []
        conn.rollback.assert_called()
        assert any("evaluate_signup failed" in r.getMessage() for r in caplog.records)

    def test_flag_lines_and_reasons_never_contain_a_full_phone_number(self, env, caplog):
        store, app = env
        caplog.set_level(logging.INFO)
        for n, phone in enumerate(["9876543210", "9876543211", "9876543212"]):
            signup(app, phone, f"203.0.113.{100 + n}")
        assert not [m for m in policy_lines(caplog, "FLAG") if re.search(r"\d{10}", m)]


# =========================================================================
# 4. OTP flooding blocked per number and per IP
# =========================================================================
# Runs the real app in a child process (the rate limits bind at import time and
# only exist when app.py was imported first), with the database and the SMS
# provider stubbed, so nothing leaves the machine.
_FLOOD_PROBE = r"""
import json, logging, os, sys
sys.path.insert(0, sys.argv[1])
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
os.environ.update({
    "SECRET_KEY": "probe", "JWT_SECRET_KEY": "probe-jwt-secret-key-32-bytes-long!",
    "DATABASE_URL": "postgresql://probe:probe@127.0.0.1:1/probe",   # closed local port
    "REDIS_URL": "", "DEBUG_SMS": "false",
})
os.environ.pop("RENDER", None)

import app as m
import routes.auth_routes as ar

events = []
class Collect(logging.Handler):
    def emit(self, record):
        events.append([record.name, record.levelname, record.getMessage()])
logging.getLogger().addHandler(Collect())

ar.get_verification = lambda phone: None
ar.store_verification = lambda *a, **k: None
ar.delete_verification = lambda *a, **k: None
ar.persist_referral_for_phone = lambda *a, **k: (True, None)
class Sms:
    def send_otp(self, full_phone):
        return True, {"responseCode": 200}, "vid"
ar.get_sms_service = lambda: Sms()

c = m.app.test_client()
def post(path, phone, ip, **extra):
    body = {"phone": phone, **extra}
    return c.post(path, json=body, environ_base={"REMOTE_ADDR": ip}).status_code

out = {}
out["send_per_ip"] = [post("/api/auth/send-otp", "90000011%02d" % i, "10.1.0.1") for i in range(7)]
out["send_per_number"] = [post("/api/auth/send-otp", "9000002001", "10.2.0.%d" % (i + 1)) for i in range(7)]
out["verify_per_ip"] = [post("/api/auth/verify-otp", "90000031%02d" % i, "10.3.0.1", otp="123456") for i in range(12)]
out["verify_per_number"] = [post("/api/auth/verify-otp", "9000004001", "10.4.0.%d" % (i + 1), otp="123456") for i in range(12)]
out["events"] = events
print("PROBE" + json.dumps(out))
"""


@pytest.fixture(scope="module")
def flood():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_URL", "REDIS_URL", "TEST_DATABASE_URL"))}
    proc = subprocess.run(
        [sys.executable, "-c", _FLOOD_PROBE, str(ROOT)],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(ROOT),
    )
    line = next((l for l in proc.stdout.splitlines() if l.startswith("PROBE")), None)
    assert line, f"flood probe failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    return json.loads(line[len("PROBE"):])


class TestOtpFlooding:
    def test_send_otp_is_blocked_per_ip(self, flood):
        assert flood["send_per_ip"] == [200] * 5 + [429] * 2  # 5 per minute per IP

    def test_send_otp_is_blocked_per_number_across_many_ips(self, flood):
        assert flood["send_per_number"] == [200] * 5 + [429] * 2  # 5 per hour per number

    def test_verify_otp_is_blocked_per_ip(self, flood):
        assert flood["verify_per_ip"] == [401] * 10 + [429] * 2  # 10 per minute per IP

    def test_verify_otp_is_blocked_per_number_across_many_ips(self, flood):
        assert flood["verify_per_number"] == [401] * 10 + [429] * 2  # 10 per minute per number


# =========================================================================
# 5. Each block or flag writes a reason to the log
# =========================================================================
class TestBlocksAreLogged:
    def test_rate_limit_blocks_log_limit_key_and_endpoint(self, flood):
        lines = [msg for name, level, msg in flood["events"] if name == "flask-limiter" and "exceeded" in msg]
        assert any("5 per 1 minute" in m and "auth.send_otp" in m for m in lines), lines
        assert any("10 per 1 minute" in m and "auth.verify_otp" in m for m in lines), lines

    def test_commission_self_referral_block_is_logged(self, caplog):
        TestSelfReferral().test_commission_layer_blocks_and_logs_self_referral(caplog)

    def test_flags_are_logged_with_their_reason(self, env, caplog):
        store, app = env
        caplog.set_level(logging.INFO)
        for n in range(6):
            signup(app, _apart(n), "203.0.113.50")
        lines = policy_lines(caplog, "FLAG")
        assert len(lines) == 1 and "reason=ip_accounts" in lines[0] and "user_id=6" in lines[0]

    @pytest.mark.parametrize("path", ["/api/auth/send-otp", "/api/auth/resend-otp"])
    def test_otp_resend_cooldown_refusal_is_logged_at_info_with_a_masked_phone(self, path, caplog):
        app = Flask(__name__)
        app.config.update(SECRET_KEY="test-secret", JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long")
        JWTManager(app)
        app.register_blueprint(auth_routes.auth_bp, url_prefix="/api/auth")
        if auth_routes.limiter is not None:
            auth_routes.limiter.reset()
        just_sent = {"verification_id": "v1", "attempts": 0, "seconds_since_created": 5}
        caplog.set_level(logging.INFO)
        with patch.object(auth_routes, "get_verification", return_value=just_sent), \
             patch.object(auth_routes, "persist_referral_for_phone", return_value=(True, None)), \
             patch.object(auth_routes, "_resend_cooldown_seconds", return_value=60), \
             patch.object(auth_routes, "_max_otp_attempts", return_value=3):
            res = app.test_client().post(
                path, json={"phone": "9876543210"}, environ_base={"REMOTE_ADDR": "203.0.113.78"},
            )
        assert res.status_code == 429
        lines = [
            r for r in caplog.records
            if r.name == "services.fraud_policy" and r.getMessage().startswith("fraud_policy THROTTLE")
        ]
        assert len(lines) == 1 and lines[0].levelno == logging.INFO
        assert "reason=otp_resend_cooldown" in lines[0].getMessage()
        assert "9876543210" not in lines[0].getMessage() and "phone=******3210" in lines[0].getMessage()

    def test_otp_lockout_is_logged_with_a_masked_phone(self, caplog):
        app = Flask(__name__)
        app.config.update(SECRET_KEY="test-secret", JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long")
        JWTManager(app)
        app.register_blueprint(auth_routes.auth_bp, url_prefix="/api/auth")
        if auth_routes.limiter is not None:
            auth_routes.limiter.reset()
        sms = MagicMock()
        sms.verify_otp.return_value = (False, {"message": "WRONG_OTP"})
        stored = {"verification_id": "v1", "attempts": 2, "seconds_since_created": 5}
        caplog.set_level(logging.INFO)
        with patch.object(auth_routes, "get_verification", return_value=stored), \
             patch.object(auth_routes, "reserve_attempt", return_value={"verification_id": "v1", "attempts": 3}), \
             patch.object(auth_routes, "_max_otp_attempts", return_value=3), \
             patch.object(auth_routes, "get_sms_service", return_value=sms):
            res = app.test_client().post(
                "/api/auth/verify-otp", json={"phone": "9876543210", "otp": "123456"},
                environ_base={"REMOTE_ADDR": "203.0.113.77"},
            )
        assert res.status_code == 429 and res.get_json()["reason"] == "OTP_LOCKED"
        lines = policy_lines(caplog, "BLOCK")
        assert len(lines) == 1 and "reason=otp_locked" in lines[0]
        assert "9876543210" not in lines[0] and "phone=******3210" in lines[0]


# =========================================================================
# 6. Fraud rules live in the policy layer only (grep agents for duplicated checks)
# =========================================================================
# The comparisons that ARE the rules (not comments or docstrings about them).
_RULE_MARKERS = (
    re.compile(r"referrer_phone\s*==\s*phone"),
    re.compile(r"referrer_id\s*==\s*referred_user_id"),
    re.compile(r"bound_referrer\s*==\s*user_id"),
    re.compile(r"ip_address\s*=\s*:ip"),
)
# Payout code is off limits for this audit; its last-line self-referral guard stays.
_PAYOUT_CODE = {"services/referral_commission.py"}


class TestRulesLiveInOnePlace:
    def test_no_agent_contains_a_fraud_rule(self):
        offenders = []
        for path in sorted((ROOT / "agents").glob("*.py")):
            src = path.read_text(encoding="utf-8-sig")
            for pattern in (*_RULE_MARKERS, re.compile(r"self[- ]referral", re.I), re.compile(r"ip_address|device_id|referred_by")):
                if pattern.search(src):
                    offenders.append((path.name, pattern.pattern))
        assert offenders == []

    def test_fraud_agent_is_a_status_stub(self):
        from agents.fraud_agent import FraudAgent

        public = [n for n, v in vars(FraudAgent).items() if callable(v) and not n.startswith("_")]
        assert public == ["get_status"]

    def test_orchestration_fraud_workflow_is_disabled(self):
        src = (ROOT / "routes" / "orchestration_routes.py").read_text(encoding="utf-8-sig")
        handler = src.split("def execute_fraud_check")[1].split("@orchestration_bp")[0]
        assert "410" in handler

    def test_the_old_ip_fraud_decorator_is_gone(self):
        assert not (ROOT / "middleware" / "fraud_protection.py").exists()

    def test_abuse_rules_are_defined_only_in_the_policy_module(self):
        modules = {
            p.relative_to(ROOT).as_posix()
            for folder in ("routes", "services", "middleware")
            for p in (ROOT / folder).glob("*.py")
            if any(pat.search(p.read_text(encoding="utf-8-sig")) for pat in _RULE_MARKERS)
        }
        assert modules - _PAYOUT_CODE == {"services/fraud_policy.py"}, sorted(modules)


# =========================================================================
# 7. Admin can see flagged accounts
# =========================================================================
def _admin_list_sql(**kwargs):
    seen = []
    conn = MagicMock()
    conn.__enter__.return_value = conn

    def execute(query, params=None):
        seen.append(str(query))
        res = MagicMock()
        res.scalar.return_value = 0
        res.fetchall.return_value = []
        return res

    conn.execute.side_effect = execute
    with patch.object(admin_service, "get_db_connection", return_value=conn):
        admin_service.get_admin_users(**kwargs)
    return " ".join(" ".join(seen).split()).lower()


class TestAdminSeesFlaggedAccounts:
    def test_admin_can_list_banned_accounts(self):
        assert "is_blocked = 1" in _admin_list_sql(status_filter="banned")

    def test_admin_can_filter_the_user_list_to_flagged_accounts(self):
        assert "u.is_flagged = 1" in _admin_list_sql(status_filter="flagged")

    def test_admin_user_rows_include_the_flag_and_its_reason(self):
        assert "u.is_flagged, u.flag_reason" in _admin_list_sql()

    def test_admin_users_page_has_a_flagged_filter_and_shows_the_reason(self):
        html = (ROOT / "templates" / "admin" / "admin_users.html").read_text(encoding="utf-8-sig")
        assert 'value="flagged"' in html
        assert 'params.append("status", status)' in html
        assert "user.flag_reason" in html

    def test_admin_users_api_passes_the_status_filter_through(self):
        src = (ROOT / "routes" / "admin_routes.py").read_text(encoding="utf-8-sig")
        assert "status = request.args.get('status', '')" in src
        assert "get_admin_users(page, limit, search, role, status)" in src

    def test_schema_adds_the_flag_columns_for_new_and_existing_databases(self):
        seen = []

        class Rec:
            def execute(self, stmt, params=None):
                seen.append(" ".join(str(getattr(stmt, "text", stmt)).lower().split()))
                res = MagicMock()
                res.fetchone.return_value = None
                res.fetchall.return_value = []
                res.scalar.return_value = 0
                return res

            def commit(self):
                pass

            def rollback(self):
                pass

        try:
            dbmod._init_db_body(Rec())
        except Exception:
            pass  # only the statements before the failure matter; the flag columns come early
        joined = " ".join(seen)
        for column in ("is_flagged integer default 0", "flag_reason text", "flagged_at timestamp"):
            assert f"alter table users add column if not exists {column}" in joined, column


# =========================================================================
# Real PostgreSQL: the policy SQL and the admin filter, end to end
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
def pg(pg_engine, monkeypatch):
    monkeypatch.setattr(auth_routes, "engine", pg_engine)
    monkeypatch.setattr(admin_service, "get_db_connection", pg_engine.connect)
    with pg_engine.connect() as conn:
        conn.execute(text("TRUNCATE users, pending_referrals RESTART IDENTITY CASCADE"))
        conn.commit()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    return pg_engine, app


def _flags(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT phone, flag_reason, flagged_at FROM users WHERE is_flagged = 1")).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


@needs_db
class TestPolicyOnPostgres:
    def test_sixth_account_from_one_ip_is_flagged_and_all_are_created(self, pg):
        engine, app = pg
        statuses = [signup(app, _apart(n), "203.0.113.50")[1] for n in range(6)]
        assert statuses == ["new"] * 6
        flags = _flags(engine)
        assert set(flags) == {_apart(5)}
        reason, flagged_at = flags[_apart(5)]
        assert reason.startswith("ip_accounts: 6 accounts from ip 203.0.113.50")
        assert flagged_at is not None

    def test_third_lookalike_number_within_a_day_is_flagged(self, pg):
        engine, app = pg
        for n, phone in enumerate(["9876543210", "9876543211", "9876543225", "9876543299"]):
            signup(app, phone, f"203.0.113.{100 + n}")
        assert set(_flags(engine)) == {"9876543225", "9876543299"}

    def test_lookalike_numbers_created_more_than_a_day_ago_do_not_count(self, pg):
        engine, app = pg
        for n, phone in enumerate(["9876543210", "9876543211"]):
            signup(app, phone, f"203.0.113.{100 + n}")
        with engine.connect() as conn:
            conn.execute(text("UPDATE users SET created_at = NOW() - INTERVAL '2 days'"))
            conn.commit()
        signup(app, "9876543225", "203.0.113.150")
        assert _flags(engine) == {}

    def test_own_code_on_a_second_account_from_the_same_ip_flags_both_and_keeps_the_referral(self, pg):
        engine, app = pg
        first, _ = signup(app, "9000000001", "203.0.113.7")
        second, _ = signup(app, "9000000002", "203.0.113.7", ref=first["referral_code"])
        assert second["referred_by"] == first["id"]
        flags = _flags(engine)
        assert set(flags) == {"9000000001", "9000000002"}
        assert "referrer_shares_ip" in flags["9000000002"][0]
        assert "referred_user_shares_ip" in flags["9000000001"][0]

    def test_repeat_flags_append_reasons_and_keep_the_first_flagged_time(self, pg):
        engine, app = pg
        user, _ = signup(app, "9000000001", "203.0.113.7")
        with engine.connect() as conn:
            fraud_policy.flag_user(conn, user["id"], "first reason")
            conn.commit()
            first_time = conn.execute(text("SELECT flagged_at FROM users WHERE id = :i"), {"i": user["id"]}).scalar()
            fraud_policy.flag_user(conn, user["id"], "second reason")
            conn.commit()
        reason, flagged_at = _flags(engine)["9000000001"]
        assert reason == "first reason; second reason"
        assert flagged_at == first_time

    def test_flag_reason_is_capped(self, pg):
        engine, app = pg
        user, _ = signup(app, "9000000001", "203.0.113.7")
        with engine.connect() as conn:
            for n in range(60):
                fraud_policy.flag_user(conn, user["id"], f"reason {n} " + "x" * 50)
            conn.commit()
        assert len(_flags(engine)["9000000001"][0]) == fraud_policy.FLAG_REASON_MAX_CHARS

    def test_admin_list_filters_to_flagged_accounts_and_shows_the_reason(self, pg):
        engine, app = pg
        for n in range(6):
            signup(app, _apart(n), "203.0.113.50")
        flagged = admin_service.get_admin_users(status_filter="flagged")
        assert flagged["total"] == 1
        row = flagged["users"][0]
        assert row["phone"] == _apart(5) and row["is_flagged"] is True
        assert row["flag_reason"].startswith("ip_accounts")
        everyone = admin_service.get_admin_users()
        assert everyone["total"] == 6
        assert sum(1 for u in everyone["users"] if u["is_flagged"]) == 1
