"""Reviews dashboard API — owner-facing review list, stats, and replies."""
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text

from database.init_db import get_db_connection

reviews_api_bp = Blueprint("reviews_api", __name__, url_prefix="/api/reviews")


def _ensure_reply_columns(conn):
    """Idempotent schema guard for older Render DBs."""
    conn.execute(text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS owner_reply TEXT"))
    conn.execute(text("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS replied_at TIMESTAMP"))
    conn.commit()


@reviews_api_bp.route("/list", methods=["GET"])
@jwt_required()
def list_reviews():
    """Fetch reviews for listings owned by the current user."""
    user_id = get_jwt_identity()
    rating_filter = request.args.get("rating", type=int)
    search = (request.args.get("q") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    try:
        conn = get_db_connection()
        _ensure_reply_columns(conn)

        clauses = ["l.user_id = :uid"]
        params = {"uid": user_id}

        if rating_filter and 1 <= rating_filter <= 5:
            clauses.append("r.rating = :rating")
            params["rating"] = rating_filter

        if search:
            clauses.append(
                "(COALESCE(r.review, '') ILIKE :q OR COALESCE(u.name, '') ILIKE :q "
                "OR COALESCE(l.business_name, '') ILIKE :q OR COALESCE(r.user_phone, '') ILIKE :q)"
            )
            params["q"] = f"%{search}%"

        if date_from:
            clauses.append("DATE(r.created_at) >= :date_from")
            params["date_from"] = date_from

        if date_to:
            clauses.append("DATE(r.created_at) <= :date_to")
            params["date_to"] = date_to

        where_sql = " AND ".join(clauses)
        rows = conn.execute(
            text(f"""
                SELECT
                    r.id,
                    r.listing_id,
                    r.user_phone,
                    COALESCE(u.name, r.user_phone, 'Anonymous') AS reviewer_name,
                    r.rating,
                    r.review,
                    r.owner_reply,
                    r.replied_at,
                    r.created_at,
                    l.business_name
                FROM reviews r
                JOIN listings l ON l.id = r.listing_id
                LEFT JOIN users u ON u.phone = r.user_phone
                WHERE {where_sql}
                ORDER BY r.created_at DESC
                LIMIT 200
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
                "user_phone": m["user_phone"],
                "rating": int(m["rating"] or 0),
                "review": m["review"] or "",
                "owner_reply": m["owner_reply"],
                "replied_at": replied.isoformat() if replied else None,
                "created_at": created.isoformat() if created else None,
            })

        return jsonify({"success": True, "reviews": reviews, "count": len(reviews)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@reviews_api_bp.route("/stats", methods=["GET"])
@jwt_required()
def review_stats():
    """Average rating and 1–5 star distribution for the owner's listings."""
    user_id = get_jwt_identity()
    try:
        conn = get_db_connection()
        _ensure_reply_columns(conn)

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
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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

    try:
        conn = get_db_connection()
        _ensure_reply_columns(conn)

        owned = conn.execute(
            text("""
                SELECT r.id
                FROM reviews r
                JOIN listings l ON l.id = r.listing_id
                WHERE r.id = :rid AND l.user_id = :uid
            """),
            {"rid": review_id, "uid": user_id},
        ).fetchone()

        if not owned:
            return jsonify({"success": False, "error": "Review not found or not owned by you"}), 404

        now = datetime.utcnow()
        conn.execute(
            text("""
                UPDATE reviews
                SET owner_reply = :reply, replied_at = :replied_at
                WHERE id = :rid
            """),
            {"reply": reply_text, "replied_at": now, "rid": review_id},
        )
        conn.commit()

        return jsonify({
            "success": True,
            "review_id": review_id,
            "owner_reply": reply_text,
            "replied_at": now.isoformat() + "Z",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
