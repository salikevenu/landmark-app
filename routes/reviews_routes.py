"""Reviews dashboard API — owner-facing review list, stats, and replies."""
from datetime import datetime
import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text

from database.init_db import get_db_connection

reviews_api_bp = Blueprint("reviews_api", __name__, url_prefix="/api/reviews")
logger = logging.getLogger(__name__)

PAGE_SIZE = 10
_reply_columns_ready = False


def _ensure_reply_columns_once(conn):
    """Do not ALTER production from a request path. Columns are created by migrations."""
    global _reply_columns_ready
    _reply_columns_ready = True
    return


def _list_filters(user_id):
    rating_filter = request.args.get("rating", type=int)
    search = (request.args.get("q") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    clauses = ["l.user_id = :uid"]
    params = {"uid": user_id}

    if rating_filter and 1 <= rating_filter <= 5:
        clauses.append("r.rating = :rating")
        params["rating"] = rating_filter

    if search:
        clauses.append(
            "(COALESCE(r.review, '') ILIKE :q OR COALESCE(u.name, '') ILIKE :q "
            "OR COALESCE(l.business_name, '') ILIKE :q)"
        )
        params["q"] = f"%{search}%"

    # Range predicates keep idx_reviews_created usable (avoid DATE(created_at))
    if date_from:
        clauses.append("r.created_at >= CAST(:date_from AS date)")
        params["date_from"] = date_from

    if date_to:
        clauses.append("r.created_at < (CAST(:date_to AS date) + INTERVAL '1 day')")
        params["date_to"] = date_to

    return " AND ".join(clauses), params


@reviews_api_bp.route("/list", methods=["GET"])
@jwt_required()
def list_reviews():
    """Fetch a page of reviews for listings owned by the current user."""
    user_id = get_jwt_identity()
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    offset = (page - 1) * PAGE_SIZE

    conn = None
    try:
        conn = get_db_connection()
        _ensure_reply_columns_once(conn)

        where_sql, params = _list_filters(user_id)
        params["limit"] = PAGE_SIZE
        params["offset"] = offset

        total = conn.execute(
            text(f"""
                SELECT COUNT(*)::int AS cnt
                FROM reviews r
                JOIN listings l ON l.id = r.listing_id
                LEFT JOIN users u ON u.id = r.user_id
                WHERE {where_sql}
            """),
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        ).scalar() or 0

        rows = conn.execute(
            text(f"""
                SELECT
                    r.id,
                    r.listing_id,
                    COALESCE(u.name, 'Anonymous') AS reviewer_name,
                    r.rating,
                    r.review,
                    r.owner_reply,
                    r.replied_at,
                    r.created_at,
                    l.business_name
                FROM reviews r
                JOIN listings l ON l.id = r.listing_id
                LEFT JOIN users u ON u.id = r.user_id
                WHERE {where_sql}
                ORDER BY r.created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            params,
        ).fetchall()

        reviews = []
        for row in rows:
            m = row._mapping
            created = m["created_at"]
            replied = m["replied_at"]
            reviews.append({
                "id": m["id"],
                "listing_id": m["listing_id"],
                "business_name": m["business_name"] or "Listing",
                "reviewer_name": m["reviewer_name"] or "Anonymous",
                "rating": int(m["rating"] or 0),
                "review": m["review"] or "",
                "owner_reply": m["owner_reply"],
                "replied_at": replied.isoformat() if replied else None,
                "created_at": created.isoformat() if created else None,
            })

        return jsonify({
            "success": True,
            "reviews": reviews,
            "count": len(reviews),
            "page": page,
            "page_size": PAGE_SIZE,
            "total": int(total),
            "has_more": (page * PAGE_SIZE) < int(total),
        })
    except Exception:
        logger.exception("list_reviews error")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            conn.close()


@reviews_api_bp.route("/stats", methods=["GET"])
@jwt_required()
def review_stats():
    """Average rating and 1–5 star distribution for the owner's listings."""
    user_id = get_jwt_identity()
    conn = None
    try:
        conn = get_db_connection()
        _ensure_reply_columns_once(conn)

        totals = conn.execute(
            text("""
                SELECT
                    COUNT(*)::int AS total_reviews,
                    COALESCE(ROUND(AVG(r.rating)::numeric, 2), 0) AS avg_rating,
                    COUNT(*) FILTER (WHERE r.owner_reply IS NOT NULL AND r.owner_reply <> '')::int AS replied_count,
                    COUNT(*) FILTER (WHERE r.owner_reply IS NULL OR r.owner_reply = '')::int AS unreplied_count
                FROM reviews r
                JOIN listings l ON l.id = r.listing_id
                WHERE l.user_id = :uid
            """),
            {"uid": user_id},
        ).fetchone()

        dist_rows = conn.execute(
            text("""
                SELECT r.rating, COUNT(*)::int AS cnt
                FROM reviews r
                JOIN listings l ON l.id = r.listing_id
                WHERE l.user_id = :uid
                GROUP BY r.rating
            """),
            {"uid": user_id},
        ).fetchall()

        distribution = {str(i): 0 for i in range(1, 6)}
        for row in dist_rows:
            rating = int(row._mapping["rating"] or 0)
            if 1 <= rating <= 5:
                distribution[str(rating)] = row._mapping["cnt"]

        tm = totals._mapping
        total = int(tm["total_reviews"] or 0)
        return jsonify({
            "success": True,
            "stats": {
                "total_reviews": total,
                "avg_rating": float(tm["avg_rating"] or 0),
                "replied_count": int(tm["replied_count"] or 0),
                "unreplied_count": int(tm["unreplied_count"] or 0),
                "distribution": distribution,
            },
        })
    except Exception:
        logger.exception("list_reviews error")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            conn.close()


@reviews_api_bp.route("/reply", methods=["POST"])
@jwt_required()
def reply_to_review():
    """Owner reply to a review on one of their listings."""
    user_id = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    review_id = data.get("review_id")
    reply_text = (data.get("reply") or "").strip()

    if not review_id:
        return jsonify({"success": False, "error": "review_id is required"}), 400
    if not reply_text:
        return jsonify({"success": False, "error": "reply text is required"}), 400
    if len(reply_text) > 2000:
        return jsonify({"success": False, "error": "reply must be under 2000 characters"}), 400

    conn = None
    try:
        conn = get_db_connection()
        _ensure_reply_columns_once(conn)
        now = datetime.utcnow()

        result = conn.execute(
            text("""
                UPDATE reviews r
                SET owner_reply = :reply, replied_at = :replied_at
                FROM listings l
                WHERE r.listing_id = l.id
                  AND r.id = :rid
                  AND l.user_id = :uid
            """),
            {"reply": reply_text, "replied_at": now, "rid": review_id, "uid": user_id},
        )
        if result.rowcount != 1:
            return jsonify({"success": False, "error": "Review not found or not owned by you"}), 404
        conn.commit()

        return jsonify({
            "success": True,
            "review_id": review_id,
            "owner_reply": reply_text,
            "replied_at": now.isoformat() + "Z",
        })
    except Exception:
        logger.exception("list_reviews error")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            conn.close()
