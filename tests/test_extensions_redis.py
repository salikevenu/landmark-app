"""Redis is optional: limiter/JWT blocklist must boot without a reachable Redis."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_unreachable_redis_uses_memory_without_raising(self):
        from extensions import limiter_storage_uri
        with patch.dict(os.environ, {"REDIS_URL": "redis://no-such-host.invalid:6379/0"}, clear=False):
            self.assertEqual(limiter_storage_uri(), "memory://")

    def test_reachable_redis_is_selected(self):
        from extensions import limiter_storage_uri
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        url = "redis://localhost:6379/0"
        with patch.dict(os.environ, {"REDIS_URL": url}, clear=False):
            with patch("redis.from_url", return_value=mock_client):
                self.assertEqual(limiter_storage_uri(), url)
        mock_client.ping.assert_called()

    def test_logger_exists_on_fallback_path(self):
        import extensions as ext
        self.assertTrue(hasattr(ext.logger, "warning"))
        src = (ROOT / "extensions.py").read_text(encoding="utf-8")
        fn = src.split("def init_extensions")[1].split("def get_razorpay_client")[0]
        self.assertNotIn("logger = ", fn)
        with patch.object(ext.logger, "warning") as warn:
            with patch.dict(os.environ, {"REDIS_URL": "redis://no-such-host.invalid:6379/0"}, clear=False):
                self.assertEqual(ext.limiter_storage_uri(), "memory://")
        warn.assert_called()


class InitExtensionsBootTests(unittest.TestCase):
    def test_init_extensions_survives_unreachable_redis(self):
        from extensions import init_extensions
        app = Flask(__name__)
        with patch.dict(os.environ, {"REDIS_URL": "redis://no-such-host.invalid:6379/0"}, clear=False):
            limiter, _razor = init_extensions(app)
        self.assertIsNotNone(limiter)


if __name__ == "__main__":
    unittest.main()
