"""Add monthly-reward support to rank_rewards (Leader/Ranger Growth Rewards).

Business model refinement: rewards are no longer one-time rank-achievement
payments, they are MONTHLY GROWTH REWARDS keyed by
(user_id, reward_type, reward_period) — e.g. ('leader_monthly', '2026-09').

Purely additive:
- Adds nullable reward_type / reward_period columns to the existing
  rank_rewards table (does not touch milestone_key, rank, amount_inr,
  status, or any existing row).
- Adds a new UNIQUE index on (user_id, reward_type, reward_period) for
  monthly-reward idempotency, alongside (not replacing) the pre-existing
  UNIQUE(user_id, milestone_key) constraint.

Idempotent — safe to run multiple times. Does not touch users,
wallet_transactions, or referral_transactions.
"""
from sqlalchemy import text
from database.init_db import get_db_connection
import logging

logger = logging.getLogger(__name__)


def migrate_add_rank_monthly_rewards():
    conn = get_db_connection()
    try:
        conn.execute(text("ALTER TABLE rank_rewards ADD COLUMN IF NOT EXISTS reward_type TEXT"))
        conn.execute(text("ALTER TABLE rank_rewards ADD COLUMN IF NOT EXISTS reward_period TEXT"))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_rank_rewards_user_type_period
            ON rank_rewards (user_id, reward_type, reward_period)
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rank_rewards_period ON rank_rewards(reward_period)"))
        conn.commit()
        logger.info("rank_rewards monthly-reward columns/index ensured")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate_add_rank_monthly_rewards()
