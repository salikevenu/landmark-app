# routes/pos_routes.py
"""LANDMARK POS business identity.

A POS business is its own tenant concept, deliberately separate from the
marketplace `businesses` table (unused) and `listings` (marketplace
directory entries) — see the LANDMARK POS repo's DECISIONS.md.

Every business-scoped route enforces POS entitlement (see
_resolve_pos_entitlement below) in addition to the existing
ownership check — LANDMARK POS remains a separate product from the
marketplace, so this never touches users.plan/subscription_expiry or the
marketplace PAID_PLANS; the only intentional overlap is Business Power,
reused unmodified via services.subscription_access.
"""
import hashlib
import hmac
import logging
import secrets

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from config.payment_config import get_razorpay_key_pair, get_razorpay_webhook_secret
from database.init_db import get_db_connection
from extensions import get_razorpay_client
from routes.auth_routes import clean_phone, validate_phone
from services.pos_billing_activation import activate_paid_billing_order
from services.subscription_access import (
    POS_PLANS,
    is_active_business_power_owner,
    resolve_pos_entitlement,
)

pos_bp = Blueprint("pos", __name__)
logger = logging.getLogger(__name__)


def _as_user_id(identity):
    try:
        uid = int(identity)
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def _resolve_pos_entitlement(conn, user_id):
    """The one place POS entitlement is resolved for a request. Wraps the
    Phase A pure function (services.subscription_access.resolve_pos_entitlement)
    with the actual DB reads it needs -- never re-implemented per route.

    Checks Business Power first and skips the pos_subscriptions query
    entirely when it applies (one fewer query for the common
    already-entitled-via-Business-Power path — see Phase B's performance
    requirement), matching resolve_pos_entitlement's own resolution order.

    Beyond {"has_access", "plan", "business_limit"} (the fields every
    authorization call site already relies on, untouched), also attaches
    "source" and "expires_at" -- additive only, so none of the 7 existing
    ownership-then-entitlement call sites are affected. These two exist
    purely for GET /api/pos/subscription (Phase E-A) to distinguish *why*
    access is granted without a second entitlement implementation:
      - source: "business_power" | "pos_subscription" | "none"
      - expires_at: raw value (datetime, str, or None) -- for Business
        Power this is the owner's own users.subscription_expiry (already
        read above to perform the check; surfaced as-is, not invented,
        since that's genuinely what bounds this grant); for a real POS
        subscription, pos_subscriptions.expires_at; otherwise None.
    """
    user_row = conn.execute(
        text("SELECT plan, subscription_expiry FROM users WHERE id = :uid"),
        {"uid": user_id},
    ).fetchone()
    user_dict = dict(user_row._mapping) if user_row else {}

    if is_active_business_power_owner(user_dict):
        entitlement = resolve_pos_entitlement(user_dict, None)
        entitlement["source"] = "business_power"
        entitlement["expires_at"] = user_dict.get("subscription_expiry")
        return entitlement

    sub_row = conn.execute(
        text("""
            SELECT pos_plan, status, expires_at FROM pos_subscriptions
            WHERE owner_user_id = :uid
        """),
        {"uid": user_id},
    ).fetchone()
    sub_dict = dict(sub_row._mapping) if sub_row else None
    entitlement = resolve_pos_entitlement(user_dict, sub_dict)
    entitlement["source"] = "pos_subscription" if entitlement["has_access"] else "none"
    entitlement["expires_at"] = (
        sub_dict.get("expires_at") if (sub_dict and entitlement["has_access"]) else None
    )
    return entitlement


def _pos_access_denied_response():
    return jsonify({"success": False, "error": "POS subscription required"}), 403


def _compute_pos_access_map(businesses, entitlement):
    """Given ONE already-resolved entitlement (never re-resolved per
    business — no N+1) and the owner's businesses, decide which are
    currently usable.

    Not entitled at all -> every business is False. Unlimited
    (business_limit is None -- Business Power) -> every business is True.
    Otherwise, the Phase B downgrade rule: the oldest `business_limit`
    businesses (created_at ASC, id ASC as the deterministic tie-breaker)
    remain entitled; the rest are False. Never mutates or deletes
    anything -- this is a pure read-time computation.
    """
    if not entitlement["has_access"]:
        return {b["id"]: False for b in businesses}

    limit = entitlement["business_limit"]
    if limit is None:
        return {b["id"]: True for b in businesses}

    oldest_first = sorted(businesses, key=lambda b: (b["created_at"], b["id"]))
    entitled_ids = {b["id"] for b in oldest_first[:limit]}
    return {b["id"]: (b["id"] in entitled_ids) for b in businesses}


def _business_payload(row, pos_access=None):
    created_at = row["created_at"]
    payload = {
        "id": row["id"],
        "name": row["name"],
        "created_at": created_at.isoformat() if created_at else None,
    }
    if pos_access is not None:
        payload["pos_access"] = pos_access
    return payload


def _product_payload(row):
    created_at = row["created_at"]
    return {
        "id": row["id"],
        "name": row["name"],
        "price": row["price"],
        "is_active": bool(row["is_active"]),
        "created_at": created_at.isoformat() if created_at else None,
    }


