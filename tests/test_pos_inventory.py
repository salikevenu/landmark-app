"""POS inventory read: GET /api/pos/businesses/<id>/inventory
(routes/pos_routes.py).

No real database connection — pos_routes.get_db_connection is patched with
an in-memory fake, matching tests/test_pos_products.py's pattern.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from routes.pos_routes import pos_bp


class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class FakeResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class PosBusinessStore:
    def __init__(self):
        self.rows = []
        self.next_id = 1

    def create(self, owner_user_id, name):
        row = {
            "id": self.next_id,
            "owner_user_id": owner_user_id,
            "name": name,
            "created_at": datetime(2026, 9, 4) + timedelta(seconds=self.next_id),
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def owned_by(self, business_id, owner_user_id):
        return next(
            (
                r
                for r in self.rows
                if r["id"] == business_id and r["owner_user_id"] == owner_user_id
            ),
            None,
        )


class PosProductStore:
    def __init__(self):
        self.rows = []
        self.next_id = 1

    def add(self, business_id, name, price, is_active=1):
        row = {
            "id": self.next_id,
            "business_id": business_id,
            "name": name,
            "price": price,
            "is_active": is_active,
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def active_for_business(self, business_id):
        return sorted(
            (
                r
                for r in self.rows
                if r["business_id"] == business_id and r["is_active"] == 1
            ),
            key=lambda r: r["id"],
        )

    def owned_active(self, business_id, product_id):
        return next(
            (
                r
                for r in self.rows
                if r["id"] == product_id and r["business_id"] == business_id and r["is_active"] == 1
            ),
            None,
        )


class PosInventoryStore:
    def __init__(self):
        self.by_product_id = {}

    def set_quantity(self, product_id, quantity):
        self.by_product_id[product_id] = quantity

    def quantity_for(self, product_id):
        return self.by_product_id.get(product_id, 0)

    def is_tracked(self, product_id):
        """Whether a pos_inventory row exists at all -- independent of
        quantity, matching the real table's product_id-PK LEFT JOIN
        semantics (see routes.pos_routes.list_inventory)."""
        return product_id in self.by_product_id

    def upsert(self, product_id, quantity, set_absolute):
        """Mirrors the real `INSERT ... ON CONFLICT (product_id) DO
        UPDATE` in routes.pos_routes._upsert_inventory_quantity: creates
        the row (starting tracking) if absent, otherwise either sets the
        quantity to exactly `quantity` (adjust) or adds `quantity` to the
        existing value (receive). Returns the resulting quantity."""
        current = self.by_product_id.get(product_id, 0)
        new_quantity = quantity if set_absolute else current + quantity
        self.by_product_id[product_id] = new_quantity
        return new_quantity


class FakeConn:
    def __init__(self, businesses, products, inventory):
        self.businesses = businesses
        self.products = products
        self.inventory = inventory

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        # Phase B entitlement gate: every fake user is Business Power by
        # default, so these inventory tests (predating POS subscriptions)
        # keep exercising inventory behavior without their own
        # entitlement setup.
        if q.startswith("select plan, subscription_expiry from users"):
            return FakeResult(row=FakeRow({"plan": "business_power", "subscription_expiry": "2099-01-01"}))

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if "from pos_products p" in q and "left join pos_inventory" in q:
            products = self.products.active_for_business(params.get("business_id"))
            rows = [
                {
                    "product_id": p["id"],
                    "product_name": p["name"],
                    "quantity": self.inventory.quantity_for(p["id"]),
                    # Mirrors the real query's `i.product_id AS
                    # inventory_product_id` -- present (non-None) only when
                    # a pos_inventory row actually exists for this product.
                    "inventory_product_id": (
                        p["id"] if self.inventory.is_tracked(p["id"]) else None
                    ),
                }
                for p in products
            ]
            return FakeResult(rows=[FakeRow(r) for r in rows])

        # _load_owned_active_product (receive/adjust product ownership check).
        if q.startswith("select id, name from pos_products"):
            row = self.products.owned_active(params.get("business_id"), params.get("product_id"))
            return FakeResult(row=FakeRow({"id": row["id"], "name": row["name"]}) if row else None)

        # _upsert_inventory_quantity's INSERT ... ON CONFLICT DO UPDATE.
        if q.startswith("insert into pos_inventory"):
            set_absolute = "quantity = excluded.quantity," in q
            new_quantity = self.inventory.upsert(
                params["product_id"], params["quantity"], set_absolute
            )
            return FakeResult(row=FakeRow({"quantity": new_quantity}))

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        return None

    def close(self):
        return None


def _make_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
        JWT_TOKEN_LOCATION=["headers"],
        TESTING=True,
    )
    JWTManager(app)
    app.register_blueprint(pos_bp, url_prefix="/api/pos")
    return app


class PosInventoryTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.products = PosProductStore()
        self.inventory = PosInventoryStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(self.businesses, self.products, self.inventory),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _auth_headers_raw_identity(self, raw_identity):
        with self.app.app_context():
            token = create_access_token(identity=raw_identity)
        return {"Authorization": f"Bearer {token}"}

    def test_unauthenticated_request_is_rejected(self):
        res = self.client.get("/api/pos/businesses/1/inventory")
        self.assertEqual(res.status_code, 401)

    def test_owner_sees_stocked_product(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)
        self.inventory.set_quantity(product["id"], 25)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/inventory",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 200)
        items = res.get_json()["inventory"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["product_id"], product["id"])
        self.assertEqual(item["product_name"], "Widget")
        self.assertEqual(item["quantity"], 25)
        self.assertIsInstance(item["quantity"], int)
        # Phase 5: tracked (a real pos_inventory row exists), independent
        # of quantity.
        self.assertIs(item["is_tracked"], True)

    def test_tracked_product_with_zero_quantity_reports_is_tracked_true(self):
        """A real pos_inventory row with quantity 0 (e.g. fully sold out)
        must still be reported as tracked -- this is the exact case Phase
        4 found indistinguishable from an untracked product before
        is_tracked existed."""
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Sold Out Widget", 1000)
        self.inventory.set_quantity(product["id"], 0)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/inventory",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 200)
        items = res.get_json()["inventory"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quantity"], 0)
        self.assertIs(items[0]["is_tracked"], True)

    def test_product_with_no_inventory_row_reports_zero_quantity_and_untracked(self):
        business = self.businesses.create(1, "Shop A")
        self.products.add(business["id"], "Never Stocked", 500)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/inventory",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 200)
        items = res.get_json()["inventory"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quantity"], 0)
        self.assertIs(items[0]["is_tracked"], False)

    def test_mixed_tracked_untracked_and_zero_products_report_correctly_and_without_duplicates(
        self,
    ):
        """One query covering all three states side by side -- also
        verifies the LEFT JOIN never duplicates a product row (each
        product_id appears exactly once, matching pos_inventory.product_id
        being the table's primary key: at most one inventory row per
        product)."""
        business = self.businesses.create(1, "Shop A")
        stocked = self.products.add(business["id"], "Stocked", 100)
        sold_out = self.products.add(business["id"], "Sold Out", 100)
        never_stocked = self.products.add(business["id"], "Never Stocked", 100)
        self.inventory.set_quantity(stocked["id"], 7)
        self.inventory.set_quantity(sold_out["id"], 0)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/inventory",
            headers=self._auth_headers(1),
        )

        items = res.get_json()["inventory"]
        by_id = {item["product_id"]: item for item in items}
        self.assertEqual(len(items), 3)
        self.assertEqual(len(by_id), 3)  # no duplicate product_id entries

        self.assertEqual(by_id[stocked["id"]]["quantity"], 7)
        self.assertIs(by_id[stocked["id"]]["is_tracked"], True)

        self.assertEqual(by_id[sold_out["id"]]["quantity"], 0)
        self.assertIs(by_id[sold_out["id"]]["is_tracked"], True)

        self.assertEqual(by_id[never_stocked["id"]]["quantity"], 0)
        self.assertIs(by_id[never_stocked["id"]]["is_tracked"], False)

    def test_inventory_isolated_per_business(self):
        """Two businesses owned by the same user must never see each
        other's inventory, tracked state included."""
        shop_a = self.businesses.create(1, "Shop A")
        shop_b = self.businesses.create(1, "Shop B")
        product_a = self.products.add(shop_a["id"], "A Widget", 100)
        product_b = self.products.add(shop_b["id"], "B Widget", 100)
        self.inventory.set_quantity(product_a["id"], 3)
        # product_b is deliberately left untracked.

        res_a = self.client.get(
            f"/api/pos/businesses/{shop_a['id']}/inventory",
            headers=self._auth_headers(1),
        )
        res_b = self.client.get(
            f"/api/pos/businesses/{shop_b['id']}/inventory",
            headers=self._auth_headers(1),
        )

        items_a = res_a.get_json()["inventory"]
        items_b = res_b.get_json()["inventory"]
        self.assertEqual(len(items_a), 1)
        self.assertEqual(items_a[0]["product_id"], product_a["id"])
        self.assertEqual(items_a[0]["quantity"], 3)
        self.assertIs(items_a[0]["is_tracked"], True)

        self.assertEqual(len(items_b), 1)
        self.assertEqual(items_b[0]["product_id"], product_b["id"])
        self.assertEqual(items_b[0]["quantity"], 0)
        self.assertIs(items_b[0]["is_tracked"], False)

    def test_empty_catalog_returns_empty_inventory(self):
        business = self.businesses.create(1, "Shop A")

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/inventory",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"inventory": []})

    def test_inactive_product_excluded_from_inventory(self):
        business = self.businesses.create(1, "Shop A")
        active = self.products.add(business["id"], "Active Widget", 100, is_active=1)
        inactive = self.products.add(business["id"], "Discontinued", 100, is_active=0)
        self.inventory.set_quantity(active["id"], 5)
        self.inventory.set_quantity(inactive["id"], 99)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/inventory",
            headers=self._auth_headers(1),
        )

        items = res.get_json()["inventory"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["product_name"], "Active Widget")

    def test_another_users_business_returns_404_with_no_inventory_data(self):
        business = self.businesses.create(1, "User1 Shop")
        product = self.products.add(business["id"], "Secret Widget", 100)
        self.inventory.set_quantity(product["id"], 10)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/inventory",
            headers=self._auth_headers(2),
        )

        self.assertEqual(res.status_code, 404)
        body = res.get_json()
        self.assertNotIn("inventory", body)

    def test_nonexistent_business_returns_404(self):
        res = self.client.get(
            "/api/pos/businesses/999/inventory", headers=self._auth_headers(1)
        )
        self.assertEqual(res.status_code, 404)

    def test_non_integer_business_id_returns_404(self):
        res = self.client.get(
            "/api/pos/businesses/not-a-number/inventory", headers=self._auth_headers(1)
        )
        self.assertEqual(res.status_code, 404)


