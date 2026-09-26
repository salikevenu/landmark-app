"""POS Catalog Management V1 (Phase 16):
POST  /api/pos/businesses/<id>/products
PATCH /api/pos/businesses/<id>/products/<product_id>
GET   /api/pos/businesses/<id>/products?include_inactive=1

No real database connection -- pos_routes.get_db_connection is patched
with an in-memory fake, matching tests/test_pos_products.py's pattern.
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


class Store:
    def __init__(self):
        self.businesses = []
        self.products = []
        self.inventory = {}
        self.next_product_id = 1
        self.has_access = True
        self.commits = 0
        self.rollbacks = 0

    def add_business(self, business_id, owner_user_id):
        self.businesses.append({"id": business_id, "owner_user_id": owner_user_id})

    def add_product(self, business_id, name, price, is_active=1):
        row = {
            "id": self.next_product_id,
            "business_id": business_id,
            "name": name,
            "price": price,
            "is_active": is_active,
            "created_at": datetime(2026, 9, 26) + timedelta(seconds=self.next_product_id),
        }
        self.products.append(row)
        self.next_product_id += 1
        return row

    def product(self, product_id):
        return next((p for p in self.products if p["id"] == product_id), None)


def _public(row):
    return {k: row[k] for k in ("id", "name", "price", "is_active", "created_at")}


class FakeConn:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        p = params or {}
        s = self.store

        if q.startswith("select plan, subscription_expiry from users"):
            if s.has_access:
                return FakeResult(row=FakeRow({"plan": "business_power", "subscription_expiry": "2099-01-01"}))
            return FakeResult(row=FakeRow({"plan": "free", "subscription_expiry": None}))

        if "from pos_subscriptions" in q:
            return FakeResult(row=None)

        if q.startswith("select id from pos_businesses"):
            row = next(
                (b for b in s.businesses if b["id"] == p.get("business_id") and b["owner_user_id"] == p.get("uid")),
                None,
            )
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("select id from pos_products") and "lower(name)" in q:
            exclude = p.get("exclude_id")
            hit = next(
                (
                    r for r in s.products
                    if r["business_id"] == p["business_id"]
                    and r["is_active"] == 1
                    and r["name"].lower() == p["name"].lower()
                    and (exclude is None or r["id"] != exclude)
                ),
                None,
            )
            return FakeResult(row=FakeRow({"id": hit["id"]}) if hit else None)

        if q.startswith("insert into pos_products"):
            row = s.add_product(p["business_id"], p["name"], p["price"])
            return FakeResult(row=FakeRow(_public(row)))

        if q.startswith("insert into pos_inventory"):
            pid = p["product_id"]
            if "excluded.quantity," in q.replace(" ", "") or "quantity = excluded.quantity" in q:
                s.inventory[pid] = p["quantity"]
            else:
                s.inventory[pid] = s.inventory.get(pid, 0) + p["quantity"]
            return FakeResult(row=FakeRow({"quantity": s.inventory[pid]}))

        if q.startswith("select id, name, price, is_active, created_at from pos_products where id ="):
            row = s.product(p["product_id"])
            if row is None or row["business_id"] != p["business_id"]:
                return FakeResult(row=None)
            return FakeResult(row=FakeRow(_public(row)))

        if q.startswith("update pos_products"):
            row = s.product(p["product_id"])
            row.update(name=p["name"], price=p["price"], is_active=p["is_active"])
            return FakeResult(row=FakeRow(_public(row)))

        if q.startswith("select id, name, price, is_active, created_at from pos_products where business_id"):
            rows = [r for r in s.products if r["business_id"] == p["business_id"]]
            if "is_active = 1" in q:
                rows = [r for r in rows if r["is_active"] == 1]
            rows.sort(key=lambda r: r["id"])
            return FakeResult(rows=[FakeRow(_public(r)) for r in rows])

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        self.store.commits += 1

    def rollback(self):
        self.store.rollbacks += 1

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


class ProductManagementTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.add_business(1, owner_user_id=1)
        self.store.add_business(2, owner_user_id=2)
        self.app = _make_app()
        self.client = self.app.test_client()
        patcher = patch("routes.pos_routes.get_db_connection", lambda: FakeConn(self.store))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _h(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _create(self, body, uid=1, business_id=1):
        return self.client.post(f"/api/pos/businesses/{business_id}/products", json=body, headers=self._h(uid))

    def _patch(self, product_id, body, uid=1, business_id=1):
        return self.client.patch(
            f"/api/pos/businesses/{business_id}/products/{product_id}", json=body, headers=self._h(uid)
        )

    # ---------------- create ----------------
    def test_create_requires_auth(self):
        res = self.client.post("/api/pos/businesses/1/products", json={"name": "Tea", "price": 1000})
        self.assertEqual(res.status_code, 401)

    def test_create_returns_201_with_the_product(self):
        res = self._create({"name": "  Masala   Tea ", "price": 1500})
        self.assertEqual(res.status_code, 201)
        product = res.get_json()["product"]
        self.assertEqual(product["name"], "Masala Tea")  # trimmed, inner spaces collapsed
        self.assertEqual(product["price"], 1500)
        self.assertIs(product["is_active"], True)
        self.assertIn("created_at", product)
        self.assertEqual(self.store.commits, 1)

    def test_create_without_opening_stock_stays_untracked(self):
        res = self._create({"name": "Tea", "price": 1000})
        pid = res.get_json()["product"]["id"]
        self.assertNotIn(pid, self.store.inventory)

    def test_create_with_opening_stock_starts_tracking(self):
        res = self._create({"name": "Tea", "price": 1000, "opening_stock": 25})
        pid = res.get_json()["product"]["id"]
        self.assertEqual(self.store.inventory[pid], 25)

    def test_create_with_zero_opening_stock_tracks_at_zero(self):
        res = self._create({"name": "Tea", "price": 1000, "opening_stock": 0})
        pid = res.get_json()["product"]["id"]
        self.assertEqual(self.store.inventory[pid], 0)

    def test_create_allows_free_items(self):
        res = self._create({"name": "Water", "price": 0})
        self.assertEqual(res.status_code, 201)

    def test_create_rejects_bad_names(self):
        for body in ({"price": 100}, {"name": "", "price": 100}, {"name": "   ", "price": 100},
                     {"name": 5, "price": 100}, {"name": "x" * 101, "price": 100}):
            with self.subTest(body=body):
                self.assertEqual(self._create(body).status_code, 400)
        self.assertEqual(self.store.products, [])

    def test_create_rejects_bad_prices(self):
        for price in (None, -1, 12.5, "100", True, 2_000_000_001):
            with self.subTest(price=price):
                self.assertEqual(self._create({"name": "Tea", "price": price}).status_code, 400)
        self.assertEqual(self.store.products, [])

    def test_create_rejects_bad_opening_stock(self):
        for stock in (-1, 1.5, "3", True):
            with self.subTest(stock=stock):
                self.assertEqual(self._create({"name": "Tea", "price": 100, "opening_stock": stock}).status_code, 400)

    def test_create_rejects_non_object_body(self):
        res = self.client.post("/api/pos/businesses/1/products", json=["Tea"], headers=self._h())
        self.assertEqual(res.status_code, 400)

    def test_create_duplicate_active_name_is_409_case_insensitive(self):
        self.store.add_product(1, "Tea", 1000)
        res = self._create({"name": "TEA", "price": 1200})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(len(self.store.products), 1)

    def test_create_same_name_as_inactive_product_is_allowed(self):
        self.store.add_product(1, "Tea", 1000, is_active=0)
        self.assertEqual(self._create({"name": "Tea", "price": 1200}).status_code, 201)

    def test_create_same_name_in_another_business_is_allowed(self):
        self.store.add_product(2, "Tea", 1000)
        self.assertEqual(self._create({"name": "Tea", "price": 1200}).status_code, 201)

    def test_create_in_someone_elses_business_is_404(self):
        res = self._create({"name": "Tea", "price": 1000}, uid=1, business_id=2)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json(), {"success": False, "error": "Business not found"})
        self.assertEqual(self.store.products, [])

    def test_create_without_pos_access_is_403(self):
        self.store.has_access = False
        res = self._create({"name": "Tea", "price": 1000})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.store.products, [])

    # ---------------- update ----------------
    def test_update_price_and_name(self):
        product = self.store.add_product(1, "Tea", 1000)
        res = self._patch(product["id"], {"name": "Ginger Tea", "price": 1800})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()["product"]
        self.assertEqual(body["name"], "Ginger Tea")
        self.assertEqual(body["price"], 1800)
        self.assertIs(body["is_active"], True)

    def test_update_only_price_keeps_name(self):
        product = self.store.add_product(1, "Tea", 1000)
        body = self._patch(product["id"], {"price": 1100}).get_json()["product"]
        self.assertEqual(body["name"], "Tea")
        self.assertEqual(body["price"], 1100)

    def test_deactivate_and_reactivate(self):
        product = self.store.add_product(1, "Tea", 1000)
        res = self._patch(product["id"], {"is_active": False})
        self.assertEqual(res.status_code, 200)
        self.assertIs(res.get_json()["product"]["is_active"], False)
        self.assertEqual(self.store.product(product["id"])["is_active"], 0)

        res = self._patch(product["id"], {"is_active": True})
        self.assertIs(res.get_json()["product"]["is_active"], True)

    def test_reactivating_into_a_taken_name_is_409(self):
        old = self.store.add_product(1, "Tea", 1000, is_active=0)
        self.store.add_product(1, "tea", 1200)
        res = self._patch(old["id"], {"is_active": True})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.product(old["id"])["is_active"], 0)

    def test_renaming_to_own_name_in_different_case_is_allowed(self):
        product = self.store.add_product(1, "tea", 1000)
        self.assertEqual(self._patch(product["id"], {"name": "Tea"}).status_code, 200)

    def test_renaming_to_another_active_products_name_is_409(self):
        self.store.add_product(1, "Tea", 1000)
        coffee = self.store.add_product(1, "Coffee", 1500)
        self.assertEqual(self._patch(coffee["id"], {"name": "TEA"}).status_code, 409)

    def test_update_rejects_empty_or_bad_bodies(self):
        product = self.store.add_product(1, "Tea", 1000)
        for body in ({}, {"colour": "red"}, {"price": -5}, {"price": 9.5}, {"name": ""}, {"is_active": "no"}, {"is_active": 0}):
            with self.subTest(body=body):
                self.assertEqual(self._patch(product["id"], body).status_code, 400)
        self.assertEqual(self.store.product(product["id"])["price"], 1000)

    def test_update_unknown_product_is_404(self):
        self.assertEqual(self._patch(999, {"price": 100}).status_code, 404)

    def test_update_product_of_another_business_is_404(self):
        other = self.store.add_product(2, "Tea", 1000)
        res = self._patch(other["id"], {"price": 1}, uid=1, business_id=1)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.store.product(other["id"])["price"], 1000)

    def test_update_in_someone_elses_business_is_404(self):
        other = self.store.add_product(2, "Tea", 1000)
        res = self._patch(other["id"], {"price": 1}, uid=1, business_id=2)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json(), {"success": False, "error": "Business not found"})

    def test_update_without_pos_access_is_403(self):
        product = self.store.add_product(1, "Tea", 1000)
        self.store.has_access = False
        self.assertEqual(self._patch(product["id"], {"price": 1}).status_code, 403)

    # ---------------- list ----------------
    def test_list_default_hides_inactive_products(self):
        self.store.add_product(1, "Tea", 1000)
        self.store.add_product(1, "Old", 500, is_active=0)
        res = self.client.get("/api/pos/businesses/1/products", headers=self._h())
        self.assertEqual([p["name"] for p in res.get_json()["products"]], ["Tea"])

    def test_list_include_inactive_returns_all_with_flags(self):
        self.store.add_product(1, "Tea", 1000)
        self.store.add_product(1, "Old", 500, is_active=0)
        res = self.client.get("/api/pos/businesses/1/products?include_inactive=1", headers=self._h())
        products = res.get_json()["products"]
        self.assertEqual([(p["name"], p["is_active"]) for p in products], [("Tea", True), ("Old", False)])

    def test_list_include_inactive_other_values_are_ignored(self):
        self.store.add_product(1, "Old", 500, is_active=0)
        res = self.client.get("/api/pos/businesses/1/products?include_inactive=maybe", headers=self._h())
        self.assertEqual(res.get_json()["products"], [])


if __name__ == "__main__":
    unittest.main()
