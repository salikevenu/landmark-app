"""Rank qualification, batch recomputation, and Monthly Growth Reward tests.

Uses a REAL in-memory SQLite database (not a hand-mocked connection) so the
actual recursive CTE and fixpoint-convergence logic in
services.rank_service are genuinely exercised. SQLite supports
WITH RECURSIVE and ON CONFLICT ... DO UPDATE/DO NOTHING, which is all this
service uses — no Postgres-only syntax appears in rank_service.py itself.

Two testing strategies are combined deliberately:
- highest_qualifying_rank() is tested directly (no DB) against the REAL,
  finalized Leader/Ranger thresholds (500/100/30/10 and 2000/400/100/30/10)
  — fast and exact, since building literal trees of hundreds of qualifying
  users just to prove threshold comparison logic would be wasteful.
- The DB-integration tests (traversal, fixpoint convergence, cumulative
  counting, idempotency) patch config.rank_config.RANK_REQUIREMENTS down to
  small test-scale numbers, because what they're proving — does the
  recursive CTE / fixpoint loop / reward ledger work correctly — does not
  depend on which specific numbers are configured.
"""
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import services.rank_service as rank_service
import config.rank_config as rank_config
from config.rank_config import (
    RANK_REQUIREMENTS,
    highest_qualifying_rank,
    LEADER,
    RANGER,
    LEADER_MONTHLY_REWARD_MIN_INR,
    REWARD_TYPE_LEADER_MONTHLY,
    REWARD_TYPE_RANGER_MONTHLY,
)

FUTURE = (date.today() + timedelta(days=365)).strftime("%Y-%m-%d")
PAST = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")

# Small test-scale thresholds used ONLY by the DB-integration tests below,
# via patch — proves the traversal/fixpoint/idempotency machinery, not the
# real finalized numbers (those are covered by ThresholdUnitTests using the
# actual config.rank_config.RANK_REQUIREMENTS values, unpatched).
TEST_SCALE_REQUIREMENTS = {
    "member": {"verified_users": 1, "active_subscribers": 1},
    "guide": {"verified_users": 1, "active_subscribers": 1, "qualified_members": 5},
    "leader": {"verified_users": 1, "active_subscribers": 1, "qualified_members": 5, "qualified_guides": 3},
    "ranger": {"verified_users": 1, "active_subscribers": 1, "qualified_members": 5,
               "qualified_guides": 3, "qualified_leaders": 2},
}


def _make_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                referred_by INTEGER,
                is_active INTEGER DEFAULT 1,
                is_blocked INTEGER DEFAULT 0,
                plan TEXT DEFAULT 'free',
                subscription_expiry TEXT,
                phone TEXT,
                name TEXT,
                referral_code TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE user_rank_stats (
                user_id INTEGER PRIMARY KEY,
                rank TEXT NOT NULL DEFAULT 'unranked',
                verified_users_count INTEGER NOT NULL DEFAULT 0,
                active_subscribers_count INTEGER NOT NULL DEFAULT 0,
                qualified_members_count INTEGER NOT NULL DEFAULT 0,
                qualified_guides_count INTEGER NOT NULL DEFAULT 0,
                qualified_leaders_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE rank_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                previous_rank TEXT NOT NULL,
                new_rank TEXT NOT NULL,
                milestone_key TEXT,
                achieved_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE rank_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                milestone_key TEXT NOT NULL,
                reward_type TEXT,
                reward_period TEXT,
                rank TEXT NOT NULL,
                amount_inr NUMERIC(12,2) NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                approved_by INTEGER,
                approved_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, milestone_key),
                UNIQUE(user_id, reward_type, reward_period)
            )
        """))
        conn.execute(text("""
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                source TEXT,
                status TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE wallet_balance (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0
            )
        """))
    return engine


def _seed_user(engine, uid, referred_by=None, is_active=1, is_blocked=0,
                plan="free", subscription_expiry=None):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO users (id, referred_by, is_active, is_blocked, plan, subscription_expiry, phone)
            VALUES (:id, :ref, :act, :blk, :plan, :exp, :phone)
        """), {
            "id": uid, "ref": referred_by, "act": is_active, "blk": is_blocked,
            "plan": plan, "exp": subscription_expiry, "phone": f"90000{uid:05d}",
        })


def _rank_of(engine, uid):
    with engine.connect() as conn:
        row = conn.execute(text("SELECT rank FROM user_rank_stats WHERE user_id=:uid"), {"uid": uid}).fetchone()
        return row._mapping["rank"] if row else None


