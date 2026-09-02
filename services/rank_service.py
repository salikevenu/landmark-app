"""LANDMARK Rank system: qualification, batch recomputation, and reads.

Reads users.referred_by (never writes it) and the canonical
is_subscription_active() check (services/subscription_access.py) for
"active subscriber" — no duplicate subscription-truth logic here.

Rank is CACHED/MATERIALIZED in user_rank_stats, not derived live, because
qualification is recursive-interdependent (a Leader's rank depends on their
downline's own already-computed ranks). recompute_all_ranks() fetches the
downline edges and personal metrics from the DB exactly once (one
recursive CTE, no per-user DB round-trips), then iterates the rank
assignment to a fixpoint in memory so a single call fully propagates rank
up an arbitrarily deep chain. Safe to run repeatedly (idempotent) and
intended to run nightly via the same cron pattern as the existing Saturday
payout job, or on demand via the admin "recompute now" action.

Team-size requirements use CUMULATIVE-AT-OR-ABOVE counting: a Guide in a
user's downline also counts toward that user's "qualified_members"
requirement, a Leader counts toward both, etc.

REWARDS: rank_achievements (badge/history) is recorded once, on
promotion, and is purely informational — no money. Money is handled
separately by evaluate_monthly_rewards(): a Leader/Ranger MONTHLY GROWTH
REWARD, evaluated against CURRENT qualification each period, never a
one-time rank-achievement payout. rank_rewards is a ledger entirely
separate from wallet_transactions / referral_transactions and never
touches either; nothing here creates an automatic real-money payout —
every reward is created 'pending' and requires admin approval.
"""
from collections import defaultdict
import logging

from sqlalchemy import text

from database.init_db import get_db_connection
from services.subscription_access import is_subscription_active
from config.rank_config import (
    RANK_ORDER,
    RANK_REQUIREMENTS,
    MAX_DOWNLINE_DEPTH,
    MONTHLY_REWARD_RANKS,
    LEADER,
    RANGER,
    LEADER_MONTHLY_REWARD_MIN_INR,
    LEADER_MONTHLY_REWARD_POOL_CAP_INR,
    RANGER_REWARD_POOL_PERCENTAGE,
    RANGER_MONTHLY_CAP_INR,
    REWARD_TYPE_LEADER_MONTHLY,
    REWARD_TYPE_RANGER_MONTHLY,
    UNRANKED,
    highest_qualifying_rank,
    milestone_key_for_rank,
    next_rank,
    rank_index,
    requirements_for,
    reward_type_for_rank,
    current_reward_period,
)

logger = logging.getLogger(__name__)

_DOWNLINE_CTE = """
    WITH RECURSIVE downline AS (
        SELECT id AS ancestor_id, id AS descendant_id, 0 AS depth
        FROM users
        UNION ALL
        SELECT d.ancestor_id, u.id, d.depth + 1
        FROM downline d
        JOIN users u ON u.referred_by = d.descendant_id
        WHERE d.depth < 50
    )
    SELECT ancestor_id, descendant_id FROM downline WHERE depth > 0
"""


def _is_valid_account(user_row):
    """Exclude banned/inactive accounts from every rank-related count."""
    return user_row.get("is_active") == 1 and user_row.get("is_blocked") != 1


def _team_counts_for(pair_rows, users_by_id, rank_of):
    """One pass over the (ancestor, descendant) pairs, counting descendants
    at each qualifying tier using rank_of (a {user_id: rank} snapshot)."""
    team_member_counts = defaultdict(int)
    team_guide_counts = defaultdict(int)
    team_leader_counts = defaultdict(int)
    for ancestor_id, descendant_id in pair_rows:
        du = users_by_id.get(descendant_id)
        if not du or not _is_valid_account(du):
            continue
        drank = rank_of.get(descendant_id, UNRANKED)
        if drank in ("member", "guide", "leader", "ranger"):
            team_member_counts[ancestor_id] += 1
        if drank in ("guide", "leader", "ranger"):
            team_guide_counts[ancestor_id] += 1
        if drank in ("leader", "ranger"):
            team_leader_counts[ancestor_id] += 1
    return team_member_counts, team_guide_counts, team_leader_counts


