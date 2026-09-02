# config/rank_config.py
"""Centralized LANDMARK Rank system configuration.

Single source of truth for rank order, qualification thresholds, and
monthly reward policy. Nothing else in the codebase should hardcode a
threshold number, rank name, or reward amount — import from here.

Counting rule: CUMULATIVE-AT-OR-ABOVE. A Guide in someone's downline also
counts toward that person's "qualified_members" requirement (a Guide has
already met every Member-level requirement and more), a Leader counts
toward "qualified_members" AND "qualified_guides", etc.

REWARD MODEL (finalized business decision):
Rewards are NOT a one-time rank-achievement payout. They are MONTHLY
GROWTH REWARDS — a Leader or Ranger must be CURRENTLY qualified during a
given calendar-month evaluation period to become eligible for that
period's reward. Losing qualification stops future months; it never
retroactively removes a reward already created for a past period. See
services.rank_service.evaluate_monthly_rewards().

Use "Monthly Growth Reward" / "Leader Growth Reward" / "Ranger Growth
Reward" in any user-facing text. Never "salary" or "employment" — these
are growth incentives, not compensation for work performed.
"""

UNRANKED = "unranked"
MEMBER = "member"
GUIDE = "guide"
LEADER = "leader"
RANGER = "ranger"

# Ranks eligible for a monthly growth reward. Member/Guide are not.
MONTHLY_REWARD_RANKS = (LEADER, RANGER)

# Order matters: index position = tier. UNRANKED is not a qualifying rank.
RANK_ORDER = [UNRANKED, MEMBER, GUIDE, LEADER, RANGER]

# Ranks that count toward a team-size requirement (i.e. "at least a Member").
QUALIFYING_RANKS = [MEMBER, GUIDE, LEADER, RANGER]

RANK_LABELS = {
    UNRANKED: "Unranked",
    MEMBER: "Member",
    GUIDE: "Guide",
    LEADER: "Leader",
    RANGER: "Ranger",
}

RANK_BADGES = {
    UNRANKED: "",
    MEMBER: "\U0001F949",   # 🥉
    GUIDE: "\U0001F948",    # 🥈
    LEADER: "\U0001F947",   # 🥇
    RANGER: "\U0001F3C6",   # 🏆
}

# ---------------------------------------------------------------------
# QUALIFICATION THRESHOLDS
# ---------------------------------------------------------------------
# Leader and Ranger values below are FINALIZED business policy.
# Member and Guide values are still PROVISIONAL PLACEHOLDERS — they are
# NOT finalized business policy. Do not treat them as approved numbers;
# they exist only so the qualification system has a complete, working
# configuration. Update only this dict when real Member/Guide numbers are
# approved — nothing else in the codebase hardcodes a threshold.
RANK_REQUIREMENTS = {
    MEMBER: {  # PROVISIONAL — not yet finalized business policy.
        "verified_users": 1,
        "active_subscribers": 1,
    },
    GUIDE: {  # PROVISIONAL — not yet finalized business policy.
        "verified_users": 1,
        "active_subscribers": 1,
        "qualified_members": 5,
    },
    LEADER: {  # FINALIZED
        "verified_users": 500,
        "active_subscribers": 100,
        "qualified_members": 30,
        "qualified_guides": 10,
    },
    RANGER: {  # FINALIZED
        "verified_users": 2000,
        "active_subscribers": 400,
        "qualified_members": 100,
        "qualified_guides": 30,
        "qualified_leaders": 10,
    },
}

# ---------------------------------------------------------------------
# MONTHLY GROWTH REWARD POLICY
# ---------------------------------------------------------------------
REWARD_TYPE_LEADER_MONTHLY = "leader_monthly"
REWARD_TYPE_RANGER_MONTHLY = "ranger_monthly"

REWARD_TYPE_LABELS = {
    REWARD_TYPE_LEADER_MONTHLY: "Leader Growth Reward",
    REWARD_TYPE_RANGER_MONTHLY: "Ranger Growth Reward",
}