def _inventory_payload(row):
    return {
        "product_id": row["product_id"],
        "product_name": row["product_name"],
        "quantity": row["quantity"],
    }


# Postgres INTEGER is 32-bit signed; keep well under that so a
# quantity*price multiplication (or a running total) can never wrap.
_MAX_SALE_QUANTITY = 1_000_000
_MAX_SALE_AMOUNT = 2_000_000_000


def _format_expiry(value):
    if value is None:
        return None
    # pos_subscriptions.expires_at is a real TIMESTAMP (-> datetime from
    # the driver); users.subscription_expiry is TEXT (already a plain
    # string) -- format only what actually needs it, matching every other
    # payload's `created_at.isoformat() if created_at else None` pattern.
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _subscription_payload(entitlement):
    """The one place GET /api/pos/subscription's response is shaped --
    from an already-resolved entitlement (see _resolve_pos_entitlement),
    never a second entitlement computation.

    business_limit=0 for the no-access case is a *display* choice made
    here only -- resolve_pos_entitlement() itself returns None for "not
    applicable" there, which stays unambiguous internally (None means
    unlimited only in the has_access=True/Business-Power case).
    """
    if not entitlement["has_access"]:
        return {
            "has_access": False,
            "source": "none",
            "plan": None,
            "business_limit": 0,
            "status": "inactive",
            "expires_at": None,
        }

    return {
        "has_access": True,
        "source": entitlement["source"],
        "plan": entitlement["plan"],
        "business_limit": entitlement["business_limit"],
        "status": "active",
        "expires_at": _format_expiry(entitlement.get("expires_at")),
    }


def _customer_payload(row):
    created_at = row["created_at"]
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "created_at": created_at.isoformat() if created_at else None,
    }


