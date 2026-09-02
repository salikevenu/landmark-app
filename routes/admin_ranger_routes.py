"""Admin-only Ranger Network APIs.

Reuses the existing admin authorization pattern verbatim (imported, not
reimplemented): JWT `role` claim check + a live DB re-check via
services.authz.db_user_is_admin, exactly as routes/admin_routes.py already
does for every other /api/admin/* route. No second admin auth mechanism.

This is the ONLY place in the codebase that may return another user's
hierarchy/rank/reward data — every route here is admin_required.
"""
from flask import Blueprint, jsonify, request, send_file, render_template
import io

from routes.admin_routes import admin_required, get_admin_info
from services.admin_service import log_admin_action
from services.rank_service import (
    get_ranger_overview,
    get_ranger_network,
    get_ranger_user_detail,
    export_ranger_csv,
    recompute_all_ranks,
    evaluate_monthly_rewards,
    get_leader_pool_status,
    get_member_pool_status,
    get_guide_pool_status,
    get_ranger_pool_status,
    approve_reward,
    reject_reward,
    mark_reward_paid,
)
from config.rank_config import (
    RANK_LABELS,
    RANK_BADGES,
    RANK_REQUIREMENTS,
    REWARD_TYPE_LABELS,
    LEADER_MONTHLY_REWARD_MIN_INR,
    LEADER_MONTHLY_REWARD_MAX_INR,
    LEADER_MONTHLY_REWARD_POOL_CAP_INR,
    MEMBER_REWARD_POOL_PERCENTAGE,
    MEMBER_MONTHLY_CAP_INR,
    GUIDE_REWARD_POOL_PERCENTAGE,
    GUIDE_MONTHLY_CAP_INR,
    RANGER_REWARD_POOL_PERCENTAGE,
    RANGER_MONTHLY_CAP_INR,
)

admin_ranger_bp = Blueprint("admin_ranger", __name__)


@admin_ranger_bp.route("/admin/ranger", methods=["GET"])
@admin_required
def admin_ranger_page():
    return render_template("admin/admin_ranger.html")


@admin_ranger_bp.route("/api/admin/ranger/config", methods=["GET"])
@admin_required
def api_ranger_config():
    """Rank labels/badges/requirements for the admin UI — read-only mirror
    of config/rank_config.py, never editable through this endpoint."""
    return jsonify({
        "labels": RANK_LABELS,
        "badges": RANK_BADGES,
        "requirements": RANK_REQUIREMENTS,
        "reward_type_labels": REWARD_TYPE_LABELS,
        "reward_policy": {
            "leader_monthly_reward_min_inr": LEADER_MONTHLY_REWARD_MIN_INR,
            "leader_monthly_reward_max_inr": LEADER_MONTHLY_REWARD_MAX_INR,
            "leader_monthly_reward_pool_cap_inr": LEADER_MONTHLY_REWARD_POOL_CAP_INR,
            "member_reward_pool_percentage": MEMBER_REWARD_POOL_PERCENTAGE,
            "member_monthly_cap_inr": MEMBER_MONTHLY_CAP_INR,
            "guide_reward_pool_percentage": GUIDE_REWARD_POOL_PERCENTAGE,
            "guide_monthly_cap_inr": GUIDE_MONTHLY_CAP_INR,
            "ranger_reward_pool_percentage": RANGER_REWARD_POOL_PERCENTAGE,
            "ranger_monthly_cap_inr": RANGER_MONTHLY_CAP_INR,
        },
    })


@admin_ranger_bp.route("/api/admin/ranger/leader-pool", methods=["GET"])
@admin_required
def api_ranger_leader_pool():
    """Current period's Leader Growth Reward pool status: allocated,
    remaining, eligible/rewarded/budget-exhausted counts. Admin-only —
    never exposed through a normal user route."""
    period = request.args.get("period")
    return jsonify(get_leader_pool_status(period))


@admin_ranger_bp.route("/api/admin/ranger/member-pool", methods=["GET"])
@admin_required
def api_ranger_member_pool():
    """Current period's Member Growth Reward pool status. Admin-only —
    never exposed through a normal user route."""
    period = request.args.get("period")
    return jsonify(get_member_pool_status(period))