def recompute_all_ranks():
    """Recompute every user's rank + qualification counts from scratch.

    Team-size requirements are recursive-interdependent (a Leader's rank
    depends on their downline's OWN already-computed ranks), so this
    iterates to a fixpoint in memory before writing anything: each pass
    recomputes every user's rank from the PREVIOUS pass's ranks, starting
    from "unranked" for everyone, until nobody's rank changes (or a safety
    cap tied to the downline-depth cap is hit). This means a single call
    fully propagates rank up an arbitrarily deep new chain — it does not
    depend on, or take multiple days across, separate cron runs to
    converge. The (ancestor, descendant) pairs and personal metrics are
    each fetched from the DB exactly once; only the in-memory rank
    assignment loops.

    Single-connection, sequential (not designed for concurrent callers —
    same operational model as the existing Saturday payout job). Safe to
    re-run: results are deterministic given the same underlying data, and
    reward issuance is guarded by a UNIQUE(user_id, milestone_key)
    constraint, so re-running never creates a duplicate reward.
    """
    conn = get_db_connection()
    try:
        user_rows = conn.execute(text("""
            SELECT id, referred_by, is_active, is_blocked, plan, subscription_expiry
            FROM users
        """)).fetchall()
        users_by_id = {r._mapping["id"]: dict(r._mapping) for r in user_rows}

        prior_rows = conn.execute(text("SELECT user_id, rank FROM user_rank_stats")).fetchall()
        prior_persisted_rank = {r._mapping["user_id"]: r._mapping["rank"] for r in prior_rows}

        # --- Personal (direct-referral) metrics: one pass, no recursion, ---
        # --- independent of rank so computed once outside the fixpoint loop.
        verified_counts = defaultdict(int)
        active_sub_counts = defaultdict(int)
        for u in users_by_id.values():
            parent = u.get("referred_by")
            if parent is None or not _is_valid_account(u):
                continue
            verified_counts[parent] += 1
            if is_subscription_active(u):
                active_sub_counts[parent] += 1

        # --- Downline edges: fetched once, reused across fixpoint passes ---
        pair_rows = [(r._mapping["ancestor_id"], r._mapping["descendant_id"])
                     for r in conn.execute(text(_DOWNLINE_CTE)).fetchall()]

        # --- Fixpoint iteration over in-memory rank assignment ---
        rank_of = {uid: UNRANKED for uid in users_by_id}
        for _pass in range(MAX_DOWNLINE_DEPTH + 1):
            team_member_counts, team_guide_counts, team_leader_counts = _team_counts_for(
                pair_rows, users_by_id, rank_of
            )
            next_rank_of = {}
            for uid, u in users_by_id.items():
                if not _is_valid_account(u):
                    next_rank_of[uid] = UNRANKED
                    continue
                metrics = {
                    "verified_users": verified_counts.get(uid, 0),
                    "active_subscribers": active_sub_counts.get(uid, 0),
                    "qualified_members": team_member_counts.get(uid, 0),
                    "qualified_guides": team_guide_counts.get(uid, 0),
                    "qualified_leaders": team_leader_counts.get(uid, 0),
                }
                next_rank_of[uid] = highest_qualifying_rank(metrics)
            if next_rank_of == rank_of:
                break
            rank_of = next_rank_of
        else:
            logger.warning("recompute_all_ranks: fixpoint not reached within %d passes", MAX_DOWNLINE_DEPTH + 1)

        # Final counts, from the converged rank_of snapshot.
        team_member_counts, team_guide_counts, team_leader_counts = _team_counts_for(
            pair_rows, users_by_id, rank_of
        )

        # --- Upsert stats + detect promotions vs the last PERSISTED rank ---
        promotions = []
        for uid, u in users_by_id.items():
            new_rank = rank_of[uid]

            conn.execute(text("""
                INSERT INTO user_rank_stats (
                    user_id, rank, verified_users_count, active_subscribers_count,
                    qualified_members_count, qualified_guides_count, qualified_leaders_count,
                    updated_at
                ) VALUES (
                    :uid, :rank, :vu, :asub, :qm, :qg, :ql, CURRENT_TIMESTAMP
                )
                ON CONFLICT (user_id) DO UPDATE SET
                    rank = EXCLUDED.rank,
                    verified_users_count = EXCLUDED.verified_users_count,
                    active_subscribers_count = EXCLUDED.active_subscribers_count,
                    qualified_members_count = EXCLUDED.qualified_members_count,
                    qualified_guides_count = EXCLUDED.qualified_guides_count,
                    qualified_leaders_count = EXCLUDED.qualified_leaders_count,
                    updated_at = CURRENT_TIMESTAMP
            """), {
                "uid": uid,
                "rank": new_rank,
                "vu": verified_counts.get(uid, 0),
                "asub": active_sub_counts.get(uid, 0),
                "qm": team_member_counts.get(uid, 0),
                "qg": team_guide_counts.get(uid, 0),
                "ql": team_leader_counts.get(uid, 0),
            })

            old_rank = prior_persisted_rank.get(uid, UNRANKED)
            if rank_index(new_rank) > rank_index(old_rank):
                promotions.append((uid, old_rank, new_rank))

        # --- Achievement history (append-only badge/history record only —
        # --- no money here; see evaluate_monthly_rewards() for rewards) ---
        for uid, old_rank, new_rank in promotions:
            milestone_key = milestone_key_for_rank(new_rank)
            conn.execute(text("""
                INSERT INTO rank_achievements (user_id, previous_rank, new_rank, milestone_key, achieved_at)
                VALUES (:uid, :old_rank, :new_rank, :milestone_key, CURRENT_TIMESTAMP)
            """), {"uid": uid, "old_rank": old_rank, "new_rank": new_rank, "milestone_key": milestone_key})

        conn.commit()
        return {"users_processed": len(users_by_id), "promotions": len(promotions)}
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