def _sale_payload(sale_row, item_rows):
    created_at = sale_row["created_at"]
    return {
        "id": sale_row["id"],
        "total_amount": sale_row["total_amount"],
        "created_at": created_at.isoformat() if created_at else None,
        "items": [
            {
                "product_id": row["product_id"],
                "product_name": row["product_name"],
                "unit_price": row["unit_price"],
                "quantity": row["quantity"],
                "line_total": row["line_total"],
            }
            for row in item_rows
        ],
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
        business_dicts = [dict(row._mapping) for row in rows]

        # Resolved once for the whole list, never per business — see
        # _compute_pos_access_map.
        entitlement = _resolve_pos_entitlement(conn, user_id)
        access_by_id = _compute_pos_access_map(business_dicts, entitlement)

        businesses = [
            _business_payload(b, pos_access=access_by_id[b["id"]]) for b in business_dicts
        ]
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

        entitlement = _resolve_pos_entitlement(conn, user_id)
        if not entitlement["has_access"]:
            return _pos_access_denied_response()

        limit = entitlement["business_limit"]
        if limit is not None:
            count_row = conn.execute(
                text("SELECT COUNT(*) AS c FROM pos_businesses WHERE owner_user_id = :uid"),
                {"uid": user_id},
            ).fetchone()
            if count_row._mapping["c"] >= limit:
                return jsonify({
                    "success": False,
                    "error": "You've reached your plan's POS business limit. Upgrade to add more.",
                }), 403

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


@pos_bp.route("/businesses/<int:business_id>/products", methods=["GET"])
@jwt_required()
def list_products(business_id):
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    conn = None
    try:
        conn = get_db_connection()

        owned = conn.execute(
            text("""
                SELECT id FROM pos_businesses
                WHERE id = :business_id AND owner_user_id = :uid
            """),
            {"business_id": business_id, "uid": user_id},
        ).fetchone()
        if owned is None:
            # Same response for "doesn't exist" and "not yours" — a 403
            # here would confirm to a caller that a given business_id
            # exists at all, even one they don't own.
            return jsonify({"success": False, "error": "Business not found"}), 404

        entitlement = _resolve_pos_entitlement(conn, user_id)
        if not entitlement["has_access"]:
            return _pos_access_denied_response()

        rows = conn.execute(
            text("""
                SELECT id, name, price, is_active, created_at
                FROM pos_products
                WHERE business_id = :business_id AND is_active = 1
                ORDER BY id
            """),
            {"business_id": business_id},
        ).fetchall()
        products = [_product_payload(dict(row._mapping)) for row in rows]
        return jsonify({"products": products}), 200
    except Exception:
        logger.exception("list pos products failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/businesses/<int:business_id>/inventory", methods=["GET"])
@jwt_required()
def list_inventory(business_id):
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    conn = None
    try:
        conn = get_db_connection()

        owned = conn.execute(
            text("""
                SELECT id FROM pos_businesses
                WHERE id = :business_id AND owner_user_id = :uid
            """),
            {"business_id": business_id, "uid": user_id},
        ).fetchone()
        if owned is None:
            return jsonify({"success": False, "error": "Business not found"}), 404

        entitlement = _resolve_pos_entitlement(conn, user_id)
        if not entitlement["has_access"]:
            return _pos_access_denied_response()

        rows = conn.execute(
            text("""
                SELECT p.id AS product_id, p.name AS product_name,
                       COALESCE(i.quantity, 0) AS quantity
                FROM pos_products p
                LEFT JOIN pos_inventory i ON i.product_id = p.id
                WHERE p.business_id = :business_id AND p.is_active = 1
                ORDER BY p.id
            """),
            {"business_id": business_id},
        ).fetchall()
        inventory = [_inventory_payload(dict(row._mapping)) for row in rows]
        return jsonify({"inventory": inventory}), 200
    except Exception:
        logger.exception("list pos inventory failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/businesses/<int:business_id>/sales", methods=["POST"])
@jwt_required()
def create_sale(business_id):
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    conn = None
    try:
        conn = get_db_connection()

        owned = conn.execute(
            text("""
                SELECT id FROM pos_businesses
                WHERE id = :business_id AND owner_user_id = :uid
            """),
            {"business_id": business_id, "uid": user_id},
        ).fetchone()
        if owned is None:
            return jsonify({"success": False, "error": "Business not found"}), 404

        entitlement = _resolve_pos_entitlement(conn, user_id)
        if not entitlement["has_access"]:
            return _pos_access_denied_response()

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Request body must be a JSON object"}), 400

        raw_items = data.get("items")
        if not isinstance(raw_items, list) or len(raw_items) == 0:
            return jsonify({"success": False, "error": "items must be a non-empty list"}), 400

        parsed_items = []
        seen_product_ids = set()
        for entry in raw_items:
            if not isinstance(entry, dict):
                return jsonify({"success": False, "error": "Each item must be an object"}), 400

            product_id = entry.get("product_id")
            quantity = entry.get("quantity")

            # bool is a subclass of int in Python — reject it explicitly so
            # {"product_id": true, ...} isn't silently accepted as 1.
            if isinstance(product_id, bool) or not isinstance(product_id, int):
                return jsonify({"success": False, "error": "product_id must be an integer"}), 400
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
                or quantity > _MAX_SALE_QUANTITY
            ):
                return jsonify({"success": False, "error": "quantity must be a positive integer"}), 400
            if product_id in seen_product_ids:
                return jsonify({"success": False, "error": "Duplicate product_id in items"}), 400
            seen_product_ids.add(product_id)

            parsed_items.append({"product_id": product_id, "quantity": quantity})

        line_items = []
        total_amount = 0
        for item in parsed_items:
            # Same business_id + is_active filter as list_products: a
            # product from another business, a nonexistent product, and
            # an inactive product all fail this lookup identically, so
            # the error response below can't be used to distinguish them.
            product_row = conn.execute(
                text("""
                    SELECT id, name, price FROM pos_products
                    WHERE id = :product_id AND business_id = :business_id AND is_active = 1
                """),
                {"product_id": item["product_id"], "business_id": business_id},
            ).fetchone()
            if product_row is None:
                return jsonify({"success": False, "error": "One or more products are invalid"}), 400

            product = dict(product_row._mapping)
            quantity = item["quantity"]
            unit_price = product["price"]
            line_total = unit_price * quantity
            if line_total > _MAX_SALE_AMOUNT or total_amount + line_total > _MAX_SALE_AMOUNT:
                return jsonify({"success": False, "error": "Sale total is too large"}), 400
            total_amount += line_total

            line_items.append({
                "product_id": product["id"],
                "product_name": product["name"],
                "unit_price": unit_price,
                "quantity": quantity,
                "line_total": line_total,
            })

        sale_row = conn.execute(
            text("""
                INSERT INTO pos_sales (business_id, total_amount, created_at)
                VALUES (:business_id, :total_amount, CURRENT_TIMESTAMP)
                RETURNING id, total_amount, created_at
            """),
            {"business_id": business_id, "total_amount": total_amount},
        ).fetchone()
        sale = dict(sale_row._mapping)

        for line in line_items:
            conn.execute(
                text("""
                    INSERT INTO pos_sale_items
                        (sale_id, product_id, product_name, unit_price, quantity, line_total)
                    VALUES (:sale_id, :product_id, :product_name, :unit_price, :quantity, :line_total)
                """),
                {"sale_id": sale["id"], **line},
            )

        conn.commit()
        return jsonify({"sale": _sale_payload(sale, line_items)}), 201
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("create pos sale failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/businesses/<int:business_id>/sales", methods=["GET"])
@jwt_required()
def list_sales(business_id):
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    conn = None
    try:
        conn = get_db_connection()

        owned = conn.execute(
            text("""
                SELECT id FROM pos_businesses
                WHERE id = :business_id AND owner_user_id = :uid
            """),
            {"business_id": business_id, "uid": user_id},
        ).fetchone()
        if owned is None:
            return jsonify({"success": False, "error": "Business not found"}), 404

        entitlement = _resolve_pos_entitlement(conn, user_id)
        if not entitlement["has_access"]:
            return _pos_access_denied_response()

        sale_rows = conn.execute(
            text("""
                SELECT id, total_amount, created_at FROM pos_sales
                WHERE business_id = :business_id
                ORDER BY id
            """),
            {"business_id": business_id},
        ).fetchall()

        sales = []
        for sale_row in sale_rows:
            sale = dict(sale_row._mapping)
            item_rows = conn.execute(
                text("""
                    SELECT product_id, product_name, unit_price, quantity, line_total
                    FROM pos_sale_items
                    WHERE sale_id = :sale_id
                    ORDER BY id
                """),
                {"sale_id": sale["id"]},
            ).fetchall()
            items = [dict(row._mapping) for row in item_rows]
            sales.append(_sale_payload(sale, items))

        return jsonify({"sales": sales}), 200
    except Exception:
        logger.exception("list pos sales failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/businesses/<int:business_id>/customers", methods=["GET"])