def _set_rank_directly(engine, uid, rank):
    """Test-only shortcut: seed user_rank_stats directly for reward-layer
    tests that don't need a full qualifying tree underneath (that traversal
    logic is already covered separately)."""
    _seed_user(engine, uid, plan="business_basic", subscription_expiry=FUTURE)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO user_rank_stats (user_id, rank, verified_users_count, active_subscribers_count,
                qualified_members_count, qualified_guides_count, qualified_leaders_count)
            VALUES (:uid, :rank, 0, 0, 0, 0, 0)
        """), {"uid": uid, "rank": rank})


def _seed_qualified_member(engine, uid, referred_by, next_id_holder):
    """uid is a Member: seed one verified + subscribed referral under uid."""
    child_id = next_id_holder[0]
    next_id_holder[0] += 1
    _seed_user(engine, uid, referred_by=referred_by, plan="business_basic", subscription_expiry=FUTURE)
    _seed_user(engine, child_id, referred_by=uid, plan="business_basic", subscription_expiry=FUTURE)
    return child_id


def _seed_qualified_member_auto(engine, referred_by, next_id_holder):
    member_id = next_id_holder[0]
    next_id_holder[0] += 1
    return _seed_qualified_member(engine, member_id, referred_by, next_id_holder)


def _seed_payment(engine, user_id, amount, status="verified", created_at=None):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO payments (user_id, amount, status, created_at)
            VALUES (:uid, :amt, :status, :created_at)
        """), {"uid": user_id, "amt": amount, "status": status, "created_at": created_at or date.today().isoformat()})


class ThresholdUnitTests(unittest.TestCase):
    """15.1-15.4: exact finalized Leader/Ranger thresholds, no DB needed —
    tests the real config.rank_config.RANK_REQUIREMENTS directly."""

    def test_leader_thresholds_are_the_finalized_business_numbers(self):
        self.assertEqual(RANK_REQUIREMENTS[LEADER], {
            "verified_users": 500, "active_subscribers": 100,
            "qualified_members": 30, "qualified_guides": 10,
        })

    def test_ranger_thresholds_are_the_finalized_business_numbers(self):
        self.assertEqual(RANK_REQUIREMENTS[RANGER], {
            "verified_users": 2000, "active_subscribers": 400,
            "qualified_members": 100, "qualified_guides": 30, "qualified_leaders": 10,
        })

    def test_leader_qualifies_at_exactly_500_100_30_10(self):
        metrics = {"verified_users": 500, "active_subscribers": 100, "qualified_members": 30, "qualified_guides": 10}
        self.assertEqual(highest_qualifying_rank(metrics), "leader")

    def test_leader_fails_if_verified_users_short_by_one(self):
        metrics = {"verified_users": 499, "active_subscribers": 100, "qualified_members": 30, "qualified_guides": 10}
        self.assertNotEqual(highest_qualifying_rank(metrics), "leader")

    def test_leader_fails_if_active_subscribers_short_by_one(self):
        metrics = {"verified_users": 500, "active_subscribers": 99, "qualified_members": 30, "qualified_guides": 10}
        self.assertNotEqual(highest_qualifying_rank(metrics), "leader")

    def test_leader_fails_if_qualified_members_short_by_one(self):
        metrics = {"verified_users": 500, "active_subscribers": 100, "qualified_members": 29, "qualified_guides": 10}
        self.assertNotEqual(highest_qualifying_rank(metrics), "leader")

    def test_leader_fails_if_qualified_guides_short_by_one(self):
        metrics = {"verified_users": 500, "active_subscribers": 100, "qualified_members": 30, "qualified_guides": 9}
        self.assertNotEqual(highest_qualifying_rank(metrics), "leader")

    def test_ranger_qualifies_at_exactly_2000_400_100_30_10(self):
        metrics = {"verified_users": 2000, "active_subscribers": 400, "qualified_members": 100,
                   "qualified_guides": 30, "qualified_leaders": 10}
        self.assertEqual(highest_qualifying_rank(metrics), "ranger")

    def test_ranger_falls_back_to_leader_if_qualified_leaders_short_by_one(self):
        metrics = {"verified_users": 2000, "active_subscribers": 400, "qualified_members": 100,
                   "qualified_guides": 30, "qualified_leaders": 9}
        self.assertEqual(highest_qualifying_rank(metrics), "leader")

    def test_ranger_fails_if_verified_users_short(self):
        metrics = {"verified_users": 1999, "active_subscribers": 400, "qualified_members": 100,
                   "qualified_guides": 30, "qualified_leaders": 10}
        self.assertNotEqual(highest_qualifying_rank(metrics), "ranger")

    def test_ranger_fails_if_active_subscribers_short(self):
        metrics = {"verified_users": 2000, "active_subscribers": 399, "qualified_members": 100,
                   "qualified_guides": 30, "qualified_leaders": 10}
        self.assertNotEqual(highest_qualifying_rank(metrics), "ranger")

    def test_ranger_fails_if_qualified_members_short(self):
        metrics = {"verified_users": 2000, "active_subscribers": 400, "qualified_members": 99,
                   "qualified_guides": 30, "qualified_leaders": 10}
        self.assertNotEqual(highest_qualifying_rank(metrics), "ranger")

    def test_ranger_fails_if_qualified_guides_short(self):
        metrics = {"verified_users": 2000, "active_subscribers": 400, "qualified_members": 100,
                   "qualified_guides": 29, "qualified_leaders": 10}
        self.assertNotEqual(highest_qualifying_rank(metrics), "ranger")

    def test_member_and_guide_thresholds_are_marked_provisional_in_source(self):
        """Member/Guide numbers must not read as finalized business policy."""
        src = (ROOT / "config" / "rank_config.py").read_text(encoding="utf-8")
        self.assertIn("PROVISIONAL", src)
        self.assertIn("not yet finalized business policy", src)