def _period_bounds(period):
    """('2026-09') -> (date(2026,9,1), date(2026,10,1)) exclusive end."""
    from datetime import date
    year, month = (int(p) for p in period.split("-"))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _period_revenue_inr(conn, period):
    """Reuses the exact revenue truth source as GET /api/admin/stats
    (routes/admin_routes.py): payments.status IN ('verified','activated'),
    Razorpay 'activated' rows stored in paise. Read-only SELECT against
    payments — never writes to it, never touches commission/payment logic."""
    start, end = _period_bounds(period)
    revenue = conn.execute(text("""
        SELECT COALESCE(SUM(
            CASE WHEN status = 'activated' THEN amount / 100.0 ELSE amount END
        ), 0)
        FROM payments
        WHERE status IN ('verified', 'activated')
          AND created_at >= :start AND created_at < :end
    """), {"start": start, "end": end}).scalar()
    return float(revenue or 0)


def _insert_monthly_reward(conn, uid, reward_type, period, rank, amount):
    # milestone_key kept populated (synthetic) so the pre-existing
    # UNIQUE(user_id, milestone_key) / NOT NULL constraint needs no change.
    milestone_key = f"{reward_type}_{period}"
    result = conn.execute(text("""
        INSERT INTO rank_rewards (user_id, milestone_key, reward_type, reward_period, rank, amount_inr, status)
        VALUES (:uid, :milestone_key, :reward_type, :period, :rank, :amount, 'pending')
        ON CONFLICT (user_id, reward_type, reward_period) DO NOTHING
    """), {
        "uid": uid, "milestone_key": milestone_key, "reward_type": reward_type,
        "period": period, "rank": rank, "amount": amount,
    })
    return bool(getattr(result, "rowcount", 0) and result.rowcount > 0)