REWARD_TYPE_FOR_RANK = {
    LEADER: REWARD_TYPE_LEADER_MONTHLY,
    RANGER: REWARD_TYPE_RANGER_MONTHLY,
}

# Leader Growth Reward: a bounded range, NOT an automatic ₹2,000.
# Current policy (see services.rank_service.evaluate_monthly_rewards):
# every qualified Leader-month is granted the MINIMUM by default. The
# MAXIMUM is the configured ceiling a future scaling policy or manual
# admin adjustment may use — it is never granted automatically.
LEADER_MONTHLY_REWARD_MIN_INR = 1000
LEADER_MONTHLY_REWARD_MAX_INR = 2000

# OPTIONAL global monthly budget cap across ALL qualifying Leaders combined
# (a financial safety valve, not a business decision — Finance has NOT
# finalized a production budget figure for this).
#   - None or 0 = unlimited: every qualifying Leader gets the reward,
#     exactly the behavior before this cap existed.
#   - A positive number = the maximum total Leader Growth Reward INR
#     allocated for a single period. Once allocated reaches the cap,
#     remaining qualifying Leaders for that period receive NO reward row
#     at all (never a partial or ₹0 row) — they are reported as
#     budget-exhausted instead. See services.rank_service.evaluate_monthly_rewards.
LEADER_MONTHLY_REWARD_POOL_CAP_INR = None

# Ranger Growth Reward: REVENUE-BACKED, never a fixed amount. LANDMARK
# never owes an unconditional fixed sum — the pool is a percentage of
# actual period revenue, split evenly among that period's qualifying
# Rangers, and hard-capped per person.
#
# Both values below are PLACEHOLDER / OFF (0) — this is a deliberate safe
# default, not a business decision. At 0% / ₹0 cap, evaluate_monthly_rewards()
# computes an amount of 0 for every Ranger and creates NO reward row at all
# (see services.rank_service — a 0 amount is skipped, not recorded as a
# pending ₹0 reward). Finance/Admin must set real values here before Ranger
# monthly rewards become active.
RANGER_REWARD_POOL_PERCENTAGE = 0      # e.g. 2 would mean 2% of period revenue
RANGER_MONTHLY_CAP_INR = 0             # per-Ranger hard ceiling once a pool % is set

REWARD_STATUSES = ("pending", "approved", "paid", "rejected")

# Recursive-CTE depth safety cap for downline traversal (defensive; referred_by
# is immutable/set-once so a cycle should not be possible, but this bounds a
# pathological/corrupted chain rather than looping forever).
MAX_DOWNLINE_DEPTH = 50


def milestone_key_for_rank(rank):
    """Key for a one-time achievement/badge record (rank_achievements).
    Unrelated to money — see REWARD_TYPE_* for the monthly reward ledger."""
    return f"reach_{rank}"


def next_rank(current_rank):
    """Rank immediately above current_rank, or None if already at the top."""
    try:
        idx = RANK_ORDER.index(current_rank)
    except ValueError:
        idx = 0
    if idx + 1 >= len(RANK_ORDER):
        return None
    return RANK_ORDER[idx + 1]


def requirements_for(rank):
    return RANK_REQUIREMENTS.get(rank, {})


def rank_index(rank):
    try:
        return RANK_ORDER.index(rank)
    except ValueError:
        return 0


def highest_qualifying_rank(metrics):
    """Given a metrics dict (verified_users, active_subscribers,
    qualified_members, qualified_guides, qualified_leaders), return the
    highest rank whose requirements are all satisfied."""
    for rank in reversed(RANK_ORDER):
        reqs = RANK_REQUIREMENTS.get(rank)
        if not reqs:
            continue
        if all(metrics.get(key, 0) >= threshold for key, threshold in reqs.items()):
            return rank
    return UNRANKED


def reward_type_for_rank(rank):
    return REWARD_TYPE_FOR_RANK.get(rank)


def current_reward_period():
    """Current calendar-month period key, e.g. '2026-09'."""
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m")
