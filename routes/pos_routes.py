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
from sqlalchemy.exc import IntegrityError

from database.init_db import get_db_connection
from routes.auth_routes import clean_phone, validate_phone

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
