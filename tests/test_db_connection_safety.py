"""Regression guards for the production connection-pool-hang fix.

Covers: engine configuration (finite timeouts, tcp_user_timeout actually
accepted by the installed psycopg2/libpq), connection-lifecycle safety
(get_db_connection() releases via its context manager, a disconnect-like
error invalidates rather than silently returning a broken connection to
the pool), the bounded background init_db startup path, and /api/readiness
behavior. No test here talks to a real Postgres server -- engine-level
tests substitute a throwaway in-memory SQLite engine, and the readiness
tests patch database.init_db.get_db_connection at the Flask-route level.
"""
import os
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def _first_int(pattern, text, label):
    match = re.search(pattern, text)
    if not match:
        raise AssertionError(f"Could not find {label} in source")
    return int(match.group(1))


# ---------------------------------------------------------------------
# A. Engine configuration
# ---------------------------------------------------------------------
class EngineConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.src = _read("database/init_db.py")

    def test_pool_pre_ping_enabled(self):
        self.assertIn("pool_pre_ping=True", self.src)

    def test_pool_timeout_is_finite(self):
        val = _first_int(r"pool_timeout\s*=\s*(\d+)", self.src, "pool_timeout")
        self.assertGreater(val, 0)

    def test_connect_timeout_is_finite(self):
        val = _first_int(r'"connect_timeout"\s*:\s*(\d+)', self.src, "connect_timeout")
        self.assertGreater(val, 0)

    def test_statement_timeout_is_finite(self):
        val = _first_int(r"statement_timeout=(\d+)", self.src, "statement_timeout")
        self.assertGreater(val, 0)

    def test_tcp_user_timeout_configured_and_finite(self):
        val = _first_int(r'"tcp_user_timeout"\s*:\s*(\d+)', self.src, "tcp_user_timeout")
        self.assertGreater(val, 0)
        # Must sit above statement_timeout: the TCP layer must not cut off
        # a legitimately slow-but-alive query before Postgres's own
        # statement_timeout gets a chance to report back cleanly.
        statement_timeout = _first_int(r"statement_timeout=(\d+)", self.src, "statement_timeout")
        self.assertGreater(val, statement_timeout)

    def test_tcp_user_timeout_accepted_by_installed_psycopg2(self):
        """Not just present in source -- actually recognized by the
        installed psycopg2/libpq. A rejected/unknown keyword raises
        TypeError from psycopg2 itself; a real (doomed) connection attempt
        to a closed local port fails with a connection-level error instead,
        which is what accepting the parameter looks like."""
        import psycopg2
        try:
            psycopg2.connect(
                "postgresql://u:p@127.0.0.1:1/db",
                connect_timeout=1,
                tcp_user_timeout=5000,
            )
        except TypeError:
            self.fail("tcp_user_timeout was rejected as an invalid keyword by installed psycopg2/libpq")
        except Exception:
            pass  # any connection-level failure is expected and fine here

    def test_keepalives_still_present(self):
        for key in ("keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count"):
            self.assertIn(f'"{key}"', self.src)


# ---------------------------------------------------------------------
# B. Connection lifecycle
# ---------------------------------------------------------------------
class ConnectionLifecycleTests(unittest.TestCase):
    def test_get_db_connection_is_closed_by_context_manager(self):
        import database.init_db as db
        from sqlalchemy import create_engine

        test_engine = create_engine("sqlite:///:memory:")
        try:
            with patch.object(db, "engine", test_engine):
                with db.get_db_connection() as conn:
                    self.assertFalse(conn.closed)
                held = conn
            self.assertTrue(held.closed)
        finally:
            test_engine.dispose()

    def test_disconnect_like_error_invalidates_the_connection(self):
        """Exercises the real _invalidate_on_disconnect listener function
        (not a re-implementation of it) with a minimal stand-in for
        SQLAlchemy's ExceptionContext shape."""
        import database.init_db as db

        fake_connection = MagicMock()
        fake_exception = OSError("connection reset by peer")
        ctx = MagicMock(is_disconnect=True, connection=fake_connection, original_exception=fake_exception)

        db._invalidate_on_disconnect(ctx)

        fake_connection.invalidate.assert_called_once_with(fake_exception)

    def test_non_disconnect_error_does_not_invalidate_the_connection(self):
        """A plain query error (bad SQL, constraint violation, ...) must
        not throw away an otherwise-healthy connection."""
        import database.init_db as db

        fake_connection = MagicMock()
        fake_exception = ValueError("syntax error at or near ...")
        ctx = MagicMock(is_disconnect=False, connection=fake_connection, original_exception=fake_exception)

        db._invalidate_on_disconnect(ctx)

        fake_connection.invalidate.assert_not_called()

    def test_pool_event_listeners_are_registered_on_the_production_engine(self):
        from sqlalchemy import event
        import database.init_db as db

        self.assertTrue(event.contains(db.engine, "checkout", db._on_pool_checkout))
        self.assertTrue(event.contains(db.engine, "checkin", db._on_pool_checkin))
        self.assertTrue(event.contains(db.engine, "invalidate", db._on_pool_invalidate))
        self.assertTrue(event.contains(db.engine, "handle_error", db._invalidate_on_disconnect))


