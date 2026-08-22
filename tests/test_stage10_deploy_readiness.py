"""Stage 10 production deployment-readiness checks (no secrets, no live calls)."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RenderBlueprintTests(unittest.TestCase):
    def setUp(self):
        self.render = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.start = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.gunicorn = (ROOT / "gunicorn.conf.py").read_text(encoding="utf-8")
        self.cron = (ROOT / "scripts" / "run_internal_job.py").read_text(encoding="utf-8")

    def test_health_check_uses_readiness(self):
        self.assertIn("healthCheckPath: /api/readiness", self.render)
        self.assertNotIn("healthCheckPath: /ping", self.render)
        self.assertNotIn("healthCheckPath: /api/health", self.render)

    def test_saturday_payout_is_1800_ist_via_utc(self):
        self.assertIn('schedule: "30 12 * * 6"', self.render)
        self.assertIn("18:00 IST", self.render)

    def test_referral_retry_every_15_minutes(self):
        self.assertIn('schedule: "*/15 * * * *"', self.render)

    def test_cron_requires_payout_secret_and_base_url(self):
        self.assertIn("SATURDAY_PAYOUT_SECRET", self.render)
        self.assertGreaterEqual(self.render.count("key: SATURDAY_PAYOUT_SECRET"), 3)
        self.assertIn("key: BASE_URL", self.render)
        self.assertIn("python3 scripts/run_internal_job.py /internal/saturday-payout", self.render)
        self.assertIn("python3 scripts/run_internal_job.py /internal/referral-commission-retry", self.render)
        self.assertIn("SATURDAY_PAYOUT_SECRET is required", self.cron)
        self.assertNotIn("JWT_SECRET_KEY", self.cron)
        self.assertIn("timeout=120", self.cron)

    def test_gunicorn_single_sync_worker(self):
        self.assertIn("workers = 1", self.gunicorn)
        self.assertIn("--workers 1", self.start)
        self.assertIn("app:app", self.start)
        self.assertIn("--timeout 120", self.start)


class RazorpayModeGuardTests(unittest.TestCase):
    def test_live_mode_rejects_test_keys(self):
        from config.payment_config import get_razorpay_key_pair
        env = {
            "RAZORPAY_MODE": "live",
            "RAZORPAY_KEY_ID": "rzp_test_not_for_prod",
            "RAZORPAY_KEY_SECRET": "testsecret",
            "RENDER": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            key_id, key_secret = get_razorpay_key_pair()
        self.assertIsNone(key_id)
        self.assertIsNone(key_secret)

    def test_test_mode_rejects_live_keys(self):
        from config.payment_config import get_razorpay_key_pair
        env = {
            "RAZORPAY_MODE": "test",
            "RAZORPAY_KEY_ID": "rzp_live_not_for_test",
            "RAZORPAY_KEY_SECRET": "livesecret",
            "RENDER": "",
        }
        with patch.dict(os.environ, env, clear=False):
            key_id, key_secret = get_razorpay_key_pair()
        self.assertIsNone(key_id)
        self.assertIsNone(key_secret)


class SchemaDeployGuardTests(unittest.TestCase):
    def test_init_db_does_not_auto_add_reviews_user_id(self):
        init_src = (ROOT / "database" / "init_db.py").read_text(encoding="utf-8")
        self.assertNotIn("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS user_id", init_src)
        self.assertNotIn("uq_reviews_listing_user", init_src)
        migration = (ROOT / "migrations" / "add_reviews_user_id_unique.py").read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS user_id", migration)
        self.assertIn("Do not apply this migration to production from this script.", migration)


if __name__ == "__main__":
    unittest.main()