class RankQualificationDbTests(unittest.TestCase):
    """Traversal/fixpoint/exclusion/history mechanics — patched to
    small test-scale thresholds; the exact real numbers are covered by
    ThresholdUnitTests above."""

    def setUp(self):
        self.engine = _make_engine()
        self.db_patcher = patch.object(rank_service, "get_db_connection", side_effect=self.engine.connect)
        self.db_patcher.start()
        self.cfg_patcher = patch.object(rank_config, "RANK_REQUIREMENTS", TEST_SCALE_REQUIREMENTS)
        self.cfg_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.cfg_patcher.stop()

    def test_isolated_user_with_no_referrals_stays_unranked(self):
        _seed_user(self.engine, 1)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "unranked")

    def test_member_qualification_requires_verified_and_active_subscriber(self):
        _seed_user(self.engine, 1)
        _seed_user(self.engine, 2, referred_by=1, plan="business_basic", subscription_expiry=FUTURE)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "member")

    def test_verified_without_active_subscription_is_not_enough_for_member(self):
        _seed_user(self.engine, 1)
        _seed_user(self.engine, 2, referred_by=1, plan="free")
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "unranked")

    def test_expired_subscription_does_not_count_as_active(self):
        _seed_user(self.engine, 1)
        _seed_user(self.engine, 2, referred_by=1, plan="business_basic", subscription_expiry=PAST)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "unranked")

    def test_banned_referral_does_not_count_toward_referrer(self):
        _seed_user(self.engine, 1)
        _seed_user(self.engine, 2, referred_by=1, is_blocked=1, plan="business_basic", subscription_expiry=FUTURE)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "unranked")

    def test_banned_user_is_always_unranked_regardless_of_own_metrics(self):
        _seed_user(self.engine, 1, is_blocked=1)
        _seed_user(self.engine, 2, referred_by=1, plan="business_basic", subscription_expiry=FUTURE)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "unranked")

    def test_inactive_user_excluded(self):
        _seed_user(self.engine, 1)
        _seed_user(self.engine, 2, referred_by=1, is_active=0, plan="business_basic", subscription_expiry=FUTURE)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "unranked")

    def test_guide_requires_five_qualified_members(self):
        next_id = [100]
        _seed_user(self.engine, 1, plan="business_basic", subscription_expiry=FUTURE)
        for mid in [2, 3, 4, 5]:
            _seed_qualified_member(self.engine, mid, referred_by=1, next_id_holder=next_id)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "member", "4 members should not be enough for Guide")

        _seed_qualified_member(self.engine, 6, referred_by=1, next_id_holder=next_id)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "guide", "5 qualified members should reach Guide")

    def test_cumulative_counting_guide_counts_toward_members_quota(self):
        next_id = [200]
        _seed_user(self.engine, 1, plan="business_basic", subscription_expiry=FUTURE)
        _seed_user(self.engine, 10, referred_by=1, plan="business_basic", subscription_expiry=FUTURE)
        for _ in range(5):
            _seed_qualified_member_auto(self.engine, referred_by=10, next_id_holder=next_id)
        for _ in range(4):
            _seed_qualified_member_auto(self.engine, referred_by=1, next_id_holder=next_id)

        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 10), "guide")
        self.assertEqual(_rank_of(self.engine, 1), "guide",
                          "a Guide in the downline must count toward the Members quota too")

    def test_leader_requires_three_qualified_guides_at_test_scale(self):
        next_id = [1000]

        def make_guide(guide_id, parent_id):
            _seed_user(self.engine, guide_id, referred_by=parent_id, plan="business_basic", subscription_expiry=FUTURE)
            for _ in range(5):
                _seed_qualified_member_auto(self.engine, referred_by=guide_id, next_id_holder=next_id)

        _seed_user(self.engine, 1, plan="business_basic", subscription_expiry=FUTURE)
        make_guide(11, 1)
        make_guide(12, 1)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "guide", "2 guides should not reach Leader")

        make_guide(13, 1)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "leader", "3 qualified guides should reach Leader")

    def test_ranger_requires_two_qualified_leaders_deep_tree_converges_in_one_call(self):
        next_id = [10000]

        def make_guide(guide_id, parent_id):
            _seed_user(self.engine, guide_id, referred_by=parent_id, plan="business_basic", subscription_expiry=FUTURE)
            for _ in range(5):
                _seed_qualified_member_auto(self.engine, referred_by=guide_id, next_id_holder=next_id)

        def make_leader(leader_id, parent_id):
            _seed_user(self.engine, leader_id, referred_by=parent_id, plan="business_basic", subscription_expiry=FUTURE)
            for i in range(3):
                guide_id = next_id[0]
                next_id[0] += 1
                make_guide(guide_id, leader_id)

        _seed_user(self.engine, 1, plan="business_basic", subscription_expiry=FUTURE)
        make_leader(21, 1)
        make_leader(22, 1)

        rank_service.recompute_all_ranks()  # single call must fully converge

        self.assertEqual(_rank_of(self.engine, 21), "leader")
        self.assertEqual(_rank_of(self.engine, 22), "leader")
        self.assertEqual(_rank_of(self.engine, 1), "ranger")

    def test_rank_progression_as_network_grows(self):
        next_id = [50000]
        _seed_user(self.engine, 1, plan="business_basic", subscription_expiry=FUTURE)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "unranked")

        _seed_qualified_member_auto(self.engine, referred_by=1, next_id_holder=next_id)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "member")

    def test_achievement_history_preserved_when_rank_later_drops(self):
        next_id = [60000]
        _seed_qualified_member(self.engine, 1, referred_by=None, next_id_holder=next_id)
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "member")

        with self.engine.begin() as conn:
            conn.execute(text("UPDATE users SET is_blocked=1 WHERE id=1"))
        rank_service.recompute_all_ranks()
        self.assertEqual(_rank_of(self.engine, 1), "unranked")

        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT new_rank FROM rank_achievements WHERE user_id=1 ORDER BY id"
            )).fetchall()
        self.assertEqual([r._mapping["new_rank"] for r in rows], ["member"],
                          "the earlier achievement must survive the later demotion")