def _leader_pool_report(conn, period, eligible_leader_ids):
    """Read-after-write snapshot of this period's Leader pool status,
    computed from persisted state (not fragile in-loop counters) so it is
    always consistent whether called mid-evaluation or from a separate
    read-only request (get_leader_pool_status)."""
    row = conn.execute(text("""
        SELECT COUNT(*) AS c, COALESCE(SUM(amount_inr), 0) AS total
        FROM rank_rewards WHERE reward_type = :t AND reward_period = :p
    """), {"t": REWARD_TYPE_LEADER_MONTHLY, "p": period}).fetchone()
    m = row._mapping
    rewarded_count = m["c"]
    allocated = float(m["total"] or 0)
    eligible_count = len(eligible_leader_ids)
    cap = LEADER_MONTHLY_REWARD_POOL_CAP_INR or None
    remaining = None
    exhausted_count = 0
    if cap:
        remaining = max(0.0, float(cap) - allocated)
        exhausted_count = max(0, eligible_count - rewarded_count)
    return {
        "leader_pool_cap_inr": cap,
        "leader_pool_allocated_inr": allocated,
        "leader_pool_remaining_inr": remaining,
        "eligible_leader_count": eligible_count,
        "rewarded_leader_count": rewarded_count,
        "budget_exhausted_leader_count": exhausted_count,
    }


def get_leader_pool_status(period=None):
    """Admin-only read: current Leader Growth Reward pool status for a
    period, without triggering any evaluation/writes. Callers (routes/
    admin_ranger_routes.py) must gate this behind admin_required."""
    period = period or current_reward_period()
    conn = get_db_connection()
    try:
        rows = conn.execute(
            text("SELECT user_id FROM user_rank_stats WHERE rank = :r"), {"r": LEADER}
        ).fetchall()
        eligible_ids = [r._mapping["user_id"] for r in rows]
        report = _leader_pool_report(conn, period, eligible_ids)
        report["period"] = period
        return report
    finally:
        try:
            conn.close()
        except Exception:
            pass


