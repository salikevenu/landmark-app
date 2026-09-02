"""User-facing Rank APIs. Every route is scoped to the caller's own JWT
identity — none of them accept or use a client-supplied user id, which
rules out IDOR by construction (there is no "other user" code path here).

Must never return hierarchy/tree/upline/downline data. See
routes/admin_ranger_routes.py for the admin-only equivalent.
"""
from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from services.rank_service import get_own_rank_summary, get_own_achievements, get_own_rewards
from config.rank_config import RANK_LABELS, RANK_BADGES

rank_bp = Blueprint("rank", __name__)


@rank_bp.route("/api/rank/me", methods=["GET"])
@jwt_required()
def rank_me():
    user_id = get_jwt_identity()
    summary = get_own_rank_summary(user_id)
    summary["rank_label"] = RANK_LABELS.get(summary["rank"], summary["rank"])
    summary["rank_badge"] = RANK_BADGES.get(summary["rank"], "")
    if summary.get("next_rank"):
        summary["next_rank_label"] = RANK_LABELS.get(summary["next_rank"], summary["next_rank"])
    return jsonify({"success": True, "data": summary})


@rank_bp.route("/api/rank/achievements", methods=["GET"])
@jwt_required()
def rank_achievements():
    user_id = get_jwt_identity()
    items = get_own_achievements(user_id)
    for item in items:
        item["previous_rank_label"] = RANK_LABELS.get(item["previous_rank"], item["previous_rank"])
        item["new_rank_label"] = RANK_LABELS.get(item["new_rank"], item["new_rank"])
    return jsonify({"success": True, "data": items})


@rank_bp.route("/api/rank/rewards", methods=["GET"])
@jwt_required()
def rank_rewards():
    user_id = get_jwt_identity()
    items = get_own_rewards(user_id)
    return jsonify({"success": True, "data": items})
