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
from datetime import datetime, timedelta

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
        # True only when a real pos_inventory row exists for this product --
        # independent of quantity, so a tracked-but-depleted product
        # (quantity 0, is_tracked True) is distinguishable from one that
        # has never been stock-tracked at all (quantity 0, is_tracked
        # False). See create_sale: the same row-existence check is what
        # decides whether a sale deducts stock at all.
        "is_tracked": row["inventory_product_id"] is not None,
    }


# Postgres INTEGER is 32-bit signed; keep well under that so a
# quantity*price multiplication (or a running total) can never wrap.
_MAX_SALE_QUANTITY = 1_000_000
_MAX_SALE_AMOUNT = 2_000_000_000

# Sales V1 checkout flow: the whitelist a client's "payment_method" must
# match. Deliberately server-enforced the same way POS_PLANS gates
# billing plans -- a client can never introduce a new payment method the
# backend hasn't been told how to report/reconcile.
_PAYMENT_METHODS = {"cash", "card", "upi"}


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
    # Customer & Sale Association V1.5 (Phase 8): sale_row carries these
    # flat customer_* keys from either create_sale's own lookup (merged
    # in before this call) or list_sales' LEFT JOIN -- same shape either
    # way, so this one function serves both call sites unchanged.
    # customer_id is None for the far-more-common no-customer sale
    # (including every pre-existing row, which never had one).
    customer_id = sale_row.get("customer_id")
    customer = (
        _customer_payload({
            "id": customer_id,
            "name": sale_row["customer_name"],
            "phone": sale_row["customer_phone"],
            "created_at": sale_row["customer_created_at"],
        })
        if customer_id is not None
        else None
    )
    return {
        "id": sale_row["id"],
        "total_amount": sale_row["total_amount"],
        "payment_method": sale_row["payment_method"],
        "created_at": created_at.isoformat() if created_at else None,
        "customer": customer,
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
                       COALESCE(i.quantity, 0) AS quantity,
                       i.product_id AS inventory_product_id
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


def _load_owned_active_product(conn, business_id, product_id):
    """Same product-ownership shape as create_sale's per-line lookup:
    wrong business, nonexistent, and inactive all resolve to the same
    "not found" outcome here, so a caller can never tell them apart --
    consistent with every other product-scoped query in this file
    (list_products/list_inventory/create_sale all filter is_active = 1)."""
    row = conn.execute(
        text("""
            SELECT id, name FROM pos_products
            WHERE id = :product_id AND business_id = :business_id AND is_active = 1
        """),
        {"product_id": product_id, "business_id": business_id},
    ).fetchone()
    return dict(row._mapping) if row else None


def _upsert_inventory_quantity(conn, business_id, product, set_absolute, quantity):
    """The one place pos_inventory rows are created or changed outside of
    a sale's deduction. A single `INSERT ... ON CONFLICT (product_id) DO
    UPDATE` is used instead of `SELECT ... FOR UPDATE` + branch: Postgres
    resolves the conflict atomically under its own row-level lock, so two
    concurrent receive/adjust calls (or a receive racing a sale's own FOR
    UPDATE lock on the same row) can never duplicate the row or lose an
    update -- the same upsert pattern already used elsewhere in this
    backend for exactly this "create or update a singleton row" shape
    (see pos_subscriptions/pending_referrals). `set_absolute` selects
    adjust's "set to exactly this value" vs receive's "add this many".
    """
    quantity_sql = "EXCLUDED.quantity" if set_absolute else "pos_inventory.quantity + EXCLUDED.quantity"
    row = conn.execute(
        text(f"""
            INSERT INTO pos_inventory (product_id, business_id, quantity, updated_at)
            VALUES (:product_id, :business_id, :quantity, CURRENT_TIMESTAMP)
            ON CONFLICT (product_id) DO UPDATE SET
                quantity = {quantity_sql},
                updated_at = CURRENT_TIMESTAMP
            RETURNING quantity
        """),
        {"product_id": product["id"], "business_id": business_id, "quantity": quantity},
    ).fetchone()
    return _inventory_payload({
        "product_id": product["id"],
        "product_name": product["name"],
        "quantity": row._mapping["quantity"],
        "inventory_product_id": product["id"],
    })


@pos_bp.route("/businesses/<int:business_id>/inventory/<int:product_id>/receive", methods=["POST"])
@jwt_required()
def receive_inventory(business_id, product_id):
    """Adds stock, creating the pos_inventory row (and starting tracking)
    if it doesn't exist yet -- the "future receive stock feature" the
    table's own comment in database/init_db.py anticipated. Never touches
    create_sale's deduction logic."""
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

        quantity = data.get("quantity")
        # bool is a subclass of int in Python -- reject it explicitly, same
        # as create_sale's item quantity check.
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or quantity > _MAX_SALE_QUANTITY
        ):
            return jsonify({"success": False, "error": "quantity must be a positive integer"}), 400

        product = _load_owned_active_product(conn, business_id, product_id)
        if product is None:
            return jsonify({"success": False, "error": "Product not found"}), 404

        item = _upsert_inventory_quantity(
            conn, business_id, product, set_absolute=False, quantity=quantity
        )
        conn.commit()
        return jsonify({"inventory_item": item}), 200
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("receive pos inventory failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/businesses/<int:business_id>/inventory/<int:product_id>/adjust", methods=["POST"])
@jwt_required()
def adjust_inventory(business_id, product_id):
    """Sets stock to an exact quantity (including 0), creating the
    pos_inventory row if it doesn't exist yet -- the resulting item is
    always tracked, matching receive_inventory."""
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

        quantity = data.get("quantity")
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
            or quantity > _MAX_SALE_QUANTITY
        ):
            return jsonify({"success": False, "error": "quantity must be a non-negative integer"}), 400

        product = _load_owned_active_product(conn, business_id, product_id)
        if product is None:
            return jsonify({"success": False, "error": "Product not found"}), 404

        item = _upsert_inventory_quantity(
            conn, business_id, product, set_absolute=True, quantity=quantity
        )
        conn.commit()
        return jsonify({"inventory_item": item}), 200
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("adjust pos inventory failed")
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

        payment_method = data.get("payment_method")
        if payment_method not in _PAYMENT_METHODS:
            return jsonify({
                "success": False,
                "error": "payment_method must be one of: " + ", ".join(sorted(_PAYMENT_METHODS)),
            }), 400

        # Customer & Sale Association V1.5 (Phase 8): optional --
        # omitted/null means a walk-in sale, exactly like every sale
        # before this feature existed. Never trust the client past this
        # point: ownership is re-checked against business_id here, the
        # same way every product_id in `items` below is, so a customer
        # from another business (or one that doesn't exist at all)
        # resolves to the same generic rejection -- never a distinct
        # signal an attacker could use to probe another business's
        # customer ids.
        raw_customer_id = data.get("customer_id")
        customer_row = None
        if raw_customer_id is not None:
            if isinstance(raw_customer_id, bool) or not isinstance(raw_customer_id, int):
                return jsonify({"success": False, "error": "customer_id must be an integer"}), 400
            customer_row = conn.execute(
                text("""
                    SELECT id, name, phone, created_at FROM pos_customers
                    WHERE id = :customer_id AND business_id = :business_id
                """),
                {"customer_id": raw_customer_id, "business_id": business_id},
            ).fetchone()
            if customer_row is None:
                return jsonify({"success": False, "error": "Customer not found"}), 400
            customer_row = dict(customer_row._mapping)

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
        # (product_id, quantity) pairs to deduct at the end -- only for
        # products with a tracked pos_inventory row (see the stock-check
        # block below for why an untracked product is never blocked).
        inventory_deductions = []
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
                conn.rollback()
                return jsonify({"success": False, "error": "One or more products are invalid"}), 400

            product = dict(product_row._mapping)
            quantity = item["quantity"]

            # Stock is enforced only for products that already have a
            # pos_inventory row. There is still no "receive stock"
            # endpoint (see that table's own comment in
            # database/init_db.py), so a missing row means this product's
            # stock has simply never been tracked -- not that it has zero
            # stock -- and must not block a sale that already worked
            # before stock tracking existed. FOR UPDATE locks any row
            # that IS tracked so a concurrent sale of the same product can
            # never oversell it (same locking pattern as
            # _finalize_pos_payment's billing-order lock).
            inventory_row = conn.execute(
                text("SELECT quantity FROM pos_inventory WHERE product_id = :product_id FOR UPDATE"),
                {"product_id": product["id"]},
            ).fetchone()
            if inventory_row is not None:
                available = inventory_row._mapping["quantity"]
                if available < quantity:
                    conn.rollback()
                    return jsonify({
                        "success": False,
                        "error": (
                            f"Insufficient stock for {product['name']} "
                            f"(have {available}, need {quantity})"
                        ),
                    }), 409
                inventory_deductions.append((product["id"], quantity))

            unit_price = product["price"]
            line_total = unit_price * quantity
            if line_total > _MAX_SALE_AMOUNT or total_amount + line_total > _MAX_SALE_AMOUNT:
                conn.rollback()
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
                INSERT INTO pos_sales
                    (business_id, total_amount, payment_method, customer_id, created_at)
                VALUES
                    (:business_id, :total_amount, :payment_method, :customer_id, CURRENT_TIMESTAMP)
                RETURNING id, total_amount, payment_method, customer_id, created_at
            """),
            {
                "business_id": business_id,
                "total_amount": total_amount,
                "payment_method": payment_method,
                "customer_id": customer_row["id"] if customer_row else None,
            },
        ).fetchone()
        sale = dict(sale_row._mapping)
        # _sale_payload expects these flat customer_* keys (see its own
        # comment) -- already fetched above during validation, so this is
        # never a second customer lookup.
        sale["customer_name"] = customer_row["name"] if customer_row else None
        sale["customer_phone"] = customer_row["phone"] if customer_row else None
        sale["customer_created_at"] = customer_row["created_at"] if customer_row else None

        for line in line_items:
            conn.execute(
                text("""
                    INSERT INTO pos_sale_items
                        (sale_id, product_id, product_name, unit_price, quantity, line_total)
                    VALUES (:sale_id, :product_id, :product_name, :unit_price, :quantity, :line_total)
                """),
                {"sale_id": sale["id"], **line},
            )

        # Applied last, still inside this same transaction: every lock
        # acquired above is held until this commit, so the sale record
        # and every stock deduction it implies become visible atomically
        # -- a reader never sees the sale without the matching deduction,
        # or the deduction without the sale.
        for product_id, quantity in inventory_deductions:
            conn.execute(
                text("""
                    UPDATE pos_inventory
                    SET quantity = quantity - :quantity, updated_at = CURRENT_TIMESTAMP
                    WHERE product_id = :product_id
                """),
                {"product_id": product_id, "quantity": quantity},
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


def _parse_page_param(raw):
    """Returns (page, error_message)."""
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None, "page must be a positive integer"
    if page < 1:
        return None, "page must be a positive integer"
    return page, None


def _parse_limit_param(raw):
    """Returns (limit, error_message)."""
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return None, "limit must be an integer between 1 and 100"
    if limit < 1 or limit > 100:
        return None, "limit must be an integer between 1 and 100"
    return limit, None


def _parse_date_param(raw, field_name):
    """Returns (date, error_message)."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except ValueError:
        return None, f"{field_name} must be a valid date (YYYY-MM-DD)"