class PosInventoryReceiveAdjustTests(unittest.TestCase):
    """POST .../inventory/<product_id>/receive and .../adjust (Phase 6).
    Entitlement denial (403) is exercised centrally in
    tests/test_pos_entitlement_enforcement.py, matching every other
    business-scoped route -- not duplicated here."""

    def setUp(self):
        self.businesses = PosBusinessStore()
        self.products = PosProductStore()
        self.inventory = PosInventoryStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(self.businesses, self.products, self.inventory),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _auth_headers_raw_identity(self, raw_identity):
        with self.app.app_context():
            token = create_access_token(identity=raw_identity)
        return {"Authorization": f"Bearer {token}"}

    def _receive(self, business_id, product_id, body, uid=1, headers=None):
        return self.client.post(
            f"/api/pos/businesses/{business_id}/inventory/{product_id}/receive",
            json=body,
            headers=headers if headers is not None else self._auth_headers(uid),
        )

    def _adjust(self, business_id, product_id, body, uid=1, headers=None):
        return self.client.post(
            f"/api/pos/businesses/{business_id}/inventory/{product_id}/adjust",
            json=body,
            headers=headers if headers is not None else self._auth_headers(uid),
        )

    # ---- AUTH ----

    def test_receive_missing_jwt_returns_401(self):
        res = self.client.post(
            "/api/pos/businesses/1/inventory/1/receive", json={"quantity": 5}
        )
        self.assertEqual(res.status_code, 401)

    def test_adjust_missing_jwt_returns_401(self):
        res = self.client.post(
            "/api/pos/businesses/1/inventory/1/adjust", json={"quantity": 5}
        )
        self.assertEqual(res.status_code, 401)

    def test_receive_invalid_identity_returns_401(self):
        res = self._receive(
            1, 1, {"quantity": 5}, headers=self._auth_headers_raw_identity("not-a-number")
        )
        self.assertEqual(res.status_code, 401)

    def test_adjust_invalid_identity_returns_401(self):
        res = self._adjust(
            1, 1, {"quantity": 5}, headers=self._auth_headers_raw_identity("0")
        )
        self.assertEqual(res.status_code, 401)

    # ---- OWNERSHIP ----

    def test_receive_nonexistent_business_returns_404(self):
        res = self._receive(999, 1, {"quantity": 5})
        self.assertEqual(res.status_code, 404)

    def test_adjust_nonexistent_business_returns_404(self):
        res = self._adjust(999, 1, {"quantity": 5})
        self.assertEqual(res.status_code, 404)

    def test_receive_business_owned_by_another_user_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._receive(business["id"], product["id"], {"quantity": 5}, uid=2)

        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json(), {"success": False, "error": "Business not found"})

    def test_adjust_business_owned_by_another_user_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._adjust(business["id"], product["id"], {"quantity": 5}, uid=2)

        self.assertEqual(res.status_code, 404)

    # ---- PRODUCT ISOLATION ----

    def test_receive_rejects_product_belonging_to_a_different_business(self):
        shop_a = self.businesses.create(1, "Shop A")
        shop_b = self.businesses.create(1, "Shop B")
        product_b = self.products.add(shop_b["id"], "B Widget", 100)

        res = self._receive(shop_a["id"], product_b["id"], {"quantity": 5})

        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json(), {"success": False, "error": "Product not found"})
        # No inventory row was created for the mismatched product.
        self.assertFalse(self.inventory.is_tracked(product_b["id"]))

    def test_adjust_rejects_product_belonging_to_a_different_business(self):
        shop_a = self.businesses.create(1, "Shop A")
        shop_b = self.businesses.create(1, "Shop B")
        product_b = self.products.add(shop_b["id"], "B Widget", 100)

        res = self._adjust(shop_a["id"], product_b["id"], {"quantity": 25})

        self.assertEqual(res.status_code, 404)
        self.assertFalse(self.inventory.is_tracked(product_b["id"]))

    def test_receive_nonexistent_product_returns_404(self):
        business = self.businesses.create(1, "Shop A")

        res = self._receive(business["id"], 999, {"quantity": 5})

        self.assertEqual(res.status_code, 404)

    def test_receive_inactive_product_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Discontinued", 100, is_active=0)

        res = self._receive(business["id"], product["id"], {"quantity": 5})

        self.assertEqual(res.status_code, 404)

    # ---- RECEIVE ----

    def test_receive_on_untracked_product_creates_tracking(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)
        self.assertFalse(self.inventory.is_tracked(product["id"]))

        res = self._receive(business["id"], product["id"], {"quantity": 10})

        self.assertEqual(res.status_code, 200)
        item = res.get_json()["inventory_item"]
        self.assertEqual(item["product_id"], product["id"])
        self.assertEqual(item["product_name"], "Widget")
        self.assertEqual(item["quantity"], 10)
        self.assertIs(item["is_tracked"], True)
        self.assertTrue(self.inventory.is_tracked(product["id"]))

    def test_receive_on_tracked_product_increments_existing_quantity(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)
        self.inventory.set_quantity(product["id"], 5)

        res = self._receive(business["id"], product["id"], {"quantity": 10})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["inventory_item"]["quantity"], 15)

    def test_receive_zero_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._receive(business["id"], product["id"], {"quantity": 0})

        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.inventory.is_tracked(product["id"]))

    def test_receive_negative_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._receive(business["id"], product["id"], {"quantity": -5})

        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.inventory.is_tracked(product["id"]))

    def test_receive_float_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._receive(business["id"], product["id"], {"quantity": 5.5})

        self.assertEqual(res.status_code, 400)

    def test_receive_string_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._receive(business["id"], product["id"], {"quantity": "10"})

        self.assertEqual(res.status_code, 400)

    def test_receive_bool_rejected(self):
        """bool is a subclass of int in Python -- {"quantity": true} must
        not be silently accepted as 1, matching create_sale's own item
        quantity check."""
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._receive(business["id"], product["id"], {"quantity": True})

        self.assertEqual(res.status_code, 400)

    def test_receive_missing_quantity_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._receive(business["id"], product["id"], {})

        self.assertEqual(res.status_code, 400)

    def test_receive_malformed_body_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self.client.post(
            f"/api/pos/businesses/{business['id']}/inventory/{product['id']}/receive",
            data="not json",
            content_type="application/json",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 400)

    def test_receive_excessively_large_quantity_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._receive(business["id"], product["id"], {"quantity": 1_000_001})

        self.assertEqual(res.status_code, 400)

    # ---- ADJUST ----

    def test_adjust_sets_absolute_quantity(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)
        self.inventory.set_quantity(product["id"], 5)

        res = self._adjust(business["id"], product["id"], {"quantity": 25})

        self.assertEqual(res.status_code, 200)
        item = res.get_json()["inventory_item"]
        self.assertEqual(item["quantity"], 25)
        self.assertIs(item["is_tracked"], True)

    def test_adjust_on_untracked_product_creates_tracking(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._adjust(business["id"], product["id"], {"quantity": 12})

        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.inventory.is_tracked(product["id"]))
        self.assertEqual(self.inventory.quantity_for(product["id"]), 12)

    def test_adjust_to_zero_allowed_and_remains_tracked(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)
        self.inventory.set_quantity(product["id"], 8)

        res = self._adjust(business["id"], product["id"], {"quantity": 0})

        self.assertEqual(res.status_code, 200)
        item = res.get_json()["inventory_item"]
        self.assertEqual(item["quantity"], 0)
        self.assertIs(item["is_tracked"], True)

    def test_adjust_negative_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)
        self.inventory.set_quantity(product["id"], 8)

        res = self._adjust(business["id"], product["id"], {"quantity": -1})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.inventory.quantity_for(product["id"]), 8)

    def test_adjust_float_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._adjust(business["id"], product["id"], {"quantity": 3.2})

        self.assertEqual(res.status_code, 400)

    def test_adjust_string_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._adjust(business["id"], product["id"], {"quantity": "25"})

        self.assertEqual(res.status_code, 400)

    def test_adjust_bool_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._adjust(business["id"], product["id"], {"quantity": False})

        self.assertEqual(res.status_code, 400)

    def test_adjust_malformed_body_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self.client.post(
            f"/api/pos/businesses/{business['id']}/inventory/{product['id']}/adjust",
            data="not json",
            content_type="application/json",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 400)

    def test_adjust_excessively_large_quantity_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        res = self._adjust(business["id"], product["id"], {"quantity": 1_000_001})

        self.assertEqual(res.status_code, 400)

    # ---- SAFETY ----

    def test_repeated_receive_produces_correct_cumulative_quantity(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        self._receive(business["id"], product["id"], {"quantity": 10})
        self._receive(business["id"], product["id"], {"quantity": 5})
        res = self._receive(business["id"], product["id"], {"quantity": 2})

        self.assertEqual(res.get_json()["inventory_item"]["quantity"], 17)

    def test_receive_never_creates_a_duplicate_inventory_row(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)

        self._receive(business["id"], product["id"], {"quantity": 10})
        self._receive(business["id"], product["id"], {"quantity": 5})

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/inventory", headers=self._auth_headers(1)
        )
        items = res.get_json()["inventory"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quantity"], 15)

    def test_receiving_stock_for_one_business_does_not_affect_another(self):
        shop_a = self.businesses.create(1, "Shop A")
        shop_b = self.businesses.create(1, "Shop B")
        product_a = self.products.add(shop_a["id"], "A Widget", 100)
        product_b = self.products.add(shop_b["id"], "B Widget", 100)

        self._receive(shop_a["id"], product_a["id"], {"quantity": 10})

        res_b = self.client.get(
            f"/api/pos/businesses/{shop_b['id']}/inventory", headers=self._auth_headers(1)
        )
        items_b = res_b.get_json()["inventory"]
        self.assertEqual(len(items_b), 1)
        self.assertEqual(items_b[0]["product_id"], product_b["id"])
        self.assertIs(items_b[0]["is_tracked"], False)


if __name__ == "__main__":
    unittest.main()