@jwt_required()
def list_customers(business_id):
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    conn = None
    try:
        conn = get_db_connection()

        owned = conn.execute(
            text("""
                SELECT id FROM pos_businesses
                WHERE id = :business_id AND owner_user_id = :uid
            """),
            {"business_id": business_id, "uid": user_id},
        ).fetchone()
        if owned is None:
            return jsonify({"success": False, "error": "Business not found"}), 404

        entitlement = _resolve_pos_entitlement(conn, user_id)
        if not entitlement["has_access"]:
            return _pos_access_denied_response()

        rows = conn.execute(
            text("""
                SELECT id, name, phone, created_at FROM pos_customers
                WHERE business_id = :business_id
                ORDER BY id
            """),
            {"business_id": business_id},
        ).fetchall()
        customers = [_customer_payload(dict(row._mapping)) for row in rows]
        return jsonify({"customers": customers}), 200
    except Exception:
        logger.exception("list pos customers failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/businesses/<int:business_id>/customers", methods=["POST"])
@jwt_required()
def create_customer(business_id):
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    conn = None
    try:
        conn = get_db_connection()

        owned = conn.execute(
            text("""
                SELECT id FROM pos_businesses
                WHERE id = :business_id AND owner_user_id = :uid
            """),
            {"business_id": business_id, "uid": user_id},
        ).fetchone()
        if owned is None:
            return jsonify({"success": False, "error": "Business not found"}), 404

        entitlement = _resolve_pos_entitlement(conn, user_id)
        if not entitlement["has_access"]:
            return _pos_access_denied_response()

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Request body must be a JSON object"}), 400

        raw_name = data.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return jsonify({"success": False, "error": "Customer name is required"}), 400
        name = raw_name.strip()

        raw_phone = data.get("phone")
        if not isinstance(raw_phone, str) or not raw_phone.strip():
            return jsonify({"success": False, "error": "Customer phone is required"}), 400

        phone = clean_phone(raw_phone)
        if not validate_phone(phone):
            return jsonify({"success": False, "error": "Enter a valid 10-digit mobile number"}), 400

        try:
            row = conn.execute(
                text("""
                    INSERT INTO pos_customers (business_id, name, phone, created_at)
                    VALUES (:business_id, :name, :phone, CURRENT_TIMESTAMP)
                    RETURNING id, name, phone, created_at
                """),
                {"business_id": business_id, "name": name, "phone": phone},
            ).fetchone()
            conn.commit()
        except IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({
                "success": False,
                "error": "A customer with this phone number already exists",
            }), 409

        return jsonify({"customer": _customer_payload(dict(row._mapping))}), 201
    except Exception:
        logger.exception("create pos customer failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/subscription", methods=["GET"])