@pos_bp.route("/businesses/<int:business_id>/sales", methods=["GET"])
@jwt_required()
def list_sales(business_id):
    """Paginated, newest-first sale history (Sales History V1.1) with
    optional date-range/payment-method filtering. Fetches the current
    page's items in one bulk query keyed by sale_id, grouped in Python --
    deliberately never one items query per sale (unlike this route's
    previous version), so cost no longer scales with how much history a
    business has.
    """
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    page, page_error = _parse_page_param(request.args.get("page", "1"))
    if page_error:
        return jsonify({"success": False, "error": page_error}), 400

    limit, limit_error = _parse_limit_param(request.args.get("limit", "20"))
    if limit_error:
        return jsonify({"success": False, "error": limit_error}), 400

    from_date = None
    raw_from = request.args.get("from")
    if raw_from:
        from_date, from_error = _parse_date_param(raw_from, "from")
        if from_error:
            return jsonify({"success": False, "error": from_error}), 400

    # The upper bound is exclusive and one day past `to` so the whole of
    # `to` itself (any time from 00:00:00 up to but not including the
    # next day) is included -- created_at is a TIMESTAMP, not a DATE.
    to_exclusive = None
    raw_to = request.args.get("to")
    if raw_to:
        to_date, to_error = _parse_date_param(raw_to, "to")
        if to_error:
            return jsonify({"success": False, "error": to_error}), 400
        to_exclusive = to_date + timedelta(days=1)

    payment_method = request.args.get("payment_method")
    if payment_method is not None and payment_method not in _PAYMENT_METHODS:
        return jsonify({
            "success": False,
            "error": "payment_method must be one of: " + ", ".join(sorted(_PAYMENT_METHODS)),
        }), 400

    # Customer Filter & Search V1.6 (Phase 9). Same _parse_page_param/
    # _parse_limit_param style: any query-string value that doesn't parse
    # cleanly as an integer (a bool-looking "true", a float-looking
    # "1.5", free text, ...) is rejected -- there is no separate bool/
    # float type to reject at this layer since query params always
    # arrive as strings.
    customer_id = None
    raw_customer_id = request.args.get("customer_id")
    if raw_customer_id is not None:
        try:
            customer_id = int(raw_customer_id)
        except ValueError:
            return jsonify({"success": False, "error": "customer_id must be an integer"}), 400

    # Blank ("customer_search=") is treated as not provided, same as the
    # existing from/to/payment_method optional-param convention.
    customer_search = request.args.get("customer_search")
    if customer_search is not None and not customer_search.strip():
        customer_search = None

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

        # Every fragment appended here is a fixed literal -- never
        # user-supplied text -- so building the WHERE clause this way is
        # safe; every actual value (including payment_method) still flows
        # through a bind parameter below, never string-interpolated.
        # Qualified with the `s.` alias (see the queries below) because
        # Phase 8's customer LEFT JOIN brings pos_customers.business_id/
        # created_at into scope too -- unqualified names would be
        # ambiguous once that join is present.
        where_clauses = ["s.business_id = :business_id"]
        params = {"business_id": business_id}
        if from_date is not None:
            where_clauses.append("s.created_at >= :from_date")
            params["from_date"] = from_date
        if to_exclusive is not None:
            where_clauses.append("s.created_at < :to_exclusive")
            params["to_exclusive"] = to_exclusive
        if payment_method is not None:
            where_clauses.append("s.payment_method = :payment_method")
            params["payment_method"] = payment_method
        if customer_id is not None:
            # Deliberately no separate pos_customers ownership lookup: a
            # sale's own customer_id can only ever have been set (by
            # create_sale) to a customer already belonging to that same
            # sale's business_id, so filtering by `s.business_id =
            # :business_id AND s.customer_id = :customer_id` together
            # already makes a nonexistent id and a cross-business id
            # resolve identically to zero matching rows -- an honest
            # empty page, never a 400, and never a way to distinguish
            # "wrong business" from "doesn't exist" from the response.
            where_clauses.append("s.customer_id = :customer_id")
            params["customer_id"] = customer_id
        if customer_search is not None:
            # ILIKE is Postgres case-insensitive LIKE (already used
            # elsewhere in this backend); substring match on the
            # LEFT-JOINed customer's own name/phone, never on any sale
            # field itself. A walk-in sale's c.name/c.phone are NULL from
            # the join, and `NULL ILIKE anything` is NULL (falsy), so
            # walk-in sales are naturally excluded whenever this filter
            # is active -- exactly the intended behavior.
            where_clauses.append("(c.name ILIKE :customer_search OR c.phone ILIKE :customer_search)")
            params["customer_search"] = f"%{customer_search.strip()}%"
        where_sql = " AND ".join(where_clauses)

        # Phase 9: always LEFT JOINed here too (not just in the main page
        # query below) so `where_sql` -- which may now reference c.name/
        # c.phone -- is valid verbatim in both queries, and COUNT(*)
        # still reflects the exact same filtered set. A LEFT JOIN never
        # duplicates or drops a pos_sales row (customer_id is nullable
        # 1:1), so this never changes the count on its own.
        total_row = conn.execute(
            text(f"""
                SELECT COUNT(*) AS total
                FROM pos_sales s
                LEFT JOIN pos_customers c
                    ON c.id = s.customer_id AND c.business_id = s.business_id
                WHERE {where_sql}
            """),
            params,
        ).fetchone()
        total = total_row._mapping["total"]

        offset = (page - 1) * limit
        # Customer & Sale Association V1.5 (Phase 8): customer info comes
        # from this same query's LEFT JOIN, never a second per-sale
        # lookup -- `c.business_id = s.business_id` is a defensive
        # belt-and-suspenders check (customer_id can only ever be set to
        # an already same-business-validated id by create_sale, so this
        # can never actually filter anything out in practice, but it
        # costs nothing and keeps this query itself provably
        # business-scoped even if that invariant ever changed).
        sale_rows = conn.execute(
            text(f"""
                SELECT s.id, s.total_amount, s.payment_method, s.created_at,
                       s.customer_id AS customer_id,
                       c.name AS customer_name,
                       c.phone AS customer_phone,
                       c.created_at AS customer_created_at
                FROM pos_sales s
                LEFT JOIN pos_customers c
                    ON c.id = s.customer_id AND c.business_id = s.business_id
                WHERE {where_sql}
                ORDER BY s.created_at DESC, s.id DESC
                LIMIT :limit OFFSET :offset
            """),
            {**params, "limit": limit, "offset": offset},
        ).fetchall()
        sale_dicts = [dict(row._mapping) for row in sale_rows]

        # One bulk items query for the whole page, grouped in Python --
        # replaces the old one-query-per-sale loop.
        sale_ids = [sale["id"] for sale in sale_dicts]
        items_by_sale_id = {sale_id: [] for sale_id in sale_ids}
        if sale_ids:
            item_rows = conn.execute(
                text("""
                    SELECT sale_id, product_id, product_name, unit_price, quantity, line_total
                    FROM pos_sale_items
                    WHERE sale_id = ANY(:sale_ids)
                    ORDER BY sale_id, id
                """),
                {"sale_ids": sale_ids},
            ).fetchall()
            for row in item_rows:
                row_dict = dict(row._mapping)
                items_by_sale_id[row_dict["sale_id"]].append(row_dict)

        sales = [_sale_payload(sale, items_by_sale_id[sale["id"]]) for sale in sale_dicts]

        return jsonify({
            "sales": sales,
            "page": page,
            "limit": limit,
            "total": total,
            "has_next": (page * limit) < total,
            "has_previous": page > 1,
        }), 200
    except Exception:
        logger.exception("list pos sales failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@pos_bp.route("/businesses/<int:business_id>/sales/analytics", methods=["GET"])
@jwt_required()
def get_sales_analytics(business_id):
    """Aggregated Sales Analytics V1.7 -- summary/payment-method/daily
    breakdowns for the active business, filtered the same way Sales
    History is (from/to/payment_method/customer_id, minus pagination and
    customer_search, which don't apply to an aggregate view). Exactly
    three fixed aggregate queries (plus the existing ownership/entitlement
    ones) regardless of how much history matches -- like
    get_dashboard_summary, this never selects individual sale rows, so
    cost scales with matching rows at the database level, not with
    request count on this server. Every date computation (`::date`
    truncation) is Postgres's own, operating on `created_at` as already
    stored -- never the caller's clock.
    """
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    from_date = None
    raw_from = request.args.get("from")
    if raw_from:
        from_date, from_error = _parse_date_param(raw_from, "from")
        if from_error:
            return jsonify({"success": False, "error": from_error}), 400

    to_date = None
    raw_to = request.args.get("to")
    if raw_to:
        to_date, to_error = _parse_date_param(raw_to, "to")
        if to_error:
            return jsonify({"success": False, "error": to_error}), 400

    if from_date is not None and to_date is not None and from_date > to_date:
        return jsonify({"success": False, "error": "from must not be after to"}), 400

    # Upper bound is exclusive and one day past `to`, same as list_sales,
    # so the whole of `to` itself (through 23:59:59) is included --
    # created_at is a TIMESTAMP, not a DATE.
    to_exclusive = to_date + timedelta(days=1) if to_date is not None else None

    payment_method = request.args.get("payment_method")
    if payment_method is not None and payment_method not in _PAYMENT_METHODS:
        return jsonify({
            "success": False,
            "error": "payment_method must be one of: " + ", ".join(sorted(_PAYMENT_METHODS)),
        }), 400

    customer_id = None
    raw_customer_id = request.args.get("customer_id")
    if raw_customer_id is not None:
        try:
            customer_id = int(raw_customer_id)
        except ValueError:
            return jsonify({"success": False, "error": "customer_id must be an integer"}), 400

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

        # No pos_customers join anywhere in this endpoint -- customer_id
        # only ever filters pos_sales.customer_id directly (never a
        # customer's name/phone), so unlike list_sales' customer_search
        # there is no ambiguous-column concern requiring a join at all.
        # Cross-business/nonexistent customer_id resolves to zero matching
        # rows for the same structural reason documented in list_sales.
        where_clauses = ["business_id = :business_id"]
        params = {"business_id": business_id}
        if from_date is not None:
            where_clauses.append("created_at >= :from_date")
            params["from_date"] = from_date
        if to_exclusive is not None:
            where_clauses.append("created_at < :to_exclusive")
            params["to_exclusive"] = to_exclusive
        if payment_method is not None:
            where_clauses.append("payment_method = :payment_method")
            params["payment_method"] = payment_method
        if customer_id is not None:
            where_clauses.append("customer_id = :customer_id")
            params["customer_id"] = customer_id
        where_sql = " AND ".join(where_clauses)

        summary_row = conn.execute(
            text(f"""
                SELECT COUNT(*) AS sale_count,
                       COALESCE(SUM(total_amount), 0) AS total_revenue,
                       COALESCE(ROUND(AVG(total_amount))::BIGINT, 0) AS average_sale
                FROM pos_sales
                WHERE {where_sql}
            """),
            params,
        ).fetchone()
        summary = dict(summary_row._mapping)

        # A NULL payment_method (a sale recorded before payment-method
        # tracking existed) is not a payment method -- excluded here so
        # this breakdown only ever reports real, known methods.
        payment_rows = conn.execute(
            text(f"""
                SELECT payment_method,
                       COUNT(*) AS sale_count,
                       COALESCE(SUM(total_amount), 0) AS total_amount
                FROM pos_sales
                WHERE {where_sql} AND payment_method IS NOT NULL
                GROUP BY payment_method
                ORDER BY payment_method
            """),
            params,
        ).fetchall()

        # `created_at::date` -- same plain, no-timezone-conversion cast
        # this file already relies on elsewhere (created_at is a bare
        # TIMESTAMP; every date comparison in list_sales compares it
        # directly against a `date` value the same way).
        daily_rows = conn.execute(
            text(f"""
                SELECT created_at::date AS sale_date,
                       COUNT(*) AS sale_count,
                       COALESCE(SUM(total_amount), 0) AS total_amount
                FROM pos_sales
                WHERE {where_sql}
                GROUP BY sale_date
                ORDER BY sale_date
            """),
            params,
        ).fetchall()

        as_of = conn.execute(text("SELECT NOW() AS as_of")).fetchone()._mapping["as_of"]

        return jsonify({
            "summary": {
                "sale_count": summary["sale_count"],
                "total_revenue": summary["total_revenue"],
                "average_sale": summary["average_sale"],
            },
            "payment_methods": [
                {
                    "payment_method": row._mapping["payment_method"],
                    "sale_count": row._mapping["sale_count"],
                    "total_amount": row._mapping["total_amount"],
                }
                for row in payment_rows
            ],
            "daily_sales": [
                {
                    "date": row._mapping["sale_date"].isoformat(),
                    "sale_count": row._mapping["sale_count"],
                    "total_amount": row._mapping["total_amount"],
                }
                for row in daily_rows
            ],
            "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        }), 200
    except Exception:
        logger.exception("get pos sales analytics failed")
        return jsonify({"success": False, "error": "Something went wrong. Please try again."}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# Reports V1.8: top products/customers are each capped at this many rows --
# a small local business owner never needs more than a top-10 view, and an
# explicit fixed LIMIT keeps the response bounded regardless of catalog/
# customer-roster size (see get_sales_reports).
_REPORTS_TOP_N = 10


@pos_bp.route("/businesses/<int:business_id>/sales/reports", methods=["GET"])
@jwt_required()
def get_sales_reports(business_id):
    """POS Operational Reports V1.8 -- one aggregated response answering
    the small-business-owner questions this phase exists for (how much did
    I sell, how many bills, best sellers, payment mix, top customers, daily
    trend). Same from/to/payment_method/customer_id filter semantics as
    Sales History/Analytics (routes/pos_routes.py:list_sales /
    get_sales_analytics) -- `from`/`to` inclusive, upper bound handled as
    an exclusive one-day-past-`to` comparison against `created_at`, every
    date computed server-side, never the caller's clock.

    Every aggregate here is computed by a small FIXED number of SQL
    queries (ownership + entitlement + summary + top_products +
    payment_methods + top_customers + daily_sales + as_of) regardless of
    how many sales/items/customers match -- never one query per product,
    per customer, or per day, and never fetches individual sale rows into
    Python. All queries share one `where_sql` built against the `s`
    (pos_sales) alias so every breakdown is filtered identically and an
    excluded sale's items/association can never leak into a total.
    """
    user_id = _as_user_id(get_jwt_identity())
    if user_id is None:
        return jsonify({"success": False, "error": "Invalid session"}), 401

    from_date = None
    raw_from = request.args.get("from")
    if raw_from:
        from_date, from_error = _parse_date_param(raw_from, "from")
        if from_error:
            return jsonify({"success": False, "error": from_error}), 400

    to_date = None
    raw_to = request.args.get("to")
    if raw_to:
        to_date, to_error = _parse_date_param(raw_to, "to")
        if to_error:
            return jsonify({"success": False, "error": to_error}), 400

    if from_date is not None and to_date is not None and from_date > to_date:
        return jsonify({"success": False, "error": "from must not be after to"}), 400

    # Upper bound is exclusive and one day past `to`, same as list_sales/
    # get_sales_analytics, so the whole of `to` itself (through 23:59:59)
    # is included -- created_at is a TIMESTAMP, not a DATE.
    to_exclusive = to_date + timedelta(days=1) if to_date is not None else None

    payment_method = request.args.get("payment_method")
    if payment_method is not None and payment_method not in _PAYMENT_METHODS:
        return jsonify({
            "success": False,
            "error": "payment_method must be one of: " + ", ".join(sorted(_PAYMENT_METHODS)),
        }), 400

    customer_id = None
    raw_customer_id = request.args.get("customer_id")
    if raw_customer_id is not None:
        try:
            customer_id = int(raw_customer_id)
        except ValueError:
            return jsonify({"success": False, "error": "customer_id must be an integer"}), 400

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

        # Aliased `s` throughout (pos_sales) so this same where_sql is
        # valid verbatim whether or not a given query also joins
        # pos_sale_items/pos_customers -- one filter definition, applied
        # identically everywhere, so an excluded sale's items/customer
        # association can never leak into a total.
        where_clauses = ["s.business_id = :business_id"]
        params = {"business_id": business_id}
        if from_date is not None:
            where_clauses.append("s.created_at >= :from_date")
            params["from_date"] = from_date
        if to_exclusive is not None:
            where_clauses.append("s.created_at < :to_exclusive")
            params["to_exclusive"] = to_exclusive
        if payment_method is not None:
            where_clauses.append("s.payment_method = :payment_method")
            params["payment_method"] = payment_method
        if customer_id is not None:
            where_clauses.append("s.customer_id = :customer_id")
            params["customer_id"] = customer_id
        where_sql = " AND ".join(where_clauses)

        summary_row = conn.execute(
            text(f"""
                SELECT COUNT(*) AS sale_count,
                       COALESCE(SUM(s.total_amount), 0) AS total_revenue,
                       COALESCE(ROUND(AVG(s.total_amount))::BIGINT, 0) AS average_sale
                FROM pos_sales s
                WHERE {where_sql}
            """),
            params,
        ).fetchone()
        summary = dict(summary_row._mapping)

        # Top products (D): aggregated straight from pos_sale_items joined
        # to their parent sale, so only items belonging to a sale that
        # matches every active filter are ever summed -- an item on an
        # excluded sale never contributes. Sorted by quantity_sold DESC
        # (the spec's primary key) with revenue DESC then product_id ASC
        # as a fully deterministic tie-break, capped at _REPORTS_TOP_N.
        product_rows = conn.execute(
            text(f"""
                SELECT si.product_id AS product_id,
                       si.product_name AS product_name,
                       SUM(si.quantity) AS quantity_sold,
                       SUM(si.line_total) AS revenue
                FROM pos_sale_items si
                JOIN pos_sales s ON s.id = si.sale_id
                WHERE {where_sql}
                GROUP BY si.product_id, si.product_name
                ORDER BY quantity_sold DESC, revenue DESC, si.product_id ASC
                LIMIT :top_n
            """),
            {**params, "top_n": _REPORTS_TOP_N},
        ).fetchall()

        # Payment methods (F): same aggregation/exclusion semantics as
        # get_sales_analytics -- a NULL payment_method (pre-tracking sale)
        # is not a payment method and is excluded here, while still
        # counted in `summary` above.
        payment_rows = conn.execute(
            text(f"""
                SELECT s.payment_method AS payment_method,
                       COUNT(*) AS sale_count,
                       COALESCE(SUM(s.total_amount), 0) AS total_amount
                FROM pos_sales s
                WHERE {where_sql} AND s.payment_method IS NOT NULL
                GROUP BY s.payment_method
                ORDER BY s.payment_method
            """),
            params,
        ).fetchall()

        # Top customers (E): walk-in sales (customer_id IS NULL) are
        # excluded by the JOIN itself (an INNER JOIN drops any sale with
        # no matching pos_customers row) -- no separate NULL check needed.
        # `c.business_id = s.business_id` keeps this provably
        # business-scoped even though customer_id can only ever already
        # be same-business (same defensive belt-and-suspenders as
        # list_sales' own customer JOIN). Sorted by total_amount DESC (the
        # spec's primary key) then customer_id ASC as a deterministic
        # tie-break, capped at _REPORTS_TOP_N.
        customer_rows = conn.execute(
            text(f"""
                SELECT s.customer_id AS customer_id,
                       c.name AS customer_name,
                       c.phone AS customer_phone,
                       COUNT(*) AS sale_count,
                       COALESCE(SUM(s.total_amount), 0) AS total_amount
                FROM pos_sales s
                JOIN pos_customers c
                    ON c.id = s.customer_id AND c.business_id = s.business_id
                WHERE {where_sql}
                GROUP BY s.customer_id, c.name, c.phone
                ORDER BY total_amount DESC, s.customer_id ASC
                LIMIT :top_n
            """),
            {**params, "top_n": _REPORTS_TOP_N},
        ).fetchall()

        # Daily sales (G): same `created_at::date` grouping as
        # get_sales_analytics, oldest -> newest (ascending sale_date) --
        # matches that endpoint's existing convention exactly. Never
        # zero-filled: a day with no matching sale is simply absent.
        daily_rows = conn.execute(
            text(f"""
                SELECT s.created_at::date AS sale_date,
                       COUNT(*) AS sale_count,
                       COALESCE(SUM(s.total_amount), 0) AS total_amount
                FROM pos_sales s
                WHERE {where_sql}
                GROUP BY sale_date
                ORDER BY sale_date
            """),
            params,
        ).fetchall()

        as_of = conn.execute(text("SELECT NOW() AS as_of")).fetchone()._mapping["as_of"]

        return jsonify({
            "summary": {
                "sale_count": summary["sale_count"],
                "total_revenue": summary["total_revenue"],
                "average_sale": summary["average_sale"],
            },
            "top_products": [
                {
                    "product_id": row._mapping["product_id"],
                    "product_name": row._mapping["product_name"],
                    "quantity_sold": row._mapping["quantity_sold"],
                    "revenue": row._mapping["revenue"],
                }
                for row in product_rows
            ],
            "payment_methods": [
                {
                    "payment_method": row._mapping["payment_method"],
                    "sale_count": row._mapping["sale_count"],
                    "total_amount": row._mapping["total_amount"],
                }
                for row in payment_rows
            ],
            "top_customers": [
                {
                    "customer_id": row._mapping["customer_id"],
                    "customer_name": row._mapping["customer_name"],
                    "phone": row._mapping["customer_phone"],
                    "sale_count": row._mapping["sale_count"],
                    "total_amount": row._mapping["total_amount"],
                }
                for row in customer_rows
            ],
            "daily_sales": [
                {
                    "date": row._mapping["sale_date"].isoformat(),
                    "sale_count": row._mapping["sale_count"],
                    "total_amount": row._mapping["total_amount"],
                }
                for row in daily_rows
            ],
            "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        }), 200
    except Exception:
        logger.exception("get pos sales reports failed")
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


def _dashboard_period_payload(row):
    return {
        "sale_count": row["sale_count"],
        "total_amount": row["total_amount"],
    }


@pos_bp.route("/businesses/<int:business_id>/dashboard/summary", methods=["GET"])
@jwt_required()
def get_dashboard_summary(business_id):
    """Operational dashboard totals for one business -- today's and this
    (ISO, Monday-start) week's sale count/revenue.

    Computed entirely by PostgreSQL's own NOW()/date_trunc() -- never the
    app server's or a client's clock, same principle as this backend's
    OTP-expiry handling in routes/auth_routes.py. Exactly two fixed
    aggregate queries regardless of how much sale history exists --
    deliberately never selects individual sale rows the way
    GET .../sales does, so a business with years of history costs the
    same as one with none (no N+1, unlike list_sales()'s per-sale items
    query)."""
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

        today_row = conn.execute(
            text("""
                SELECT COUNT(*) AS sale_count,
                       COALESCE(SUM(total_amount), 0) AS total_amount,
                       NOW() AS as_of
                FROM pos_sales
                WHERE business_id = :business_id
                  AND created_at >= date_trunc('day', NOW())
            """),
            {"business_id": business_id},
        ).fetchone()
        today = dict(today_row._mapping)

        week_row = conn.execute(
            text("""
                SELECT COUNT(*) AS sale_count,
                       COALESCE(SUM(total_amount), 0) AS total_amount
                FROM pos_sales
                WHERE business_id = :business_id
                  AND created_at >= date_trunc('week', NOW())
            """),
            {"business_id": business_id},
        ).fetchone()
        week = dict(week_row._mapping)

        as_of = today["as_of"]
        return jsonify({
            "today": _dashboard_period_payload(today),
            "week": _dashboard_period_payload(week),
            "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        }), 200
    except Exception:
        logger.exception("get pos dashboard summary failed")
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