@admin_ranger_bp.route("/api/admin/ranger/guide-pool", methods=["GET"])
@admin_required
def api_ranger_guide_pool():
    """Current period's Guide Growth Reward pool status. Admin-only —
    never exposed through a normal user route."""
    period = request.args.get("period")
    return jsonify(get_guide_pool_status(period))


@admin_ranger_bp.route("/api/admin/ranger/ranger-pool", methods=["GET"])
@admin_required
def api_ranger_ranger_pool():
    """Current period's Ranger Growth Reward pool status (read-only
    reporting only — does not alter Ranger reward calculation). Admin-only
    — never exposed through a normal user route."""
    period = request.args.get("period")
    return jsonify(get_ranger_pool_status(period))


@admin_ranger_bp.route("/api/admin/ranger/overview", methods=["GET"])
@admin_required
def api_ranger_overview():
    return jsonify(get_ranger_overview())


@admin_ranger_bp.route("/api/admin/ranger/network", methods=["GET"])
@admin_required
def api_ranger_network():
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 50))
    search = request.args.get("search", "")
    rank_filter = request.args.get("rank", "")
    status_filter = request.args.get("status", "")
    subscription_filter = request.args.get("subscription", "")
    result = get_ranger_network(page, limit, search, rank_filter, status_filter, subscription_filter)
    return jsonify(result)


@admin_ranger_bp.route("/api/admin/ranger/users/<int:user_id>", methods=["GET"])
@admin_required
def api_ranger_user_detail(user_id):
    detail = get_ranger_user_detail(user_id)
    if not detail:
        return jsonify({"error": "User not found"}), 404
    return jsonify(detail)


@admin_ranger_bp.route("/api/admin/ranger/export.csv", methods=["GET"])
@admin_required
def api_ranger_export():
    csv_data = export_ranger_csv()
    return send_file(
        io.BytesIO(csv_data.encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="ranger_network.csv",
    )


@admin_ranger_bp.route("/api/admin/ranger/recompute", methods=["POST"])
@admin_required
def api_ranger_recompute():
    """Manual on-demand recompute + this period's monthly reward evaluation
    (same two functions the nightly cron calls), useful for admins who
    don't want to wait for the schedule."""
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    rank_result = recompute_all_ranks()
    reward_result = evaluate_monthly_rewards()
    log_admin_action(
        admin_id, admin_phone, "ranger_recompute", "rank_system", "all",
        details=(
            f"processed={rank_result.get('users_processed')} promotions={rank_result.get('promotions')} "
            f"reward_period={reward_result.get('period')} rewards_created={reward_result.get('rewards_created')}"
        ),
        ip_address=ip,
    )
    return jsonify({"success": True, "rank": rank_result, "monthly_rewards": reward_result})


def _reward_action(reward_id, action_fn, action_name):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    updated = action_fn(reward_id, admin_id)
    if not updated:
        return jsonify({"error": "Reward not found or not in a valid state for this action"}), 409
    log_admin_action(admin_id, admin_phone, action_name, "rank_reward", str(reward_id),
                      details=f"{action_name} reward {reward_id}", ip_address=ip)
    return jsonify({"success": True, "reward": updated})


@admin_ranger_bp.route("/api/admin/ranger/rewards/<int:reward_id>/approve", methods=["POST"])
@admin_required
def api_ranger_reward_approve(reward_id):
    return _reward_action(reward_id, approve_reward, "approve_rank_reward")


@admin_ranger_bp.route("/api/admin/ranger/rewards/<int:reward_id>/reject", methods=["POST"])
@admin_required
def api_ranger_reward_reject(reward_id):
    return _reward_action(reward_id, reject_reward, "reject_rank_reward")


@admin_ranger_bp.route("/api/admin/ranger/rewards/<int:reward_id>/paid", methods=["POST"])
@admin_required
def api_ranger_reward_paid(reward_id):
    return _reward_action(reward_id, mark_reward_paid, "mark_rank_reward_paid")
