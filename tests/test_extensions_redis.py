"""Redis is optional: limiter/JWT blocklist must boot without a reachable Redis,
and the rate limiter must keep limiting (in memory) when Redis goes away."""
import json
import logging
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask


class LimiterStorageUriTests(unittest.TestCase):
    def test_missing_redis_url_uses_memory(self):
        from extensions import limiter_storage_uri
        env = {k: v for k, v in os.environ.items() if k != "REDIS_URL"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(limiter_storage_uri(), "memory://")

    def test_redis_url_is_always_used_without_a_boot_time_probe(self):
        """Down-at-boot and down-later are both left to the in-memory fallback,
        so an unreachable Redis at boot must NOT pin the process to memory."""
        from extensions import limiter_storage_uri
        url = "redis://no-such-host.invalid:6379/0"
        with patch.dict(os.environ, {"REDIS_URL": url}, clear=False):
            with patch("redis.from_url") as from_url:
                self.assertEqual(limiter_storage_uri(), url)
        from_url.assert_not_called()


class InitExtensionsBootTests(unittest.TestCase):
    def test_init_extensions_survives_unreachable_redis(self):
        from extensions import init_extensions
        app = Flask(__name__)
        with patch.dict(os.environ, {"REDIS_URL": "redis://no-such-host.invalid:6379/0"}, clear=False):
            limiter, _razor = init_extensions(app)
        self.assertIsNotNone(limiter)
        self.assertTrue(limiter._in_memory_fallback_enabled)
        # swallow_errors would silently skip limits on a storage error.
        self.assertFalse(limiter._swallow_errors)

    def test_redis_storage_gets_bounded_socket_timeouts(self):
        from extensions import init_extensions
        app = Flask(__name__)
        with patch.dict(os.environ, {"REDIS_URL": "redis://no-such-host.invalid:6379/0"}, clear=False):
            limiter, _razor = init_extensions(app)
        pool_kwargs = limiter.storage.storage.connection_pool.connection_kwargs
        self.assertEqual(pool_kwargs.get("socket_connect_timeout"), 2)
        self.assertEqual(pool_kwargs.get("socket_timeout"), 2)

    def test_storage_recovery_is_logged_as_warning(self):
        from extensions import init_extensions
        init_extensions(Flask(__name__))
        # Flask-Limiter emits this at INFO; app.py's root logger is at INFO.
        with self.assertLogs("flask-limiter", level="INFO") as logs:
            logging.getLogger("flask-limiter").info("Rate limit storage recovered")
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertEqual(logs.records[0].levelno, logging.WARNING)


# Runs the real app in a clean process whose limiter storage is a Redis
# stand-in that can be switched off and on at runtime.
_OUTAGE_PROBE = textwrap.dedent(r"""
    import json, logging, os, sys, time
    sys.path.insert(0, sys.argv[1])
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    os.environ.update({
        "SECRET_KEY": "probe", "JWT_SECRET_KEY": "probe-jwt-secret-key-32-bytes-long!",
        # closed local port: nothing remote is ever contacted
        "DATABASE_URL": "postgresql://probe:probe@127.0.0.1:1/probe",
        "REDIS_URL": "", "DEBUG_SMS": "false",
    })
    os.environ.pop("RENDER", None)

    import redis
    from limits.storage import MemoryStorage

    class FlakyRedis(MemoryStorage):
        STORAGE_SCHEME = ["flakyredis"]
        down = False

        def _guard(self):
            if FlakyRedis.down:
                raise redis.ConnectionError("simulated Redis outage")

        def incr(self, *a, **k):
            self._guard(); return super().incr(*a, **k)

        def get(self, *a, **k):
            self._guard(); return super().get(*a, **k)

        def get_expiry(self, *a, **k):
            self._guard(); return super().get_expiry(*a, **k)

        def check(self):
            return not FlakyRedis.down

    import extensions
    extensions.limiter_storage_uri = lambda: "flakyredis://"

    events = []
    class Collect(logging.Handler):
        def emit(self, record):
            events.append([record.levelname, record.getMessage()])
    lim_log = logging.getLogger("flask-limiter")
    lim_log.addHandler(Collect())
    lim_log.setLevel(logging.INFO)

    import app as m
    import routes.auth_routes as ar

    # Keep send-otp off the database and the SMS provider.
    ar.get_verification = lambda phone: None
    ar.store_verification = lambda *a, **k: None
    ar.delete_verification = lambda *a, **k: None
    ar.persist_referral_for_phone = lambda *a, **k: (True, None)
    ar._resend_cooldown_seconds = lambda: 60
    class Sms:
        def send_otp(self, full_phone):
            return True, {"responseCode": 200}, "vid"
    ar.get_sms_service = lambda: Sms()

    c = m.app.test_client()
    def send(ip):
        # A fresh IP per call: only the per-number limit can refuse it.
        return c.post("/api/auth/send-otp", json={"phone": "9876543210"},
                      environ_base={"REMOTE_ADDR": ip}).status_code
    def refresh(ip):
        return c.post("/api/refresh", headers={"Accept": "application/json"},
                      environ_base={"REMOTE_ADDR": ip}).status_code

    out = {"before": send("10.0.0.1")}
    FlakyRedis.down = True
    out["send_during"] = [send("10.0.1.%d" % i) for i in range(1, 7)]
    out["refresh_during"] = [refresh("10.0.2.1") for _ in range(31)]

    FlakyRedis.down = False
    deadline = time.time() + 60
    while time.time() < deadline and not any("recovered" in e[1] for e in events):
        refresh("10.0.3.1")
        time.sleep(0.25)
    # Back on the primary storage, which still holds the 1 pre-outage hit.
    out["send_after"] = send("10.0.4.1")
    out["events"] = events
    print("PROBE" + json.dumps(out))
""")


class RedisOutageAtRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_URL", "REDIS_URL"))}
        proc = subprocess.run(
            [sys.executable, "-c", _OUTAGE_PROBE, str(ROOT)],
            capture_output=True, text=True, timeout=180, env=env, cwd=str(ROOT),
        )
        line = next((l for l in proc.stdout.splitlines() if l.startswith("PROBE")), None)
        assert line, f"outage probe failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
        cls.out = json.loads(line[len("PROBE"):])

    def test_otp_send_keeps_working_and_limited_in_memory(self):
        self.assertEqual(self.out["before"], 200)
        # Memory starts empty, so the per-number 5/hour limit counts afresh.
        self.assertEqual(self.out["send_during"], [200] * 5 + [429])

    def test_refresh_keeps_responding_and_limited_in_memory(self):
        codes = self.out["refresh_during"]
        self.assertNotIn(500, codes)
        self.assertNotIn(429, codes[:30])
        self.assertEqual(codes[30], 429)  # "30 per minute" still enforced

    def test_fallback_and_recovery_are_logged_as_warnings(self):
        warnings = [msg for level, msg in self.out["events"] if level == "WARNING"]
        self.assertTrue(any("falling back to in-memory" in m for m in warnings), self.out["events"])
        self.assertTrue(any("recovered" in m for m in warnings), self.out["events"])

    def test_limits_return_to_redis_after_recovery(self):
        self.assertEqual(self.out["send_after"], 200)


if __name__ == "__main__":
    unittest.main()