class MonthlyGrowthRewardTests(unittest.TestCase):
    """Section 4-10, 15 (monthly reward items): the finalized monthly
    reward model. Ranks are seeded directly (via _set_rank_directly) since
    the qualification/traversal mechanics are already proven above — these
    tests focus purely on evaluate_monthly_rewards()'s own behavior."""

    def setUp(self):
        self.engine = _make_engine()
        self.patcher = patch.object(rank_service, "get_db_connection", side_effect=self.engine.connect)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _rewards_for(self, uid):
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT reward_type, reward_period, amount_inr, status FROM rank_rewards WHERE user_id=:uid"
            ), {"uid": uid}).fetchall()
        return [dict(r._mapping) for r in rows]

    # --- Leader monthly reward ---
    def test_qualified_leader_gets_a_pending_monthly_reward(self):
        _set_rank_directly(self.engine, 1, "leader")
        result = rank_service.evaluate_monthly_rewards(period="2026-09")
        self.assertEqual(result["rewards_created"], 1)
        rewards = self._rewards_for(1)
        self.assertEqual(len(rewards), 1)
        self.assertEqual(rewards[0]["reward_type"], REWARD_TYPE_LEADER_MONTHLY)
        self.assertEqual(rewards[0]["reward_period"], "2026-09")
        self.assertEqual(rewards[0]["status"], "pending", "reward must remain pending until admin approval")
        self.assertEqual(float(rewards[0]["amount_inr"]), float(LEADER_MONTHLY_REWARD_MIN_INR))

    def test_member_and_guide_never_get_a_monthly_reward(self):
        _set_rank_directly(self.engine, 1, "member")
        _set_rank_directly(self.engine, 2, "guide")
        rank_service.evaluate_monthly_rewards(period="2026-09")
        self.assertEqual(self._rewards_for(1), [])
        self.assertEqual(self._rewards_for(2), [])

    # --- Ranger monthly reward: revenue-backed, placeholder-off by default ---
    def test_ranger_reward_is_zero_and_no_row_created_with_placeholder_policy(self):
        """Default config (pool%=0, cap=0) must never create an obligation."""
        _set_rank_directly(self.engine, 1, "ranger")
        _seed_payment(self.engine, 1, 100000, status="verified")
        result = rank_service.evaluate_monthly_rewards(period="2026-09")
        self.assertEqual(result["rewards_created"], 0)
        self.assertEqual(self._rewards_for(1), [])

    def test_ranger_reward_is_revenue_backed_when_policy_is_configured(self):
        _set_rank_directly(self.engine, 1, "ranger")
        _seed_payment(self.engine, 99, 100000, status="verified")  # revenue this period
        with patch.object(rank_config, "RANGER_REWARD_POOL_PERCENTAGE", 10), \
             patch.object(rank_config, "RANGER_MONTHLY_CAP_INR", 100000), \
             patch.object(rank_service, "RANGER_REWARD_POOL_PERCENTAGE", 10), \
             patch.object(rank_service, "RANGER_MONTHLY_CAP_INR", 100000):
            today = date.today().strftime("%Y-%m")
            _seed_payment(self.engine, 99, 100000, status="verified", created_at=today + "-15")
            result = rank_service.evaluate_monthly_rewards(period=today)
        rewards = self._rewards_for(1)
        self.assertEqual(len(rewards), 1)
        self.assertEqual(rewards[0]["reward_type"], REWARD_TYPE_RANGER_MONTHLY)
        # 10% of >= 100000 revenue = >= 10000, single Ranger, under the cap.
        self.assertGreaterEqual(float(rewards[0]["amount_inr"]), 10000.0)

    def test_ranger_reward_capped_even_with_large_pool(self):
        _set_rank_directly(self.engine, 1, "ranger")
        today = date.today().strftime("%Y-%m")
        _seed_payment(self.engine, 99, 10_000_000, status="verified", created_at=today + "-05")
        with patch.object(rank_config, "RANGER_REWARD_POOL_PERCENTAGE", 50), \
             patch.object(rank_config, "RANGER_MONTHLY_CAP_INR", 5000), \
             patch.object(rank_service, "RANGER_REWARD_POOL_PERCENTAGE", 50), \
             patch.object(rank_service, "RANGER_MONTHLY_CAP_INR", 5000):
            rank_service.evaluate_monthly_rewards(period=today)
        rewards = self._rewards_for(1)
        self.assertEqual(len(rewards), 1)
        self.assertLessEqual(float(rewards[0]["amount_inr"]), 5000.0,
                              "per-Ranger amount must never exceed the configured cap")

    def test_ranger_pool_split_evenly_across_qualifying_rangers(self):
        _set_rank_directly(self.engine, 1, "ranger")
        _set_rank_directly(self.engine, 2, "ranger")
        today = date.today().strftime("%Y-%m")
        _seed_payment(self.engine, 99, 100000, status="verified", created_at=today + "-10")
        with patch.object(rank_config, "RANGER_REWARD_POOL_PERCENTAGE", 10), \
             patch.object(rank_config, "RANGER_MONTHLY_CAP_INR", 1_000_000), \
             patch.object(rank_service, "RANGER_REWARD_POOL_PERCENTAGE", 10), \
             patch.object(rank_service, "RANGER_MONTHLY_CAP_INR", 1_000_000):
            rank_service.evaluate_monthly_rewards(period=today)
        amt1 = self._rewards_for(1)[0]["amount_inr"]
        amt2 = self._rewards_for(2)[0]["amount_inr"]
        self.assertEqual(float(amt1), float(amt2), "an even split must give each qualifying Ranger the same amount")

    # --- Idempotency (section 9, test list 8/9/10/11) ---
    def test_reward_period_uniqueness_and_no_duplicate_on_repeated_recomputation(self):
        _set_rank_directly(self.engine, 1, "leader")
        for _ in range(4):
            rank_service.evaluate_monthly_rewards(period="2026-09")
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT COUNT(*) AS c FROM rank_rewards WHERE user_id=1 AND reward_type=:t AND reward_period='2026-09'"
            ), {"t": REWARD_TYPE_LEADER_MONTHLY}).fetchone()
        self.assertEqual(row._mapping["c"], 1, "4 evaluations of the same period must yield exactly one reward row")

    def test_previous_month_reward_does_not_block_current_month(self):
        _set_rank_directly(self.engine, 1, "leader")
        rank_service.evaluate_monthly_rewards(period="2026-08")
        rank_service.evaluate_monthly_rewards(period="2026-09")
        rewards = self._rewards_for(1)
        periods = sorted(r["reward_period"] for r in rewards)
        self.assertEqual(periods, ["2026-08", "2026-09"])

    def test_loss_of_qualification_prevents_a_new_monthly_reward(self):
        _set_rank_directly(self.engine, 1, "leader")
        rank_service.evaluate_monthly_rewards(period="2026-08")
        # Demote for the next period (no longer a Leader in user_rank_stats).
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE user_rank_stats SET rank='member' WHERE user_id=1"))
        rank_service.evaluate_monthly_rewards(period="2026-09")
        rewards = self._rewards_for(1)
        self.assertEqual([r["reward_period"] for r in rewards], ["2026-08"],
                          "no longer qualifying must not create a reward for the new period")

    def test_demotion_and_repromotion_within_same_month_does_not_duplicate(self):
        _set_rank_directly(self.engine, 1, "leader")
        rank_service.evaluate_monthly_rewards(period="2026-09")
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE user_rank_stats SET rank='member' WHERE user_id=1"))
        rank_service.evaluate_monthly_rewards(period="2026-09")  # no-op: not a leader right now
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE user_rank_stats SET rank='leader' WHERE user_id=1"))
        rank_service.evaluate_monthly_rewards(period="2026-09")  # re-qualified, same period
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT COUNT(*) AS c FROM rank_rewards WHERE user_id=1 AND reward_period='2026-09'"
            )).fetchone()
        self.assertEqual(row._mapping["c"], 1)

    # --- Revenue safety / no automatic payout ---
    def test_no_automatic_wallet_payout_in_evaluate_monthly_rewards_source(self):
        """Structural check: the reward-evaluation function must never
        reference wallet_transactions / wallet_balance / referral_transactions."""
        src = (ROOT / "services" / "rank_service.py").read_text(encoding="utf-8")
        fn_src = src.split("def evaluate_monthly_rewards")[1].split("\ndef ")[0]
        for forbidden in ("wallet_transactions", "wallet_balance", "referral_transactions"):
            self.assertNotIn(forbidden, fn_src, f"evaluate_monthly_rewards must not touch {forbidden}")

    def test_evaluate_monthly_rewards_never_writes_wallet_tables(self):
        _set_rank_directly(self.engine, 1, "leader")
        _set_rank_directly(self.engine, 2, "ranger")
        with patch.object(rank_config, "RANGER_REWARD_POOL_PERCENTAGE", 100), \
             patch.object(rank_config, "RANGER_MONTHLY_CAP_INR", 1_000_000), \
             patch.object(rank_service, "RANGER_REWARD_POOL_PERCENTAGE", 100), \
             patch.object(rank_service, "RANGER_MONTHLY_CAP_INR", 1_000_000):
            _seed_payment(self.engine, 99, 500000, status="verified")
            rank_service.evaluate_monthly_rewards(period="2026-09")
        with self.engine.connect() as conn:
            wt = conn.execute(text("SELECT COUNT(*) AS c FROM wallet_transactions")).fetchone()
            wb = conn.execute(text("SELECT COUNT(*) AS c FROM wallet_balance")).fetchone()
        self.assertEqual(wt._mapping["c"], 0)
        self.assertEqual(wb._mapping["c"], 0)

    def test_reward_stays_pending_until_admin_approves(self):
        _set_rank_directly(self.engine, 1, "leader")
        rank_service.evaluate_monthly_rewards(period="2026-09")
        self.assertEqual(self._rewards_for(1)[0]["status"], "pending")

        with self.engine.connect() as conn:
            reward_id = conn.execute(text(
                "SELECT id FROM rank_rewards WHERE user_id=1"
            )).fetchone()._mapping["id"]
        updated = rank_service.approve_reward(reward_id, admin_id=1)
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(self._rewards_for(1)[0]["status"], "approved")