@jwt_required()
def get_subscription():
    """The authenticated owner's current POS entitlement/subscription
    information -- read-only. No plan/status can ever be influenced by
    the client; this only reads what _resolve_pos_entitlement already
    derives server-side from users/pos_subscriptions."""
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    conn = None
    try:
        conn = get_db_connection()
        entitlement = _resolve_pos_entitlement(conn, user_id)
        return jsonify({"subscription": _subscription_payload(entitlement)}), 200
    except Exception:
        logger.exception("get pos subscription failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# =====================================================
# POS BILLING ORDERS (Phase G-A)
# =====================================================
# Razorpay order-creation foundation only. Creating an order here NEVER
# reads or writes pos_subscriptions/users -- entitlement is completely
# untouched by this route. The verified payment lifecycle (signature
# verification, webhooks, subscription activation) is out of scope for
# this phase and belongs to G-B/G-C.
#
# Policy for existing-subscription/Business-Power cases (task-mandated
# inspection, Phase G-A item 7): order creation is allowed uniformly for
# every case -- no subscription, active Starter re-requesting Starter,
# active Starter requesting Growth, active Growth requesting Growth,
# expired, suspended, and Business Power owners requesting starter/growth.
# This route deliberately never calls _resolve_pos_entitlement at all:
# there is nothing here that reads current entitlement to gate on, so it
# is structurally impossible for this endpoint to alter, downgrade, or
# even observe Business Power/subscription state. Whether a given
# transition is sensible (e.g. a Business Power owner buying Starter) is
# a question for G-C's activation step, once real money has actually been
# verified -- not for order creation, which is non-binding. The one
# Business-Power-specific rule this phase does enforce is structural: the
# server-side plan whitelist (POS_PLANS) never contains "business_power",
# so it can never be offered or accepted as a purchasable plan.


def _generate_billing_receipt(user_id, plan):
    """Merchant-generated Razorpay `receipt` for traceability only -- not
    used for idempotency/dedup lookups (see module docstring above: this
    phase deliberately keeps idempotency out of scope beyond the
    razorpay_order_id UNIQUE constraint)."""
    return f"pos_{plan}_{user_id}_{secrets.token_hex(6)}"


def _billing_order_payload(row, razorpay_key_id):
    return {
        "billing_order_id": row["id"],
        "razorpay_order_id": row["razorpay_order_id"],
        "amount": row["amount_paise"],
        "currency": row["currency"],
        "plan": row["pos_plan"],
        "status": row["status"],
        "razorpay_key_id": razorpay_key_id,
    }


@pos_bp.route("/billing/orders", methods=["POST"])
@jwt_required()
def create_billing_order():
    """Creates a Razorpay order for the authenticated owner's intended POS
    plan and records it in pos_billing_orders. Does not verify payment,
    does not activate/modify pos_subscriptions -- see the module note
    above. Only `plan` is accepted from the client; amount, currency, and
    owner identity are always server-resolved."""
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    if plan not in POS_PLANS:
        return jsonify({
            "success": False,
            "error": "Invalid plan",
            "allowed_plans": list(POS_PLANS.keys()),
        }), 400

    amount_paise = POS_PLANS[plan]["monthly_price_paise"]

    client = get_razorpay_client()
    if not client:
        return jsonify({"success": False, "error": "Payment provider unavailable"}), 503

    try:
        order = client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "payment_capture": 1,
            "receipt": _generate_billing_receipt(user_id, plan),
            "notes": {
                "purpose": "pos_subscription",
                "pos_plan": plan,
                "owner_user_id": str(user_id),
            },
        })
    except Exception:
        # Razorpay rejected/errored -- no DB row is written, so nothing
        # here can ever look like a successfully created order that
        # Razorpay never actually created.
        logger.exception("razorpay pos billing order creation failed")
        return jsonify({"success": False, "error": "Unable to create payment order"}), 502

    razorpay_order_id = order.get("id") if isinstance(order, dict) else None
    if not razorpay_order_id:
        logger.error("razorpay pos billing order creation returned no order id")
        return jsonify({"success": False, "error": "Unable to create payment order"}), 502

    conn = None
    try:
        conn = get_db_connection()
        try:
            row = conn.execute(
                text("""
                    INSERT INTO pos_billing_orders
                        (owner_user_id, pos_plan, amount_paise, currency,
                         razorpay_order_id, status, created_at, updated_at)
                    VALUES
                        (:uid, :plan, :amount, 'INR', :rzp_order_id, 'created',
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING id, owner_user_id, pos_plan, amount_paise, currency,
                              razorpay_order_id, status, created_at
                """),
                {
                    "uid": user_id,
                    "plan": plan,
                    "amount": amount_paise,
                    "rzp_order_id": razorpay_order_id,
                },
            ).fetchone()
            conn.commit()
        except IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            # A Razorpay order was created but this exact razorpay_order_id
            # already exists locally -- practically unreachable (Razorpay
            # order ids are themselves unique per creation call), but fail
            # safely rather than silently overwrite/duplicate.
            logger.error("duplicate razorpay_order_id on pos billing order insert")
            return jsonify({"success": False, "error": "Duplicate order"}), 409

        key_id, _ = get_razorpay_key_pair()
        billing_order = dict(row._mapping)
        return jsonify(_billing_order_payload(billing_order, key_id)), 201
    except Exception:
        logger.exception("create pos billing order failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# =====================================================
# POS BILLING PAYMENT VERIFICATION + WEBHOOK (Phase G-B)
# =====================================================
# Establishes that a pos_billing_orders row received a genuine, verified
# Razorpay payment. Does NOT activate pos_subscriptions, does NOT change
# business access/limits, does NOT touch Business Power -- G-C owns all
# of that. The only state this section ever writes is on
# pos_billing_orders itself: status ('created' -> 'paid'/'failed'),
# razorpay_payment_id, paid_at, updated_at.
#
# State model: created -> paid | failed. This is the smallest set that
# distinguishes "awaiting payment" from "verified paid" (what G-C's
# activation step will look for) and "known failed" (so a failed attempt
# doesn't look identical to a never-attempted order). No further states
# (e.g. "refunded", "expired") are added here -- not needed by this
# phase's scope, and speculative states were explicitly disallowed.
#
# Idempotency mechanism: no separate webhook-event ledger table.
# pos_billing_orders itself is the single source of truth, protected two
# ways: (1) a conditional `UPDATE ... WHERE status <> 'paid'` inside a
# `SELECT ... FOR UPDATE`-locked transaction (see _finalize_pos_payment),
# so at most one caller ever transitions a given row out of 'created'/
# 'failed', and every concurrent/duplicate caller either blocks briefly on
# the row lock and then observes the already-'paid' state, or (for a
# `payment.failed` event) idempotently re-applies the same terminal state;
# (2) a partial UNIQUE index on razorpay_payment_id, so the same Razorpay
# payment id can never end up attached to two different local orders even
# under a race. Both the client-verification endpoint and the webhook
# funnel through this same helper, so "verification then webhook",
# "webhook then verification", and "duplicate of either" all resolve
# through identical, single-writer-at-a-time logic. This was judged
# sufficient for reliable idempotency without introducing a
# pos_billing_webhook_events table (Phase G-B item H's own condition for
# creating one: "ONLY if needed").


def _load_billing_order(conn, billing_order_id, owner_user_id):
    row = conn.execute(
        text("""
            SELECT id, owner_user_id, pos_plan, amount_paise, currency,
                   razorpay_order_id, razorpay_payment_id, status, paid_at
            FROM pos_billing_orders
            WHERE id = :id AND owner_user_id = :uid
        """),
        {"id": billing_order_id, "uid": owner_user_id},
    ).fetchone()
    return dict(row._mapping) if row else None


def _load_billing_order_by_razorpay_order_id(conn, razorpay_order_id):
    row = conn.execute(
        text("""
            SELECT id, owner_user_id, pos_plan, amount_paise, currency,
                   razorpay_order_id, razorpay_payment_id, status, paid_at
            FROM pos_billing_orders
            WHERE razorpay_order_id = :rzp_order_id
        """),
        {"rzp_order_id": razorpay_order_id},
    ).fetchone()
    return dict(row._mapping) if row else None


def _billing_order_status_payload(row):
    return {
        "billing_order_id": row["id"],
        "status": row["status"],
        "plan": row["pos_plan"],
        "amount": row["amount_paise"],
        "currency": row["currency"],
        "razorpay_order_id": row["razorpay_order_id"],
        "razorpay_payment_id": row.get("razorpay_payment_id"),
    }


def _finalize_pos_payment(conn, billing_order_id, owner_user_id, razorpay_payment_id):
    """The single write path that ever moves a pos_billing_orders row to
    'paid'. Locks the row first (FOR UPDATE) so concurrent callers
    (duplicate verification calls, a webhook racing a client call, two
    webhook deliveries) serialize on this one row rather than double-apply
    the transition -- see module note above.

    Returns (row, outcome) where outcome is one of:
      "paid_now"     -- this call performed the created/failed -> paid
                        transition.
      "already_same" -- already 'paid' with this exact razorpay_payment_id;
                        idempotent no-op.
      "conflict"      -- already 'paid' with a DIFFERENT payment id, or the
                        UPDATE hit the partial UNIQUE index because
                        razorpay_payment_id is already attached to a
                        different row (raises IntegrityError instead --
                        caller must catch it).
      "not_found"     -- no such row for this id+owner (defensive; callers
                        have normally already confirmed existence).
    """
    row = conn.execute(
        text("""
            SELECT id, owner_user_id, pos_plan, amount_paise, currency,
                   razorpay_order_id, razorpay_payment_id, status, paid_at
            FROM pos_billing_orders
            WHERE id = :id AND owner_user_id = :uid
            FOR UPDATE
        """),
        {"id": billing_order_id, "uid": owner_user_id},
    ).fetchone()
    row = dict(row._mapping) if row else None
    if row is None:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, "not_found"

    if row["status"] == "paid":
        conn.commit()  # nothing to change; release the row lock
        if row["razorpay_payment_id"] == razorpay_payment_id:
            return row, "already_same"
        return row, "conflict"

    updated = conn.execute(
        text("""
            UPDATE pos_billing_orders
            SET status = 'paid',
                razorpay_payment_id = :pid,
                paid_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND status <> 'paid'
            RETURNING id, owner_user_id, pos_plan, amount_paise, currency,
                      razorpay_order_id, razorpay_payment_id, status, paid_at
        """),
        {"id": billing_order_id, "pid": razorpay_payment_id},
    ).fetchone()
    conn.commit()
    if updated is None:
        # Lost a race despite the lock (shouldn't happen, but fail safe
        # rather than claim success) -- treat like already-paid-elsewhere.
        return row, "conflict"
    return dict(updated._mapping), "paid_now"


def _mark_pos_billing_order_failed(conn, billing_order_id):
    """Idempotent: applying this twice, or after the order is already
    'paid', leaves state unchanged either way -- never regresses a paid
    order back to failed (mirrors services/payment_service.py's
    mark_payment_failed never regressing an activated row)."""
    conn.execute(
        text("""
            UPDATE pos_billing_orders
            SET status = 'failed', updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND status <> 'paid'
        """),
        {"id": billing_order_id},
    )
    conn.commit()


@pos_bp.route("/billing/verify", methods=["POST"])
@jwt_required()
def verify_billing_payment():
    """Client-initiated payment verification after Razorpay checkout.
    Never trusts the client's claim of success -- the Razorpay signature,
    order, and payment are all independently confirmed server-side before
    anything is written. See the module note above for the full
    idempotency/consistency model."""
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    data = request.get_json(silent=True) or {}
    billing_order_id = data.get("billing_order_id")
    razorpay_order_id = data.get("razorpay_order_id")
    razorpay_payment_id = data.get("razorpay_payment_id")
    razorpay_signature = data.get("razorpay_signature")

    if (
        not isinstance(billing_order_id, int) or isinstance(billing_order_id, bool)
        or not isinstance(razorpay_order_id, str) or not razorpay_order_id
        or not isinstance(razorpay_payment_id, str) or not razorpay_payment_id
        or not isinstance(razorpay_signature, str) or not razorpay_signature
    ):
        return jsonify({"success": False, "error": "Missing required fields"}), 400

    conn = None
    try:
        conn = get_db_connection()

        row = _load_billing_order(conn, billing_order_id, user_id)
        if row is None:
            # Same response whether the id doesn't exist at all or belongs
            # to another owner -- never confirm another owner's billing
            # order exists.
            return jsonify({"success": False, "error": "Billing order not found"}), 404

        if row["razorpay_order_id"] != razorpay_order_id:
            return jsonify({
                "success": False,
                "error": "Order does not match this billing order",
            }), 409

        if row["status"] == "paid":
            if row["razorpay_payment_id"] == razorpay_payment_id:
                return jsonify(_billing_order_status_payload(row)), 200
            return jsonify({
                "success": False,
                "error": "Billing order already associated with a different payment",
            }), 409

        client = get_razorpay_client()
        if not client:
            return jsonify({"success": False, "error": "Payment provider unavailable"}), 503

        try:
            client.utility.verify_payment_signature({
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            })
        except Exception:
            return jsonify({
                "success": False,
                "error": "Payment signature verification failed",
            }), 400

        try:
            rzp_order = client.order.fetch(razorpay_order_id)
        except Exception:
            logger.exception("razorpay order fetch failed during pos billing verification")
            return jsonify({"success": False, "error": "Unable to verify payment"}), 502
        if not isinstance(rzp_order, dict) or rzp_order.get("status") != "paid":
            return jsonify({"success": False, "error": "Order not paid"}), 400

        try:
            payment = client.payment.fetch(razorpay_payment_id)
        except Exception:
            logger.exception("razorpay payment fetch failed during pos billing verification")
            return jsonify({"success": False, "error": "Unable to verify payment"}), 502
        if not isinstance(payment, dict) or payment.get("status") != "captured":
            return jsonify({"success": False, "error": "Payment not captured"}), 400
        if str(payment.get("order_id") or "") != str(razorpay_order_id):
            return jsonify({
                "success": False,
                "error": "Payment does not belong to this order",
            }), 409

        expected_amount = row["amount_paise"]
        expected_currency = row["currency"]
        order_amount = rzp_order.get("amount")
        payment_amount = payment.get("amount")
        if (
            order_amount is None or payment_amount is None
            or int(order_amount) != expected_amount
            or int(payment_amount) != expected_amount
        ):
            return jsonify({"success": False, "error": "Amount mismatch"}), 409

        order_currency = rzp_order.get("currency") or expected_currency
        payment_currency = payment.get("currency") or expected_currency
        if order_currency != expected_currency or payment_currency != expected_currency:
            return jsonify({"success": False, "error": "Currency mismatch"}), 409

        try:
            finalized_row, outcome = _finalize_pos_payment(
                conn, billing_order_id, user_id, razorpay_payment_id
            )
        except IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({
                "success": False,
                "error": "This payment is already associated with a different billing order",
            }), 409

        if outcome == "conflict":
            return jsonify({
                "success": False,
                "error": "Billing order already associated with a different payment",
            }), 409
        if outcome == "not_found":
            return jsonify({"success": False, "error": "Billing order not found"}), 404

        return jsonify(_billing_order_status_payload(finalized_row)), 200
    except Exception:
        logger.exception("pos billing verification failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/billing/webhook", methods=["POST"])
def pos_billing_webhook():
    """POS-specific Razorpay webhook. Authenticated by Razorpay's HMAC
    webhook signature (never JWT) -- mirrors
    routes/payment_routes.py:razorpay_webhook's exact HMAC mechanism and
    the existing config.payment_config.get_razorpay_webhook_secret(), but
    writes only to pos_billing_orders, never to the marketplace `payments`
    table. The webhook payload's own amount/currency/status are trusted
    directly once the HMAC signature is verified (the signature already
    proves Razorpay authenticity) -- unlike the client-facing /verify
    endpoint above, which independently re-fetches from the Razorpay API
    since a client request carries no such proof."""
    payload = request.get_data() or b""
    signature = request.headers.get("X-Razorpay-Signature") or ""

    webhook_secret = get_razorpay_webhook_secret()
    if not webhook_secret:
        return jsonify({"success": False, "error": "Webhook secret not configured"}), 503

    expected = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        return jsonify({"success": False, "error": "Invalid webhook signature"}), 403

    data = request.get_json(silent=True) or {}
    event_type = data.get("event")
    try:
        entity = data["payload"]["payment"]["entity"]
        razorpay_payment_id = entity["id"]
        razorpay_order_id = entity.get("order_id")
        entity_status = entity.get("status")
        amount = entity.get("amount")
        currency = entity.get("currency")
    except Exception:
        return jsonify({"success": False, "error": "Invalid payload"}), 400

    if not razorpay_order_id:
        return jsonify({"success": True, "status": "ignored"}), 200

    conn = None
    try:
        conn = get_db_connection()
        row = _load_billing_order_by_razorpay_order_id(conn, razorpay_order_id)
        if row is None:
            # Not a POS billing order (unknown id, or a marketplace order
            # on the same merchant account) -- acknowledge and ignore
            # rather than guess.
            return jsonify({"success": True, "status": "order_not_found"}), 200

        if event_type == "payment.failed" or entity_status == "failed":
            _mark_pos_billing_order_failed(conn, row["id"])
            return jsonify({"success": True, "status": "failed_recorded"}), 200

        if event_type != "payment.captured" or entity_status != "captured":
            return jsonify({"success": True, "status": "ignored"}), 200

        if int(amount or 0) != row["amount_paise"] or (currency or row["currency"]) != row["currency"]:
            logger.error(
                "pos billing webhook amount/currency mismatch for razorpay_order_id=%s",
                razorpay_order_id,
            )
            return jsonify({"success": False, "error": "Amount mismatch"}), 409

        try:
            finalized_row, outcome = _finalize_pos_payment(
                conn, row["id"], row["owner_user_id"], razorpay_payment_id
            )
        except IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(
                "pos billing webhook duplicate razorpay_payment_id=%s", razorpay_payment_id
            )
            return jsonify({
                "success": False,
                "error": "Payment already associated with a different order",
            }), 409

        if outcome == "conflict":
            logger.error(
                "pos billing webhook conflicting payment for razorpay_order_id=%s",
                razorpay_order_id,
            )
            return jsonify({
                "success": False,
                "error": "Order already associated with a different payment",
            }), 409
        if outcome == "not_found":
            return jsonify({"success": True, "status": "order_not_found"}), 200

        status_out = "already_processed" if outcome == "already_same" else "captured"
        return jsonify({"success": True, "status": status_out}), 200
    except Exception:
        logger.exception("pos billing webhook processing failed")
        return jsonify({"success": False, "error": "Could not process webhook"}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# =====================================================
# POS SUBSCRIPTION ACTIVATION (Phase G-C)
# =====================================================
# The only route that ever writes to pos_subscriptions from the billing
# flow. Trusts nothing except pos_billing_orders.status == 'paid' -- that
# status was already established, independently, by G-B's signature
# verification/webhook; this route does not re-verify Razorpay, does not
# accept a razorpay_payment_id from the client, and does not accept
# plan/amount/owner from the client. All lifecycle policy (renewal,
# upgrade, downgrade protection, expired/suspended restart) lives in
# services.pos_billing_activation, which is also where the transactional
# idempotency guarantees are implemented -- see that module for the full
# concurrency/atomicity explanation.
#
# G-B's webhook is intentionally left unchanged: it still only marks a
# billing order 'paid', nothing more. Auto-activation from the webhook is
# deliberately deferred to a later phase (see the Phase G-C report) rather
# than wiring it in now -- this endpoint is what makes every paid order
# safely activatable in the meantime, and not touching the webhook keeps
# G-B's already-reviewed security boundary untouched.


def _activation_payload(result):
    order = result["billing_order"]
    sub = result.get("subscription") or {}
    return {
        "billing_order_id": order["id"],
        "status": "activated",
        "plan": sub.get("pos_plan"),
        "subscription_status": sub.get("status"),
        "expires_at": _format_expiry(sub.get("expires_at")),
        "subscription_changed": result.get("subscription_changed", False),
    }


@pos_bp.route("/billing/activate", methods=["POST"])
@jwt_required()
def activate_billing_order():
    """Applies a paid billing order to pos_subscriptions. Idempotent: a
    billing order that was already activated returns its existing
    resulting subscription state rather than re-applying the period."""
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    data = request.get_json(silent=True) or {}
    billing_order_id = data.get("billing_order_id")
    if not isinstance(billing_order_id, int) or isinstance(billing_order_id, bool):
        return jsonify({"success": False, "error": "Missing required fields"}), 400

    conn = None
    try:
        conn = get_db_connection()
        result = activate_paid_billing_order(conn, billing_order_id, user_id)

        if result["outcome"] == "not_found":
            # Same response whether the id doesn't exist at all or
            # belongs to another owner -- never confirm another owner's
            # billing order exists.
            return jsonify({"success": False, "error": "Billing order not found"}), 404
        if result["outcome"] == "not_paid":
            return jsonify({"success": False, "error": "Billing order is not paid"}), 409

        return jsonify(_activation_payload(result)), 200
    except Exception:
        logger.exception("pos billing activation failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