def evaluate_monthly_rewards(period=None):
    """Create this period's pending Leader/Ranger Growth Reward for every
    CURRENTLY qualifying user. Reads the cached user_rank_stats — callers
    should run recompute_all_ranks() first in the same cycle so rewards
    reflect current, not stale, qualification (both /internal/recompute-ranks
    and the admin "recompute now" action do this).

    Idempotent: UNIQUE(user_id, reward_type, reward_period) means calling
    this any number of times within the same period never creates a second
    reward row for a user already evaluated that period — including across
    a demotion-then-repromotion within the same month. A user who stops
    qualifying simply isn't included in future periods; past periods'
    rewards are untouched (rank achievement/loss is never retroactive).

    Leader Growth Reward: flat LEADER_MONTHLY_REWARD_MIN_INR per qualified
    Leader-month. LEADER_MONTHLY_REWARD_MAX_INR is a configured ceiling for
    a future scaling policy or manual admin adjustment — never auto-granted.
    If LEADER_MONTHLY_REWARD_POOL_CAP_INR is set (None/0 = unlimited, the
    default), total Leader allocation for the period is hard-capped: once
    the running total would exceed the cap, remaining qualifying Leaders
    get NO reward row at all for this period (never a partial/₹0 row) —
    they show up as budget_exhausted_leader_count in the returned
    leader_pool report instead. Financial invariant: total Leader Growth
    Reward allocated for a period never exceeds a configured cap.

    Ranger Growth Reward: REVENUE-BACKED, never a fixed amount. This
    period's revenue (same source as the admin dashboard's revenue stat) is
    multiplied by RANGER_REWARD_POOL_PERCENTAGE, split evenly across this
    period's qualifying Rangers, and hard-capped per person at
    RANGER_MONTHLY_CAP_INR. With the placeholder pool%/cap (0), the computed
    amount is always 0 and NO reward row is created at all — LANDMARK is
    never left with an unconditional obligation to pay a fixed amount.
    """
    period = period or current_reward_period()
    conn = get_db_connection()
    try:
        leader_rows = conn.execute(
            text("SELECT user_id FROM user_rank_stats WHERE rank = :r ORDER BY user_id"), {"r": LEADER}
        ).fetchall()
        leader_ids = [r._mapping["user_id"] for r in leader_rows]

        ranger_rows = conn.execute(
            text("SELECT user_id FROM user_rank_stats WHERE rank = :r"), {"r": RANGER}
        ).fetchall()
        ranger_ids = [r._mapping["user_id"] for r in ranger_rows]

        created = 0
        leader_pool_cap = LEADER_MONTHLY_REWARD_POOL_CAP_INR or None

        if LEADER_MONTHLY_REWARD_MIN_INR > 0:
            # Idempotent re-evaluation: whatever this period already
            # allocated (from an earlier run) counts against the cap too.
            leader_pool_allocated = 0.0
            if leader_pool_cap:
                already = conn.execute(text("""
                    SELECT COALESCE(SUM(amount_inr), 0) FROM rank_rewards
                    WHERE reward_type = :t AND reward_period = :p
                """), {"t": REWARD_TYPE_LEADER_MONTHLY, "p": period}).scalar()
                leader_pool_allocated = float(already or 0)

            for uid in leader_ids:
                if leader_pool_cap and (leader_pool_allocated + LEADER_MONTHLY_REWARD_MIN_INR) > leader_pool_cap:
                    # Budget exhausted: no row at all for this user this
                    # period — never a partial or ₹0 reward. They are
                    # reported via the eligible/rewarded delta, not a row.
                    continue
                if _insert_monthly_reward(conn, uid, REWARD_TYPE_LEADER_MONTHLY, period, LEADER,
                                           LEADER_MONTHLY_REWARD_MIN_INR):
                    created += 1
                    if leader_pool_cap:
                        leader_pool_allocated += LEADER_MONTHLY_REWARD_MIN_INR

        if ranger_ids and RANGER_REWARD_POOL_PERCENTAGE > 0 and RANGER_MONTHLY_CAP_INR > 0:
            period_revenue = _period_revenue_inr(conn, period)
            pool = period_revenue * (RANGER_REWARD_POOL_PERCENTAGE / 100.0)
            per_ranger = round(min(pool / len(ranger_ids), RANGER_MONTHLY_CAP_INR), 2)
            if per_ranger > 0:
                for uid in ranger_ids:
                    if _insert_monthly_reward(conn, uid, REWARD_TYPE_RANGER_MONTHLY, period, RANGER, per_ranger):
                        created += 1

        leader_pool = _leader_pool_report(conn, period, leader_ids)

        conn.commit()
        return {
            "period": period,
            "leaders_evaluated": len(leader_ids),
            "rangers_evaluated": len(ranger_ids),
            "rewards_created": created,
            "leader_pool": leader_pool,
        }
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


