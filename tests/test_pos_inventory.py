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


class PosInventoryStore:
    def __init__(self):
        self.by_product_id = {}

    def set_quantity(self, product_id, quantity):
        self.by_product_id[product_id] = quantity

    def quantity_for(self, product_id):
        return self.by_product_id.get(product_id, 0)


class FakeConn:
    def __init__(self, businesses, products, inventory):
        self.businesses = businesses
        self.products = products
        self.inventory = inventory

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

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
                }
                for p in products
            ]
            return FakeResult(rows=[FakeRow(r) for r in rows])

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

    def test_product_with_no_inventory_row_reports_zero_quantity(self):
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


if __name__ == "__main__":
    unittest.main()
