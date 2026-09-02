"""Add the Rank system tables (user_rank_stats, rank_achievements, rank_rewards).

Purely additive — no existing table is altered. Idempotent (CREATE TABLE /
INDEX IF NOT EXISTS), safe to run multiple times. Does not touch
users.referred_by, wallet_transactions, or referral_transactions.

This is the same schema init_db.py now creates automatically on boot; this
script exists (matching the project's established migration convention —
see add_pending_referrals.py, add_wallet_withdrawal_safety.py) to apply it
to an already-running deployment ahead of the next full deploy.

Reversal: since nothing else references these three tables, they can be
dropped safely if ever needed (DROP TABLE rank_rewards, rank_achievements,
user_rank_stats). No existing migration in this project ships a scripted
"down" path either — this follows that same forward-only convention.
"""
from sqlalchemy import text
from database.init_db import get_db_connection
import logging

logger = logging.getLogger(__name__)


def migrate_add_rank_system():
    conn = get_db_connection()
    try:
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)"))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_rank_stats (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                rank TEXT NOT NULL DEFAULT 'unranked',
                verified_users_count INTEGER NOT NULL DEFAULT 0,
                active_subscribers_count INTEGER NOT NULL DEFAULT 0,
                qualified_members_count INTEGER NOT NULL DEFAULT 0,
                qualified_guides_count INTEGER NOT NULL DEFAULT 0,
                qualified_leaders_count INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_rank_stats_rank ON user_rank_stats(rank)"))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS rank_achievements (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                previous_rank TEXT NOT NULL,
                new_rank TEXT NOT NULL,
                milestone_key TEXT,
                achieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rank_achievements_user ON rank_achievements(user_id)"))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS rank_rewards (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                milestone_key TEXT NOT NULL,
                rank TEXT NOT NULL,
                amount_inr NUMERIC(12,2) NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                approved_by INTEGER REFERENCES users(id),
                approved_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_rank_rewards_user_milestone UNIQUE (user_id, milestone_key),
                CONSTRAINT chk_rank_rewards_status CHECK (status IN ('pending','approved','paid','rejected'))
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rank_rewards_user ON rank_rewards(user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rank_rewards_status ON rank_rewards(status)"))

        conn.commit()
        logger.info("Rank system tables ensured (user_rank_stats, rank_achievements, rank_rewards)")
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
    migrate_add_rank_system()