def get_own_rank_summary(user_id):
    """Everything the user's own /api/rank/me page needs. Self-scoped only
    — callers must pass the caller's own id (see routes/rank_routes.py)."""
    conn = get_db_connection()
    try:
        uid = int(user_id)
        row = conn.execute(text("""
            SELECT rank, verified_users_count, active_subscribers_count,
                   qualified_members_count, qualified_guides_count, qualified_leaders_count,
                   updated_at
            FROM user_rank_stats WHERE user_id = :uid
        """), {"uid": uid}).fetchone()

        if not row:
            current_rank = UNRANKED
            counts = {
                "verified_users": 0, "active_subscribers": 0,
                "qualified_members": 0, "qualified_guides": 0, "qualified_leaders": 0,
            }
            updated_at = None
            calculated = False
        else:
            m = row._mapping
            current_rank = m["rank"]
            counts = {
                "verified_users": m["verified_users_count"],
                "active_subscribers": m["active_subscribers_count"],
                "qualified_members": m["qualified_members_count"],
                "qualified_guides": m["qualified_guides_count"],
                "qualified_leaders": m["qualified_leaders_count"],
            }
            updated_at = m["updated_at"]
            calculated = True

        upcoming = next_rank(current_rank)
        remaining = {}
        progress_percent = 100 if upcoming is None else 0
        if upcoming:
            reqs = requirements_for(upcoming)
            parts = []
            for key, threshold in reqs.items():
                have = counts.get(key, 0)
                remaining[key] = max(0, threshold - have)
                parts.append(min(1.0, have / threshold) if threshold else 1.0)
            progress_percent = round((sum(parts) / len(parts)) * 100) if parts else 0

        return {
            "rank": current_rank,
            "next_rank": upcoming,
            "progress_percent": progress_percent,
            "metrics": counts,
            "remaining_for_next_rank": remaining,
            "stats_calculated": calculated,
            "stats_updated_at": updated_at,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_own_achievements(user_id):
    conn = get_db_connection()
    try:
        uid = int(user_id)
        rows = conn.execute(text("""
            SELECT id, previous_rank, new_rank, milestone_key, achieved_at
            FROM rank_achievements
            WHERE user_id = :uid
            ORDER BY achieved_at DESC
        """), {"uid": uid}).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_own_rewards(user_id):
    conn = get_db_connection()
    try:
        uid = int(user_id)
        rows = conn.execute(text("""
            SELECT id, reward_type, reward_period, rank, amount_inr, status, created_at, approved_at
            FROM rank_rewards
            WHERE user_id = :uid
            ORDER BY created_at DESC
        """), {"uid": uid}).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------
# ADMIN-ONLY reads/writes. Callers (routes/admin_ranger_routes.py) must
# gate every one of these behind admin_required — nothing here checks
# authorization itself, matching how services/admin_service.py works.
# ---------------------------------------------------------------------

def get_ranger_overview():
    """Platform-wide rank distribution + reward totals, from the cache."""
    conn = get_db_connection()
    try:
        rank_rows = conn.execute(text("SELECT rank, COUNT(*) AS c FROM user_rank_stats GROUP BY rank")).fetchall()
        by_rank = {r._mapping["rank"]: r._mapping["c"] for r in rank_rows}
        distribution = {rank: by_rank.get(rank, 0) for rank in RANK_ORDER}

        reward_rows = conn.execute(text("""
            SELECT status, COUNT(*) AS c, COALESCE(SUM(amount_inr), 0) AS total
            FROM rank_rewards GROUP BY status
        """)).fetchall()
        rewards_by_status = {
            r._mapping["status"]: {"count": r._mapping["c"], "amount_inr": float(r._mapping["total"])}
            for r in reward_rows
        }

        last_updated = conn.execute(text("SELECT MAX(updated_at) FROM user_rank_stats")).scalar()
        total_users = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()

        leader_ids = [
            r._mapping["user_id"] for r in
            conn.execute(text("SELECT user_id FROM user_rank_stats WHERE rank = :r"), {"r": LEADER}).fetchall()
        ]
        leader_pool = _leader_pool_report(conn, current_reward_period(), leader_ids)
        leader_pool["period"] = current_reward_period()

        return {
            "rank_distribution": distribution,
            "rewards_by_status": rewards_by_status,
            "total_users": total_users,
            "stats_last_updated": last_updated,
            "leader_pool": leader_pool,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_ranger_network(page=1, limit=50, search="", rank_filter="", status_filter="", subscription_filter=""):
    """Paginated admin user list with rank info. No city/location filter —
    the users table has no city column (only lat/lng), so that filter is
    not offered rather than faked."""
    conn = get_db_connection()
    try:
        page = max(1, int(page))
        limit = max(1, min(int(limit), 200))
        offset = (page - 1) * limit

        where = ["1=1"]
        params = {}
        if search:
            where.append("(u.phone LIKE :search OR u.name LIKE :search OR u.referral_code LIKE :search)")
            params["search"] = f"%{search}%"
        if rank_filter:
            where.append("COALESCE(urs.rank, 'unranked') = :rank_filter")
            params["rank_filter"] = rank_filter
        if status_filter == "active":
            where.append("u.is_blocked = 0 AND u.is_active = 1")
        elif status_filter == "inactive":
            where.append("(u.is_blocked = 1 OR u.is_active = 0)")
        where_sql = " AND ".join(where)

        count_sql = f"""
            SELECT COUNT(*) FROM users u
            LEFT JOIN user_rank_stats urs ON urs.user_id = u.id
            WHERE {where_sql}
        """
        total = conn.execute(text(count_sql), params).scalar()

        query = f"""
            SELECT u.id, u.phone, u.name, u.referral_code, u.referred_by,
                   u.is_active, u.is_blocked, u.plan, u.subscription_expiry, u.created_at,
                   COALESCE(urs.rank, 'unranked') AS rank,
                   COALESCE(urs.verified_users_count, 0) AS verified_users_count,
                   COALESCE(urs.active_subscribers_count, 0) AS active_subscribers_count,
                   COALESCE(urs.qualified_members_count, 0) AS qualified_members_count,
                   COALESCE(urs.qualified_guides_count, 0) AS qualified_guides_count,
                   COALESCE(urs.qualified_leaders_count, 0) AS qualified_leaders_count
            FROM users u
            LEFT JOIN user_rank_stats urs ON urs.user_id = u.id
            WHERE {where_sql}
            ORDER BY u.id DESC
            LIMIT :limit OFFSET :offset
        """
        params["limit"] = limit
        params["offset"] = offset
        rows = conn.execute(text(query), params).fetchall()
        users = [dict(r._mapping) for r in rows]

        # Subscription-status filter applied in Python on this page only
        # (bounded to `limit` rows — not a full-table scan), reusing the
        # canonical is_subscription_active() check rather than a new one.
        if subscription_filter in ("active", "inactive"):
            wanted_active = subscription_filter == "active"
            users = [u for u in users if is_subscription_active(u) == wanted_active]

        return {
            "users": users, "total": total, "page": page, "limit": limit,
            "pages": (total + limit - 1) // limit if limit else 0,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_ranger_user_detail(user_id):
    """One user's rank detail + direct parent/children (lazy one-level
    expand — callers fetch a child's own detail to expand further, so a
    full tree is never serialized in one response)."""
    conn = get_db_connection()
    try:
        uid = int(user_id)
        user = conn.execute(text("""
            SELECT u.id, u.phone, u.name, u.referral_code, u.referred_by, u.created_at,
                   u.is_active, u.is_blocked, u.plan, u.subscription_expiry,
                   COALESCE(urs.rank, 'unranked') AS rank,
                   COALESCE(urs.verified_users_count, 0) AS verified_users_count,
                   COALESCE(urs.active_subscribers_count, 0) AS active_subscribers_count,
                   COALESCE(urs.qualified_members_count, 0) AS qualified_members_count,
                   COALESCE(urs.qualified_guides_count, 0) AS qualified_guides_count,
                   COALESCE(urs.qualified_leaders_count, 0) AS qualified_leaders_count,
                   urs.updated_at AS stats_updated_at
            FROM users u
            LEFT JOIN user_rank_stats urs ON urs.user_id = u.id
            WHERE u.id = :uid
        """), {"uid": uid}).fetchone()
        if not user:
            return None
        user = dict(user._mapping)

        parent = None
        if user.get("referred_by"):
            prow = conn.execute(text("""
                SELECT u.id, u.phone, u.name, COALESCE(urs.rank, 'unranked') AS rank
                FROM users u LEFT JOIN user_rank_stats urs ON urs.user_id = u.id
                WHERE u.id = :pid
            """), {"pid": user["referred_by"]}).fetchone()
            parent = dict(prow._mapping) if prow else None

        children_rows = conn.execute(text("""
            SELECT u.id, u.phone, u.name, u.created_at, u.is_active, u.is_blocked,
                   COALESCE(urs.rank, 'unranked') AS rank
            FROM users u LEFT JOIN user_rank_stats urs ON urs.user_id = u.id
            WHERE u.referred_by = :uid
            ORDER BY u.created_at DESC
            LIMIT 200
        """), {"uid": uid}).fetchall()
        children = [dict(r._mapping) for r in children_rows]

        achievements = get_own_achievements(uid)
        rewards = get_own_rewards(uid)

        return {
            "user": user, "parent": parent, "children": children,
            "children_count": len(children),
            "achievements": achievements, "rewards": rewards,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def export_ranger_csv():
    """CSV export matching services/admin_service.py's csv.writer convention."""
    import csv
    import io

    conn = get_db_connection()
    try:
        rows = conn.execute(text("""
            SELECT u.id, u.phone, u.name, COALESCE(urs.rank, 'unranked') AS rank,
                   COALESCE(urs.verified_users_count, 0) AS verified_users_count,
                   COALESCE(urs.active_subscribers_count, 0) AS active_subscribers_count,
                   COALESCE(urs.qualified_members_count, 0) AS qualified_members_count,
                   COALESCE(urs.qualified_guides_count, 0) AS qualified_guides_count,
                   COALESCE(urs.qualified_leaders_count, 0) AS qualified_leaders_count,
                   urs.updated_at
            FROM users u
            LEFT JOIN user_rank_stats urs ON urs.user_id = u.id
            ORDER BY u.id
        """)).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "ID", "Phone", "Name", "Rank", "Verified Users", "Active Subscribers",
            "Qualified Members", "Qualified Guides", "Qualified Leaders", "Stats Updated At",
        ])
        for r in rows:
            m = r._mapping
            writer.writerow([
                m["id"], m["phone"], m["name"], m["rank"],
                m["verified_users_count"], m["active_subscribers_count"],
                m["qualified_members_count"], m["qualified_guides_count"], m["qualified_leaders_count"],
                m["updated_at"],
            ])
        return output.getvalue()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _reward_status_transition(reward_id, from_statuses, to_status, admin_id):
    """CAS-style status update, mirroring admin_service.py's withdrawal
    approve/reject/paid pattern. Returns the updated row's dict or None."""
    conn = get_db_connection()
    try:
        rid = int(reward_id)
        placeholders = ", ".join(f"'{s}'" for s in from_statuses)  # fixed internal set, not user input
        if to_status == "approved":
            result = conn.execute(text(f"""
                UPDATE rank_rewards
                SET status = 'approved', approved_by = :admin_id, approved_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :rid AND status IN ({placeholders})
            """), {"rid": rid, "admin_id": admin_id})
        else:
            result = conn.execute(text(f"""
                UPDATE rank_rewards
                SET status = :to_status, updated_at = CURRENT_TIMESTAMP
                WHERE id = :rid AND status IN ({placeholders})
            """), {"rid": rid, "to_status": to_status})
        if result.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        row = conn.execute(text("SELECT * FROM rank_rewards WHERE id = :rid"), {"rid": rid}).fetchone()
        return dict(row._mapping) if row else None
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


def approve_reward(reward_id, admin_id):
    return _reward_status_transition(reward_id, ["pending"], "approved", admin_id)


def reject_reward(reward_id, admin_id):
    return _reward_status_transition(reward_id, ["pending", "approved"], "rejected", admin_id)


def mark_reward_paid(reward_id, admin_id):
    return _reward_status_transition(reward_id, ["approved"], "paid", admin_id)