class LeaderRewardPoolCapTests(unittest.TestCase):
    """Financial safety fix: LEADER_MONTHLY_REWARD_POOL_CAP_INR.

    Patches both config.rank_config and services.rank_service's bound
    copies of the cap constant (rank_service imports it by name, so only
    patching config.rank_config would not affect evaluate_monthly_rewards'
    already-bound reference) — same pattern already used for the Ranger
    pool percentage/cap in MonthlyGrowthRewardTests above.
    """

    def setUp(self):
        self.engine = _make_engine()
        self.patcher = patch.object(rank_service, "get_db_connection", side_effect=self.engine.connect)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _cap(self, value):
        return (
            patch.object(rank_config, "LEADER_MONTHLY_REWARD_POOL_CAP_INR", value),
            patch.object(rank_service, "LEADER_MONTHLY_REWARD_POOL_CAP_INR", value),
        )

    def _seed_leaders(self, n, start_id=1):
        ids = list(range(start_id, start_id + n))
        for uid in ids:
            _set_rank_directly(self.engine, uid, "leader")
        return ids

    def _rewards_for(self, uid, period=None):
        with self.engine.connect() as conn:
            q = "SELECT reward_type, reward_period, amount_inr, status FROM rank_rewards WHERE user_id=:uid"
            params = {"uid": uid}
            if period:
                q += " AND reward_period=:p"
                params["p"] = period
            rows = conn.execute(text(q), params).fetchall()
        return [dict(r._mapping) for r in rows]

    def _leader_rewards_total(self, period):
        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT COUNT(*) AS c, COALESCE(SUM(amount_inr), 0) AS total FROM rank_rewards
                WHERE reward_type=:t AND reward_period=:p
            """), {"t": REWARD_TYPE_LEADER_MONTHLY, "p": period}).fetchone()
        return row._mapping["c"], float(row._mapping["total"])

    # 1. Pool cap disabled preserves current behavior
    def test_cap_none_preserves_unlimited_behavior(self):
        ids = self._seed_leaders(15)
        p1, p2 = self._cap(None)
        with p1, p2:
            result = rank_service.evaluate_monthly_rewards(period="2026-09")
        count, total = self._leader_rewards_total("2026-09")
        self.assertEqual(count, 15)
        self.assertEqual(total, 15 * LEADER_MONTHLY_REWARD_MIN_INR)
        self.assertEqual(result["leader_pool"]["budget_exhausted_leader_count"], 0)
        self.assertIsNone(result["leader_pool"]["leader_pool_cap_inr"])

    def test_cap_zero_also_preserves_unlimited_behavior(self):
        self._seed_leaders(5)
        p1, p2 = self._cap(0)
        with p1, p2:
            rank_service.evaluate_monthly_rewards(period="2026-09")
        count, _ = self._leader_rewards_total("2026-09")
        self.assertEqual(count, 5)

    # 2. Total allocated never exceeds the cap (with a non-round leader count)
    def test_total_allocated_never_exceeds_cap(self):
        self._seed_leaders(37)
        p1, p2 = self._cap(10000)
        with p1, p2:
            rank_service.evaluate_monthly_rewards(period="2026-09")
        count, total = self._leader_rewards_total("2026-09")
        self.assertLessEqual(total, 10000)
        self.assertEqual(count, 10)  # 10 x 1000 = 10000, the 11th would exceed

    # 3. Exact boundary
    def test_exact_boundary_cap_10000_reward_1000_rewards_exactly_10(self):
        self._seed_leaders(15)
        p1, p2 = self._cap(10000)
        with p1, p2:
            result = rank_service.evaluate_monthly_rewards(period="2026-09")
        count, total = self._leader_rewards_total("2026-09")
        self.assertEqual(count, 10)
        self.assertEqual(total, 10000)
        self.assertEqual(result["leader_pool"]["rewarded_leader_count"], 10)
        self.assertEqual(result["leader_pool"]["budget_exhausted_leader_count"], 5)

    # 4. Insufficient remaining budget for one more full reward
    def test_insufficient_remaining_budget_leaves_500_unused_not_partial(self):
        self._seed_leaders(15)
        p1, p2 = self._cap(10500)
        with p1, p2:
            result = rank_service.evaluate_monthly_rewards(period="2026-09")
        count, total = self._leader_rewards_total("2026-09")
        self.assertEqual(count, 10, "the 11th Leader's ₹1000 would exceed the ₹10500 cap")
        self.assertEqual(total, 10000)
        self.assertEqual(result["leader_pool"]["leader_pool_remaining_inr"], 500)
        # 5. No partial reward was ever created for the 11th+ Leaders.
        with self.engine.connect() as conn:
            amounts = conn.execute(text(
                "SELECT DISTINCT amount_inr FROM rank_rewards WHERE reward_type=:t AND reward_period='2026-09'"
            ), {"t": REWARD_TYPE_LEADER_MONTHLY}).fetchall()
        self.assertEqual([float(a._mapping["amount_inr"]) for a in amounts], [float(LEADER_MONTHLY_REWARD_MIN_INR)])

    # 6. Budget-exhausted eligible Leaders receive NO reward row at all
    def test_budget_exhausted_leaders_get_no_reward_row(self):
        ids = self._seed_leaders(12)
        p1, p2 = self._cap(10000)
        with p1, p2:
            rank_service.evaluate_monthly_rewards(period="2026-09")
        rewarded_ids = {uid for uid in ids if self._rewards_for(uid, "2026-09")}
        exhausted_ids = set(ids) - rewarded_ids
        self.assertEqual(len(rewarded_ids), 10)
        self.assertEqual(len(exhausted_ids), 2)
        for uid in exhausted_ids:
            self.assertEqual(self._rewards_for(uid, "2026-09"), [], "budget-exhausted Leader must have zero reward rows")

    # 7. Repeated evaluation remains idempotent under a cap
    def test_repeated_evaluation_under_cap_is_idempotent(self):
        self._seed_leaders(20)
        p1, p2 = self._cap(10000)
        with p1, p2:
            for _ in range(4):
                result = rank_service.evaluate_monthly_rewards(period="2026-09")
        count, total = self._leader_rewards_total("2026-09")
        self.assertEqual(count, 10)
        self.assertEqual(total, 10000)
        self.assertEqual(result["leader_pool"]["leader_pool_allocated_inr"], 10000)

    # 8. Different month can create a new reward (budget resets per period)
    def test_different_month_gets_a_fresh_budget(self):
        ids = self._seed_leaders(15)
        p1, p2 = self._cap(10000)
        with p1, p2:
            rank_service.evaluate_monthly_rewards(period="2026-09")
            rank_service.evaluate_monthly_rewards(period="2026-10")
        count_sep, total_sep = self._leader_rewards_total("2026-09")
        count_oct, total_oct = self._leader_rewards_total("2026-10")
        self.assertEqual((count_sep, total_sep), (10, 10000))
        self.assertEqual((count_oct, total_oct), (10, 10000))
        # Each period is an independent budget: a Leader rewarded in
        # September gets a SEPARATE, distinct row for October too (the
        # September reward does not block or get reused for October).
        rewarded_in_sep = [uid for uid in ids if self._rewards_for(uid, "2026-09")][0]
        sep_rows = self._rewards_for(rewarded_in_sep, "2026-09")
        oct_rows = self._rewards_for(rewarded_in_sep, "2026-10")
        self.assertTrue(sep_rows)
        self.assertTrue(oct_rows)
        self.assertNotEqual(sep_rows[0]["reward_period"], oct_rows[0]["reward_period"])

    # 9. Admin statistics correctly report allocated/remaining/eligible/rewarded/exhausted
    def test_admin_leader_pool_status_reports_all_fields_correctly(self):
        self._seed_leaders(15)
        p1, p2 = self._cap(10000)
        with p1, p2:
            rank_service.evaluate_monthly_rewards(period="2026-09")
            status = rank_service.get_leader_pool_status(period="2026-09")
        self.assertEqual(status["leader_pool_cap_inr"], 10000)
        self.assertEqual(status["leader_pool_allocated_inr"], 10000)
        self.assertEqual(status["leader_pool_remaining_inr"], 0)
        self.assertEqual(status["eligible_leader_count"], 15)
        self.assertEqual(status["rewarded_leader_count"], 10)
        self.assertEqual(status["budget_exhausted_leader_count"], 5)

    def test_get_leader_pool_status_does_not_write_anything(self):
        """A read-only status check (e.g. an admin loading the Overview
        page) must not itself create reward rows."""
        self._seed_leaders(5)
        p1, p2 = self._cap(10000)
        with p1, p2:
            rank_service.get_leader_pool_status(period="2026-09")
        count, _ = self._leader_rewards_total("2026-09")
        self.assertEqual(count, 0, "a status read must never allocate the pool")

    # 10. FINANCIAL INVARIANT — the core proof this fix exists for.
    def test_financial_invariant_leader_total_never_exceeds_configured_cap(self):
        for cap, n_leaders in [(10000, 37), (1000, 50), (999, 10), (1, 5), (100000, 250)]:
            with self.subTest(cap=cap, n_leaders=n_leaders):
                engine = _make_engine()
                with patch.object(rank_service, "get_db_connection", side_effect=engine.connect):
                    for uid in range(1, n_leaders + 1):
                        _set_rank_directly(engine, uid, "leader")
                    p1, p2 = (
                        patch.object(rank_config, "LEADER_MONTHLY_REWARD_POOL_CAP_INR", cap),
                        patch.object(rank_service, "LEADER_MONTHLY_REWARD_POOL_CAP_INR", cap),
                    )
                    with p1, p2:
                        rank_service.evaluate_monthly_rewards(period="2026-09")
                    with engine.connect() as conn:
                        total = conn.execute(text("""
                            SELECT COALESCE(SUM(amount_inr), 0) FROM rank_rewards
                            WHERE reward_type=:t AND reward_period='2026-09'
                        """), {"t": REWARD_TYPE_LEADER_MONTHLY}).scalar()
                self.assertLessEqual(float(total), float(cap),
                                      f"total Leader rewards ({total}) exceeded cap ({cap})")

    def test_financial_invariant_ranger_total_never_exceeds_revenue_pool(self):
        """Preserved from the Ranger design (unchanged by this fix) —
        re-verified here alongside the new Leader invariant per the task's
        'also preserve' requirement."""
        engine = _make_engine()
        with patch.object(rank_service, "get_db_connection", side_effect=engine.connect):
            for uid in range(1, 8):
                _set_rank_directly(engine, uid, "ranger")
            today = date.today().strftime("%Y-%m")
            _seed_payment(engine, 99, 37000, status="verified", created_at=today + "-12")
            with patch.object(rank_config, "RANGER_REWARD_POOL_PERCENTAGE", 15), \
                 patch.object(rank_config, "RANGER_MONTHLY_CAP_INR", 1_000_000), \
                 patch.object(rank_service, "RANGER_REWARD_POOL_PERCENTAGE", 15), \
                 patch.object(rank_service, "RANGER_MONTHLY_CAP_INR", 1_000_000):
                rank_service.evaluate_monthly_rewards(period=today)
            with engine.connect() as conn:
                total = conn.execute(text("""
                    SELECT COALESCE(SUM(amount_inr), 0) FROM rank_rewards WHERE reward_type=:t
                """), {"t": REWARD_TYPE_RANGER_MONTHLY}).scalar()
        expected_pool = 37000 * 0.15
        self.assertLessEqual(float(total), expected_pool + 0.1)  # +epsilon for per-user rounding (<=0.005/ranger)


if __name__ == "__main__":
    unittest.main()
