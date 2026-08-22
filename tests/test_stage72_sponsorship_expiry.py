"""Stage 7.2 live sponsorship window and expiry cleanup."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.sponsorship import (
    CLEANUP_EXPIRED_SQL,
    cleanup_expired_sponsorships,
    is_live_sponsored,
    public_is_sponsored_sql,
)
from services.listing_service import update_sponsored_status


NOW = datetime(2026, 8, 22, 15, 0, 0)


class LiveWindowTests(unittest.TestCase):
    def test_active_is_sponsored(self):
        self.assertTrue(is_live_sponsored(
            is_sponsored=1,
            start_date=NOW - timedelta(days=1),
            end_date=NOW + timedelta(days=1),
            now=NOW,
        ))

    def test_future_not_sponsored(self):
        self.assertFalse(is_live_sponsored(
            is_sponsored=1,
            start_date=NOW + timedelta(hours=1),
            end_date=NOW + timedelta(days=30),
            now=NOW,
        ))

    def test_expired_not_sponsored(self):
        self.assertFalse(is_live_sponsored(
            is_sponsored=1,
            start_date=NOW - timedelta(days=30),
            end_date=NOW - timedelta(seconds=1),
            now=NOW,
        ))

    def test_exact_expiry_boundary(self):
        self.assertFalse(is_live_sponsored(
            is_sponsored=1,
            start_date=NOW - timedelta(days=30),
            end_date=NOW,
            now=NOW,
        ))

    def test_flag_off_not_sponsored(self):
        self.assertFalse(is_live_sponsored(
            is_sponsored=0,
            start_date=NOW - timedelta(days=1),
            end_date=NOW + timedelta(days=1),
            now=NOW,
        ))

    def test_expired_does_not_rank_or_badge(self):
        expired = {
            "id": 1,
            "sponsored": is_live_sponsored(
                is_sponsored=1,
                start_date=NOW - timedelta(days=40),
                end_date=NOW - timedelta(days=1),
                now=NOW,
            ),
            "rating": 5,
        }
        live = {
            "id": 2,
            "sponsored": is_live_sponsored(
                is_sponsored=1,
                start_date=NOW - timedelta(days=1),
                end_date=NOW + timedelta(days=1),
                now=NOW,
            ),
            "rating": 1,
        }
        ordered = sorted([expired, live], key=lambda x: (-x["sponsored"], -x["rating"]))
        self.assertEqual(ordered[0]["id"], 2)
        self.assertFalse(expired["sponsored"])
        badge = '<span class="sponsored">Sponsored</span>' if expired["sponsored"] else ""
        self.assertEqual(badge, "")


class PublicSqlTests(unittest.TestCase):
    def test_nearby_and_browse_use_window(self):
        sql = public_is_sponsored_sql("")
        self.assertIn("sponsored_ads", sql)
        self.assertIn("start_date <= CURRENT_TIMESTAMP", sql)
        self.assertIn("end_date > CURRENT_TIMESTAMP", sql)
        nearby = (ROOT / "services" / "nearby_service.py").read_text(encoding="utf-8")
        self.assertIn("public_is_sponsored_sql", nearby)
        routes = (ROOT / "routes" / "nearby_routes.py").read_text(encoding="utf-8")
        self.assertIn("sponsorship_rank_sql", routes)
        self.assertNotIn("ORDER BY is_sponsored DESC", routes)
        browse = (ROOT / "routes" / "listing_routes.py").read_text(encoding="utf-8")
        self.assertIn("ORDER BY sponsored DESC", browse)
        user = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        self.assertIn("sponsorship_rank_sql", user)

    def test_owner_cannot_create_or_modify(self):
        listing = (ROOT / "routes" / "listing_routes.py").read_text(encoding="utf-8")
        create = listing.split("def api_create_listing")[1].split("def my_listings")[0]
        self.assertNotIn('request.form.get("is_sponsored")', create)
        update = listing.split("def update_listing")[1].split("def delete_listing")[0]
        self.assertNotIn("is_sponsored", update)
        self.assertNotIn("/sponsor", listing)


class CleanupConn:
    def __init__(self, listings):
        self.listings = listings
        self.updates = 0
        self.deletes = 0
        self.featured_writes = 0

    def execute(self, query, params=None):
        q = " ".join(str(getattr(query, "text", query)).lower().split())
        res = MagicMock()
        res.rowcount = 0
        if "delete from sponsored_ads" in q:
            self.deletes += 1
        if "is_featured" in q and q.startswith("update"):
            self.featured_writes += 1
        if q.startswith("update listings") and "is_sponsored = 0" in q:
            self.updates += 1
            cleared = 0
            now = NOW
            for row in self.listings:
                if not row["is_sponsored"]:
                    continue
                ads = row.get("ads") or []
                keep = any(
                    ad.get("is_active", 1) and ad["end_date"] > now
                    for ad in ads
                )
                if not keep:
                    row["is_sponsored"] = 0
                    row["is_featured"] = row.get("is_featured", 0)
                    cleared += 1
            res.rowcount = cleared
        return res

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class CleanupTests(unittest.TestCase):
    def test_clears_expired_only_and_is_idempotent(self):
        rows = [
            {
                "id": 1,
                "is_sponsored": 1,
                "is_featured": 1,
                "ads": [{"is_active": 1, "end_date": NOW - timedelta(days=1)}],
            },
            {
                "id": 2,
                "is_sponsored": 1,
                "is_featured": 0,
                "ads": [{"is_active": 1, "end_date": NOW + timedelta(days=5)}],
            },
            {
                "id": 3,
                "is_sponsored": 1,
                "is_featured": 0,
                "ads": [{"is_active": 1, "end_date": NOW + timedelta(days=10),
                         "start_date": NOW + timedelta(days=1)}],
            },
        ]
        conn = CleanupConn(rows)
        with patch("services.sponsorship.get_db_connection", return_value=conn):
            first = cleanup_expired_sponsorships()
            second = cleanup_expired_sponsorships()
        self.assertEqual(first["cleared"], 1)
        self.assertEqual(second["cleared"], 0)
        self.assertEqual(rows[0]["is_sponsored"], 0)
        self.assertEqual(rows[0]["is_featured"], 1)
        self.assertEqual(rows[1]["is_sponsored"], 1)
        self.assertEqual(rows[2]["is_sponsored"], 1)
        self.assertEqual(conn.deletes, 0)
        self.assertEqual(conn.featured_writes, 0)

    def test_concurrent_cleanup_cannot_re_set_flag(self):
        rows = [{
            "id": 1,
            "is_sponsored": 1,
            "is_featured": 0,
            "ads": [{"is_active": 1, "end_date": NOW - timedelta(days=2)}],
        }]
        conn = CleanupConn(rows)
        with patch("services.sponsorship.get_db_connection", return_value=conn):
            cleanup_expired_sponsorships()
            cleanup_expired_sponsorships()
        self.assertEqual(rows[0]["is_sponsored"], 0)
        self.assertNotIn("is_sponsored = 1", CLEANUP_EXPIRED_SQL.lower())
        self.assertIn("SKIP LOCKED", CLEANUP_EXPIRED_SQL)
        self.assertNotIn("DELETE FROM sponsored_ads", CLEANUP_EXPIRED_SQL)
        self.assertNotIn("is_featured", CLEANUP_EXPIRED_SQL)

    def test_update_sponsored_status_delegates(self):
        with patch("services.sponsorship.cleanup_expired_sponsorships", return_value={"cleared": 2}) as fn:
            out = update_sponsored_status()
        self.assertEqual(out["cleared"], 2)
        fn.assert_called_once()

    def test_saturday_payout_calls_cleanup(self):
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        payout = app_src.split("def saturday_payout")[1].split("def referral_commission_retry")[0]
        self.assertIn("cleanup_expired_sponsorships", payout)


if __name__ == "__main__":
    unittest.main()
