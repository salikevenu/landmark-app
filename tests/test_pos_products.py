"""POS product catalog: GET /api/pos/businesses/<id>/products
(routes/pos_routes.py).

No real database connection — pos_routes.get_db_connection is patched with
an in-memory fake, matching tests/test_pos_businesses.py's pattern.
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
            "created_at": datetime(2026, 9, 4) + timedelta(seconds=self.next_id),
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


class FakeConn:
    def __init__(self, businesses, products):
        self.businesses = businesses
        self.products = products

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("select id, name, created_at") and "from pos_businesses" in q:
            rows = [
                r for r in self.businesses.rows if r["owner_user_id"] == params.get("uid")
            ]
            rows.sort(key=lambda r: r["id"])
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

        if q.startswith("insert into pos_businesses"):
            row = self.businesses.create(params["uid"], params["name"])
            return FakeResult(row=FakeRow(dict(row)))

        if "from pos_products" in q:
            rows = self.products.active_for_business(params.get("business_id"))
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

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


class PosProductsTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.products = PosProductStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(self.businesses, self.products),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    # A. unauthenticated
    def test_unauthenticated_request_is_rejected(self):
        res = self.client.get("/api/pos/businesses/1/products")
        self.assertEqual(res.status_code, 401)

    # B. authenticated owner + populated catalog
    def test_owner_sees_populated_catalog(self):
        business = self.businesses.create(1, "Shop A")
        self.products.add(business["id"], "Widget", 15000)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/products",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 200)
        products = res.get_json()["products"]
        self.assertEqual(len(products), 1)
        product = products[0]
        self.assertEqual(product["name"], "Widget")
        self.assertEqual(product["price"], 15000)
        self.assertIsInstance(product["price"], int)
        self.assertIs(product["is_active"], True)
        self.assertIn("created_at", product)

    # C. authenticated owner + empty catalog
    def test_owner_sees_empty_catalog(self):
        business = self.businesses.create(1, "Shop A")

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/products",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"products": []})

    # D. another user's business (CRITICAL)
    def test_another_users_business_returns_404_with_no_product_data(self):
        business = self.businesses.create(1, "User1 Shop")
        self.products.add(business["id"], "Secret Widget", 99999)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/products",
            headers=self._auth_headers(2),
        )

        self.assertEqual(res.status_code, 404)
        body = res.get_json()
        self.assertNotIn("products", body)
        self.assertEqual(body, {"success": False, "error": "Business not found"})

    # E. nonexistent business — same shape/status as "not yours"
    def test_nonexistent_business_returns_404_same_shape_as_not_owned(self):
        res_nonexistent = self.client.get(
            "/api/pos/businesses/999/products", headers=self._auth_headers(1)
        )

        business = self.businesses.create(2, "User2 Shop")
        res_not_owned = self.client.get(
            f"/api/pos/businesses/{business['id']}/products",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res_nonexistent.status_code, 404)
        self.assertEqual(res_not_owned.status_code, 404)
        self.assertEqual(res_nonexistent.get_json(), res_not_owned.get_json())

    # F. non-integer business path segment
    def test_non_integer_business_id_returns_404(self):
        res = self.client.get(
            "/api/pos/businesses/not-a-number/products", headers=self._auth_headers(1)
        )
        self.assertEqual(res.status_code, 404)

    # G. inactive product exclusion
    def test_inactive_products_are_excluded_from_the_catalog(self):
        business = self.businesses.create(1, "Shop A")
        self.products.add(business["id"], "Active Widget", 1000, is_active=1)
        self.products.add(business["id"], "Discontinued Widget", 500, is_active=0)

        res = self.client.get(
            f"/api/pos/businesses/{business['id']}/products",
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 200)
        products = res.get_json()["products"]
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name"], "Active Widget")

    # H. multiple businesses owned by the same user
    def test_same_user_can_access_products_from_each_of_their_businesses(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        self.products.add(business_a["id"], "A Widget", 1000)
        self.products.add(business_b["id"], "B Widget", 2000)

        headers = self._auth_headers(1)
        res_a = self.client.get(f"/api/pos/businesses/{business_a['id']}/products", headers=headers)
        res_b = self.client.get(f"/api/pos/businesses/{business_b['id']}/products", headers=headers)

        self.assertEqual(res_a.status_code, 200)
        self.assertEqual(res_b.status_code, 200)
        self.assertEqual(res_a.get_json()["products"][0]["name"], "A Widget")
        self.assertEqual(res_b.get_json()["products"][0]["name"], "B Widget")


if __name__ == "__main__":
    unittest.main()
