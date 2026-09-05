"""POS sales foundation: POST/GET /api/pos/businesses/<id>/sales
(routes/pos_routes.py).

No real database connection — pos_routes.get_db_connection is patched with
an in-memory fake, matching tests/test_pos_products.py's pattern. The fake
stages writes (a pending sale + pending items) and only applies them to
the committed store on `commit()`, discarding them on `rollback()` or an
unhandled exception — this is what lets the "no partial sale on failure"
test actually prove atomicity rather than just asserting a status code.
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
        row = {"id": self.next_id, "owner_user_id": owner_user_id, "name": name}
        self.rows.append(row)
        self.next_id += 1
        return row

    def owned_by(self, business_id, owner_user_id):
        return next(
            (r for r in self.rows if r["id"] == business_id and r["owner_user_id"] == owner_user_id),
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

    def find_sellable(self, product_id, business_id):
        return next(
            (
                r
                for r in self.rows
                if r["id"] == product_id and r["business_id"] == business_id and r["is_active"] == 1
            ),
            None,
        )

    def update_price(self, product_id, new_price):
        for r in self.rows:
            if r["id"] == product_id:
                r["price"] = new_price


class PosSalesStore:
    """Only the *committed* view — writes reach here exclusively through
    FakeConn.commit(), never directly from an INSERT execute() call."""

    def __init__(self):
        self.sales = []
        self.items = []
        self.next_sale_id = 1

    def commit_sale(self, sale, items):
        self.sales.append(dict(sale))
        self.items.extend(dict(i) for i in items)
        self.next_sale_id += 1

    def for_business(self, business_id):
        return sorted((s for s in self.sales if s["business_id"] == business_id), key=lambda s: s["id"])

    def items_for_sale(self, sale_id):
        return [i for i in self.items if i["sale_id"] == sale_id]


class FakeConn:
    def __init__(self, businesses, products, sales, fail_on_item_index=None):
        self.businesses = businesses
        self.products = products
        self.sales = sales
        self.fail_on_item_index = fail_on_item_index
        self._item_insert_count = 0
        self._pending_sale = None
        self._pending_items = []

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("select id, name, price from pos_products"):
            row = self.products.find_sellable(params.get("product_id"), params.get("business_id"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("insert into pos_sales"):
            self._pending_sale = {
                "id": self.sales.next_sale_id,
                "business_id": params["business_id"],
                "total_amount": params["total_amount"],
                "created_at": datetime(2026, 9, 4) + timedelta(seconds=self.sales.next_sale_id),
            }
            return FakeResult(row=FakeRow(dict(self._pending_sale)))

        if q.startswith("insert into pos_sale_items"):
            self._item_insert_count += 1
            if self.fail_on_item_index == self._item_insert_count:
                raise RuntimeError("simulated failure mid-insert")
            self._pending_items.append({
                "sale_id": params["sale_id"],
                "product_id": params["product_id"],
                "product_name": params["product_name"],
                "unit_price": params["unit_price"],
                "quantity": params["quantity"],
                "line_total": params["line_total"],
            })
            return FakeResult()

        if q.startswith("select id, total_amount, created_at from pos_sales"):
            rows = self.sales.for_business(params.get("business_id"))
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

        if q.startswith(
            "select product_id, product_name, unit_price, quantity, line_total from pos_sale_items"
        ):
            rows = self.sales.items_for_sale(params.get("sale_id"))
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        if self._pending_sale is not None:
            self.sales.commit_sale(self._pending_sale, self._pending_items)
        self._pending_sale = None
        self._pending_items = []

    def rollback(self):
        self._pending_sale = None
        self._pending_items = []

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


class PosSalesTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.products = PosProductStore()
        self.sales = PosSalesStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        self._fail_on_item_index = None
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(self.businesses, self.products, self.sales, self._fail_on_item_index),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _post(self, business_id, body, uid=1):
        return self.client.post(
            f"/api/pos/businesses/{business_id}/sales",
            json=body,
            headers=self._auth_headers(uid),
        )

    def _get(self, business_id, uid=1):
        return self.client.get(
            f"/api/pos/businesses/{business_id}/sales", headers=self._auth_headers(uid)
        )

    # ---- AUTH/ACCESS ----

    def test_post_unauthenticated_is_rejected(self):
        res = self.client.post("/api/pos/businesses/1/sales", json={"items": []})
        self.assertEqual(res.status_code, 401)

    def test_get_unauthenticated_is_rejected(self):
        res = self.client.get("/api/pos/businesses/1/sales")
        self.assertEqual(res.status_code, 401)

    def test_post_another_users_business_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 1}]}, uid=2)

        self.assertEqual(res.status_code, 404)

    def test_get_another_users_business_returns_404(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"], uid=2)

        self.assertEqual(res.status_code, 404)

    def test_post_nonexistent_business_returns_404(self):
        res = self._post(999, {"items": [{"product_id": 1, "quantity": 1}]})
        self.assertEqual(res.status_code, 404)

    def test_get_nonexistent_business_returns_404(self):
        res = self._get(999)
        self.assertEqual(res.status_code, 404)

    # ---- CREATE ----

    def test_successful_sale_with_correct_price_and_total(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1500)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 3}]})

        self.assertEqual(res.status_code, 201)
        sale = res.get_json()["sale"]
        self.assertEqual(sale["total_amount"], 4500)
        item = sale["items"][0]
        self.assertEqual(item["unit_price"], 1500)
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["line_total"], 4500)

    def test_snapshot_captures_product_name_and_price(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1500)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 1}]})

        item = res.get_json()["sale"]["items"][0]
        self.assertEqual(item["product_name"], "Widget")
        self.assertEqual(item["unit_price"], 1500)

    def test_multiple_products_sum_to_correct_total(self):
        business = self.businesses.create(1, "Shop A")
        a = self.products.add(business["id"], "A", 1000)
        b = self.products.add(business["id"], "B", 2500)

        res = self._post(
            business["id"],
            {"items": [
                {"product_id": a["id"], "quantity": 2},
                {"product_id": b["id"], "quantity": 1},
            ]},
        )

        sale = res.get_json()["sale"]
        self.assertEqual(sale["total_amount"], 4500)  # 1000*2 + 2500*1
        self.assertEqual(len(sale["items"]), 2)

    def test_empty_items_rejected(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(business["id"], {"items": []})

        self.assertEqual(res.status_code, 400)

    def test_missing_items_key_rejected(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(business["id"], {})

        self.assertEqual(res.status_code, 400)

    def test_malformed_item_entry_rejected(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(business["id"], {"items": ["not-an-object"]})

        self.assertEqual(res.status_code, 400)

    def test_non_integer_product_id_rejected(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(business["id"], {"items": [{"product_id": "1", "quantity": 1}]})

        self.assertEqual(res.status_code, 400)

    def test_zero_quantity_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 0}]})

        self.assertEqual(res.status_code, 400)

    def test_negative_quantity_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": -1}]})

        self.assertEqual(res.status_code, 400)

    def test_nonexistent_product_rejected(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(business["id"], {"items": [{"product_id": 999, "quantity": 1}]})

        self.assertEqual(res.status_code, 400)

    def test_product_from_another_business_rejected(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        product_b = self.products.add(business_b["id"], "B Widget", 100)

        res = self._post(business_a["id"], {"items": [{"product_id": product_b["id"], "quantity": 1}]})

        self.assertEqual(res.status_code, 400)

    def test_inactive_product_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Discontinued", 100, is_active=0)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 1}]})

        self.assertEqual(res.status_code, 400)

    def test_duplicate_product_id_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(
            business["id"],
            {"items": [
                {"product_id": product["id"], "quantity": 1},
                {"product_id": product["id"], "quantity": 2},
            ]},
        )

        self.assertEqual(res.status_code, 400)

    def test_client_supplied_price_and_total_are_ignored(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1500)

        res = self._post(
            business["id"],
            {
                "items": [{
                    "product_id": product["id"],
                    "quantity": 1,
                    "unit_price": 1,
                    "line_total": 1,
                }],
                "total_amount": 1,
            },
        )

        sale = res.get_json()["sale"]
        self.assertEqual(sale["total_amount"], 1500)
        self.assertEqual(sale["items"][0]["unit_price"], 1500)

    def test_no_partial_sale_when_an_item_insert_fails(self):
        business = self.businesses.create(1, "Shop A")
        a = self.products.add(business["id"], "A", 100)
        b = self.products.add(business["id"], "B", 200)
        self._fail_on_item_index = 2  # fail inserting the second item

        res = self._post(
            business["id"],
            {"items": [
                {"product_id": a["id"], "quantity": 1},
                {"product_id": b["id"], "quantity": 1},
            ]},
        )

        self.assertEqual(res.status_code, 500)
        self.assertEqual(self.sales.sales, [])
        self.assertEqual(self.sales.items, [])

    # ---- READ ----

    def test_sale_history_returns_nested_items(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)
        self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 2}]})

        res = self._get(business["id"])

        self.assertEqual(res.status_code, 200)
        sales = res.get_json()["sales"]
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["total_amount"], 2000)
        self.assertEqual(len(sales[0]["items"]), 1)
        self.assertEqual(sales[0]["items"][0]["product_name"], "Widget")

    def test_empty_sale_history(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"])

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"sales": []})

    def test_sale_history_is_business_isolated(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        product_a = self.products.add(business_a["id"], "A Widget", 100)
        product_b = self.products.add(business_b["id"], "B Widget", 200)
        self._post(business_a["id"], {"items": [{"product_id": product_a["id"], "quantity": 1}]})
        self._post(business_b["id"], {"items": [{"product_id": product_b["id"], "quantity": 1}]})

        res_a = self._get(business_a["id"])
        res_b = self._get(business_b["id"])

        self.assertEqual(len(res_a.get_json()["sales"]), 1)
        self.assertEqual(len(res_b.get_json()["sales"]), 1)
        self.assertEqual(res_a.get_json()["sales"][0]["items"][0]["product_name"], "A Widget")
        self.assertEqual(res_b.get_json()["sales"][0]["items"][0]["product_name"], "B Widget")

    def test_snapshot_remains_historical_after_the_product_changes(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 1000)
        self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 1}]})

        # Product is repriced/renamed after the sale.
        self.products.update_price(product["id"], 9999)
        self.products.rows[0]["name"] = "Renamed Widget"

        res = self._get(business["id"])

        item = res.get_json()["sales"][0]["items"][0]
        self.assertEqual(item["unit_price"], 1000)
        self.assertEqual(item["product_name"], "Widget")


if __name__ == "__main__":
    unittest.main()
