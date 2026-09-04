# routes/pos_routes.py
"""LANDMARK POS business identity.

A POS business is its own tenant concept, deliberately separate from the
marketplace `businesses` table (unused) and `listings` (marketplace
directory entries) — see the LANDMARK POS repo's DECISIONS.md. No
subscription/plan/limit checks here yet; that is future work.
"""
import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text

from database.init_db import get_db_connection

pos_bp = Blueprint("pos", __name__)
logger = logging.getLogger(__name__)


def _as_user_id(identity):
    try:
        uid = int(identity)
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def _business_payload(row):
    created_at = row["created_at"]
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": created_at.isoformat() if created_at else None,
    }


@pos_bp.route("/businesses", methods=["GET"])
@jwt_required()
def list_businesses():
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    conn = None
    try:
        conn = get_db_connection()
        rows = conn.execute(
            text("""
                SELECT id, name, created_at
                FROM pos_businesses
                WHERE owner_user_id = :uid
                ORDER BY id
            """),
            {"uid": user_id},
        ).fetchall()
        businesses = [_business_payload(dict(row._mapping)) for row in rows]
        return jsonify({"businesses": businesses}), 200
    except Exception:
        logger.exception("list pos businesses failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/businesses", methods=["POST"])
@jwt_required()
def create_business():
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    data = request.get_json(silent=True) or {}
    raw_name = data.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return jsonify({"success": False, "error": "Business name is required"}), 400
    name = raw_name.strip()

    conn = None
    try:
        conn = get_db_connection()
        row = conn.execute(
            text("""
                INSERT INTO pos_businesses (owner_user_id, name, created_at)
                VALUES (:uid, :name, CURRENT_TIMESTAMP)
                RETURNING id, name, created_at
            """),
            {"uid": user_id, "name": name},
        ).fetchone()
        conn.commit()
        return jsonify({"business": _business_payload(dict(row._mapping))}), 201
    except Exception:
        logger.exception("create pos business failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
