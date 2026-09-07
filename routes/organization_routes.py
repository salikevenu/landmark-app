"""Business Power V2 — organization management API.

Every route re-derives identity from the JWT (get_jwt_identity()) and
authorizes fresh against the DB via services/organization_authz.py.
Nothing here trusts a JWT role/organization claim, a client-supplied
organization/business ID, or a client-supplied phone number as proof of
identity.
"""
import logging

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt

from services.organization_service import (
    list_organizations_for_user,
    get_organization_detail,
    invite_member,
    accept_invitation,
    list_members,
    change_member_role,
    remove_member,
    create_organization_business,
    list_organization_businesses,
    update_organization_business,
    delete_organization_business,
)

logger = logging.getLogger(__name__)
organization_bp = Blueprint("organization", __name__)


@organization_bp.route("", methods=["GET"])
@jwt_required()
def list_my_organizations():
    user_id = get_jwt_identity()
    page = request.args.get("page", 1, type=int) or 1
    limit = request.args.get("limit", 50, type=int) or 50
    result = list_organizations_for_user(user_id, page=page, limit=limit)
    return jsonify(result)


@organization_bp.route("/<int:organization_id>", methods=["GET"])
@jwt_required()
def organization_detail(organization_id):
    user_id = get_jwt_identity()
    detail = get_organization_detail(user_id, organization_id)
    if detail is None:
        return jsonify({"error": "Not found or unauthorized"}), 404
    return jsonify(detail)


@organization_bp.route("/<int:organization_id>/members/invite", methods=["POST"])
@jwt_required()
def invite_organization_member(organization_id):
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    result = invite_member(
        user_id, organization_id, data.get("phone"), data.get("role"),
        ip_address=request.remote_addr,
    )
    if result.get("error"):
        return jsonify(result), result.get("_http") or 400
    return jsonify(result), 201


@organization_bp.route("/<int:organization_id>/members/invitations/<token>/accept", methods=["POST"])
@jwt_required()
def accept_organization_invitation(organization_id, token):
    user_id = get_jwt_identity()
    result = accept_invitation(user_id, organization_id, token)
    if result.get("error"):
        return jsonify(result), result.get("_http") or 400
    return jsonify(result)


@organization_bp.route("/<int:organization_id>/members", methods=["GET"])
@jwt_required()
def list_organization_members(organization_id):
    user_id = get_jwt_identity()
    page = request.args.get("page", 1, type=int) or 1
    limit = request.args.get("limit", 50, type=int) or 50
    result = list_members(user_id, organization_id, page=page, limit=limit)
    if result is None:
        return jsonify({"error": "Not found or unauthorized"}), 404
    return jsonify(result)


@organization_bp.route("/<int:organization_id>/members/<int:member_id>", methods=["PUT"])
@jwt_required()
def update_organization_member(organization_id, member_id):
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    result = change_member_role(
        user_id, organization_id, member_id, data.get("role"),
        ip_address=request.remote_addr,
    )
    if result.get("error"):
        return jsonify(result), result.get("_http") or 400
    return jsonify(result)


@organization_bp.route("/<int:organization_id>/members/<int:member_id>", methods=["DELETE"])
@jwt_required()
def delete_organization_member(organization_id, member_id):
    user_id = get_jwt_identity()
    result = remove_member(user_id, organization_id, member_id, ip_address=request.remote_addr)
    if result.get("error"):
        return jsonify(result), result.get("_http") or 400
    return jsonify(result)


@organization_bp.route("/<int:organization_id>/businesses", methods=["GET"])
@jwt_required()
def list_org_businesses(organization_id):
    user_id = get_jwt_identity()
    page = request.args.get("page", 1, type=int) or 1
    limit = request.args.get("limit", 50, type=int) or 50
    result = list_organization_businesses(user_id, organization_id, page=page, limit=limit)
    if result is None:
        return jsonify({"error": "Not found or unauthorized"}), 404
    return jsonify(result)


@organization_bp.route("/<int:organization_id>/businesses", methods=["POST"])
@jwt_required()
def create_org_business(organization_id):
    user_id = get_jwt_identity()
    claims = get_jwt()
    result = create_organization_business(
        user_id, organization_id, request.form.to_dict(), user_phone=claims.get("phone"),
    )
    if result.get("error"):
        return jsonify(result), result.get("_http") or 400
    return jsonify(result), 201


@organization_bp.route("/<int:organization_id>/businesses/<int:listing_id>", methods=["PUT"])
@jwt_required()
def update_org_business(organization_id, listing_id):
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or request.form.to_dict()
    result = update_organization_business(
        user_id, organization_id, listing_id, data, ip_address=request.remote_addr,
    )
    if result.get("error"):
        return jsonify(result), result.get("_http") or 400
    return jsonify(result)


@organization_bp.route("/<int:organization_id>/businesses/<int:listing_id>", methods=["DELETE"])
@jwt_required()
def delete_org_business(organization_id, listing_id):
    user_id = get_jwt_identity()
    result = delete_organization_business(
        user_id, organization_id, listing_id, ip_address=request.remote_addr,
    )
    if result.get("error"):
        return jsonify(result), result.get("_http") or 400
    return jsonify(result)