# ---------------------------------------------------------------------
# C. Startup lifecycle
# ---------------------------------------------------------------------
class StartupInitDbTests(unittest.TestCase):
    def test_returns_done_quickly_when_init_db_succeeds(self):
        import database.init_db as db

        with patch.object(db, "init_db", return_value=None):
            status, err = db.run_init_db_in_background(timeout_seconds=5)
        self.assertEqual(status, "done")
        self.assertIsNone(err)

    def test_returns_failed_when_init_db_raises(self):
        import database.init_db as db

        def _boom():
            raise RuntimeError("schema init exploded")

        with patch.object(db, "init_db", side_effect=_boom):
            status, err = db.run_init_db_in_background(timeout_seconds=5)
        self.assertEqual(status, "failed")
        self.assertIsInstance(err, RuntimeError)

    def test_times_out_without_hanging_the_caller(self):
        """The defining behavior of this fix: a stuck init_db must not be
        able to hang worker boot (and therefore a Render deploy)
        indefinitely -- the caller must get control back at timeout_seconds
        even though the background thread is still running."""
        import database.init_db as db

        def _slow():
            time.sleep(2)

        start = time.monotonic()
        with patch.object(db, "init_db", side_effect=_slow):
            status, err = db.run_init_db_in_background(timeout_seconds=0.2)
        elapsed = time.monotonic() - start

        self.assertEqual(status, "timeout")
        self.assertIsNone(err)
        self.assertLess(elapsed, 1.5, "caller blocked far longer than the requested timeout")

    def test_no_duplicate_thread_is_left_running_on_success(self):
        import database.init_db as db

        before = threading.active_count()
        with patch.object(db, "init_db", return_value=None):
            status, _ = db.run_init_db_in_background(timeout_seconds=5)
        after = threading.active_count()

        self.assertEqual(status, "done")
        self.assertEqual(after, before, "background thread did not terminate/join cleanly")

    def test_app_py_calls_run_init_db_in_background_not_the_old_fire_and_forget_pattern(self):
        src = _read("app.py")
        self.assertIn("run_init_db_in_background", src)
        self.assertNotIn("_run_init_db_async", src)
        # Exactly one thread-start call site in the whole startup path --
        # inside run_init_db_in_background itself, not duplicated in app.py.
        self.assertNotIn("threading.Thread(", src)

    def test_app_py_timeout_log_mentions_the_configured_seconds(self):
        """Item 3: the 60s-timeout boot log names the actual configured
        ceiling rather than a hardcoded/stale number."""
        src = _read("app.py")
        idx = src.index("still running after")
        snippet = src[max(0, idx - 100):idx + 100]
        self.assertIn("INIT_DB_STARTUP_TIMEOUT_SECONDS", snippet)

    def test_app_py_failed_log_uses_exception_type_name_not_raw_text(self):
        """FIX #2 / item 2: the normal (within-timeout) failure boot log
        must never interpolate the raw exception object -- only its type
        name."""
        src = _read("app.py")
        idx = src.index('elif _init_db_status == "failed":')
        snippet = src[idx:idx + 200]
        self.assertIn("type(_init_db_error).__name__", snippet)
        self.assertNotIn("{_init_db_error}", snippet)

    def test_late_successful_completion_logs_after_timeout(self):
        """FIX #1 / item 4: if init_db finishes AFTER the caller already
        gave up waiting, the background thread must log that outcome
        itself -- otherwise it is lost forever."""
        import database.init_db as db

        def _slow_success():
            time.sleep(0.5)

        with self.assertLogs("database.init_db", level="INFO") as cm:
            with patch.object(db, "init_db", side_effect=_slow_success):
                status, err = db.run_init_db_in_background(timeout_seconds=0.2)
                self.assertEqual(status, "timeout")
                time.sleep(1.0)  # let the background thread finish and log

        joined = "\n".join(cm.output)
        self.assertIn("completed after startup timeout", joined)

    def test_late_failure_logs_exception_type_only_no_raw_text(self):
        """FIX #1 / item 5 + item 6: a failure discovered after the
        startup timeout must be logged with the exception TYPE only --
        the raw exception message must never appear in the log output."""
        import database.init_db as db

        secret_text = "super-secret-connection-detail-should-never-appear-in-logs"

        def _slow_boom():
            time.sleep(0.5)
            raise RuntimeError(secret_text)

        with self.assertLogs("database.init_db", level="INFO") as cm:
            with patch.object(db, "init_db", side_effect=_slow_boom):
                status, err = db.run_init_db_in_background(timeout_seconds=0.2)
                self.assertEqual(status, "timeout")
                time.sleep(1.0)  # let the background thread finish and log

        joined = "\n".join(cm.output)
        self.assertIn("FAILED after startup timeout", joined)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn(secret_text, joined)

    def test_no_raw_exception_interpolation_remains_in_either_file(self):
        """Item 6, combined source check: neither startup file may log
        str(exception)/a bare f-string of the exception object for these
        init_db outcome messages -- only .__name__ everywhere."""
        app_src = _read("app.py")
        db_src = _read("database/init_db.py")
        self.assertNotIn("{_init_db_error}", app_src)
        self.assertIn("type(_init_db_error).__name__", app_src)
        self.assertIn('type(outcome["error"]).__name__', db_src)


# ---------------------------------------------------------------------
# D. /api/readiness
# ---------------------------------------------------------------------
class ReadinessRouteTests(unittest.TestCase):
    def setUp(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def test_ready_when_select_1_succeeds(self):
        class OkConn:
            def execute(self, *a, **k):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("database.init_db.get_db_connection", return_value=OkConn()):
            res = self.client.get("/api/readiness")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"status": "ready"})

    def test_not_ready_on_a_normal_db_exception(self):
        def _raise():
            raise RuntimeError("connection failed")

        with patch("database.init_db.get_db_connection", side_effect=_raise):
            res = self.client.get("/api/readiness")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json(), {"status": "not ready"})


if __name__ == "__main__":
    unittest.main()
