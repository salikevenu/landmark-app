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

    def filtered_for_business(self, business_id, from_date=None, to_exclusive=None, payment_method=None):
        """Mirrors list_sales()'s WHERE clause -- from_date/to_exclusive
        are `date` objects (matching what routes/pos_routes.py parses
        them into), compared against created_at's own date, exactly like
        real Postgres comparing a TIMESTAMP column against DATE bounds."""
        def matches(sale):
            if sale["business_id"] != business_id:
                return False
            if from_date is not None and sale["created_at"].date() < from_date:
                return False
            if to_exclusive is not None and sale["created_at"].date() >= to_exclusive:
                return False
            if payment_method is not None and sale["payment_method"] != payment_method:
                return False
            return True

        return [s for s in self.sales if matches(s)]


class PosCustomerStore:
    """Phase 8: mirrors pos_customers for create_sale's customer
    ownership check and list_sales' LEFT JOIN."""

    def __init__(self):
        self.rows = []
        self.next_id = 1

    def add(self, business_id, name, phone):
        row = {
            "id": self.next_id,
            "business_id": business_id,
            "name": name,
            "phone": phone,
            "created_at": datetime(2026, 9, 1) + timedelta(seconds=self.next_id),
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def find(self, customer_id, business_id):
        return next(
            (
                r
                for r in self.rows
                if r["id"] == customer_id and r["business_id"] == business_id
            ),
            None,
        )


class PosInventoryStore:
    """Only the *committed* view, same staging discipline as
    PosSalesStore -- a deduction only lands here via FakeConn.commit(),
    so "no partial deduction when the sale fails" is something these
    tests can actually observe rather than assume. A product with no
    entry here is "untracked" (mirrors a real missing pos_inventory row):
    distinct from an entry of 0, which means tracked-and-out-of-stock."""

    def __init__(self):
        self.quantities = {}

    def track(self, product_id, quantity):
        self.quantities[product_id] = quantity

    def get(self, product_id):
        return self.quantities.get(product_id)

    def upsert(self, product_id, quantity, set_absolute):
        """Mirrors routes.pos_routes._upsert_inventory_quantity's `INSERT
        ... ON CONFLICT (product_id) DO UPDATE`. Applied immediately
        (unlike PosSalesStore's staged deductions) since receive/adjust
        here are only ever used as an up-front setup step before a sale,
        never something these tests need to observe mid-rollback."""
        current = self.quantities.get(product_id, 0)
        new_quantity = quantity if set_absolute else current + quantity
        self.quantities[product_id] = new_quantity
        return new_quantity


class FakeConn:
    def __init__(
        self, businesses, products, sales, inventory, fail_on_item_index=None, customers=None
    ):
        self.businesses = businesses
        self.products = products
        self.sales = sales
        self.inventory = inventory
        self.fail_on_item_index = fail_on_item_index
        self.customers = customers if customers is not None else PosCustomerStore()
        self._item_insert_count = 0
        self._pending_sale = None
        self._pending_items = []
        self._pending_inventory_deductions = []

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        # Phase B entitlement gate: every fake user is Business Power by
        # default, so these sales tests (predating POS subscriptions)
        # keep exercising sales behavior without their own entitlement
        # setup.
        if q.startswith("select plan, subscription_expiry from users"):
            return FakeResult(row=FakeRow({"plan": "business_power", "subscription_expiry": "2099-01-01"}))

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("select id, name, price from pos_products"):
            row = self.products.find_sellable(params.get("product_id"), params.get("business_id"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        # _load_owned_active_product (receive/adjust product ownership check).
        if q.startswith("select id, name from pos_products"):
            row = self.products.find_sellable(params.get("product_id"), params.get("business_id"))
            return FakeResult(row=FakeRow({"id": row["id"], "name": row["name"]}) if row else None)

        # _upsert_inventory_quantity's INSERT ... ON CONFLICT DO UPDATE
        # (receive/adjust) -- applied immediately, see PosInventoryStore.upsert.
        if q.startswith("insert into pos_inventory"):
            set_absolute = "quantity = excluded.quantity," in q
            new_quantity = self.inventory.upsert(
                params["product_id"], params["quantity"], set_absolute
            )
            return FakeResult(row=FakeRow({"quantity": new_quantity}))

        if q.startswith("select quantity from pos_inventory"):
            quantity = self.inventory.get(params.get("product_id"))
            if quantity is None:
                return FakeResult(row=None)
            return FakeResult(row=FakeRow({"quantity": quantity}))

        if q.startswith("update pos_inventory"):
            self._pending_inventory_deductions.append(
                (params["product_id"], params["quantity"])
            )
            return FakeResult()

        # Phase 8: create_sale's customer ownership check.
        if q.startswith("select id, name, phone, created_at from pos_customers"):
            row = self.customers.find(params.get("customer_id"), params.get("business_id"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("insert into pos_sales"):
            self._pending_sale = {
                "id": self.sales.next_sale_id,
                "business_id": params["business_id"],
                "total_amount": params["total_amount"],
                "payment_method": params["payment_method"],
                "customer_id": params.get("customer_id"),
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

        if q.startswith("select count(*) as total from pos_sales"):
            matching = self.sales.filtered_for_business(
                params.get("business_id"),
                from_date=params.get("from_date"),
                to_exclusive=params.get("to_exclusive"),
                payment_method=params.get("payment_method"),
            )
            return FakeResult(row=FakeRow({"total": len(matching)}))

        if q.startswith("select s.id, s.total_amount, s.payment_method, s.created_at,"):
            matching = self.sales.filtered_for_business(
                params.get("business_id"),
                from_date=params.get("from_date"),
                to_exclusive=params.get("to_exclusive"),
                payment_method=params.get("payment_method"),
            )
            ordered = sorted(matching, key=lambda s: (s["created_at"], s["id"]), reverse=True)
            limit = params.get("limit")
            offset = params.get("offset", 0) or 0
            page = ordered[offset:offset + limit] if limit is not None else ordered
            # Mirrors the real query's LEFT JOIN pos_customers -- same
            # bulk-per-page shape, never one lookup per sale.
            rows = []
            for sale in page:
                row = dict(sale)
                customer_id = sale.get("customer_id")
                customer = (
                    self.customers.find(customer_id, sale["business_id"])
                    if customer_id is not None
                    else None
                )
                row["customer_name"] = customer["name"] if customer else None
                row["customer_phone"] = customer["phone"] if customer else None
                row["customer_created_at"] = customer["created_at"] if customer else None
                rows.append(row)
            return FakeResult(rows=[FakeRow(r) for r in rows])

        if q.startswith(
            "select sale_id, product_id, product_name, unit_price, quantity, line_total "
            "from pos_sale_items"
        ):
            sale_ids = set(params.get("sale_ids") or [])
            # Stable sort -- items for the same sale already appear in
            # insertion order in self.sales.items, so sorting by sale_id
            # alone (Python's sort is stable) reproduces "ORDER BY
            # sale_id, id" without needing a fabricated item id.
            rows = sorted(
                (i for i in self.sales.items if i["sale_id"] in sale_ids),
                key=lambda i: i["sale_id"],
            )
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        if self._pending_sale is not None:
            self.sales.commit_sale(self._pending_sale, self._pending_items)
        for product_id, quantity in self._pending_inventory_deductions:
            current = self.inventory.get(product_id) or 0
            self.inventory.track(product_id, current - quantity)
        self._pending_sale = None
        self._pending_items = []
        self._pending_inventory_deductions = []

    def rollback(self):
        self._pending_sale = None
        self._pending_items = []
        self._pending_inventory_deductions = []

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
        self.inventory = PosInventoryStore()
        self.customers = PosCustomerStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        self._fail_on_item_index = None
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(
                self.businesses,
                self.products,
                self.sales,
                self.inventory,
                self._fail_on_item_index,
                customers=self.customers,
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _post(self, business_id, body, uid=1):
        # payment_method defaults to "cash" so the many pre-existing tests
        # below (which predate the payment-method requirement) don't each
        # need updating — a test exercising payment_method itself passes
        # its own key, which overrides this default.
        full_body = {"payment_method": "cash", **body}
        return self.client.post(
            f"/api/pos/businesses/{business_id}/sales",
            json=full_body,
            headers=self._auth_headers(uid),
        )

    def _get(self, business_id, uid=1):
        return self.client.get(
            f"/api/pos/businesses/{business_id}/sales", headers=self._auth_headers(uid)
        )

    def _receive(self, business_id, product_id, quantity, uid=1):
        return self.client.post(
            f"/api/pos/businesses/{business_id}/inventory/{product_id}/receive",
            json={"quantity": quantity},
            headers=self._auth_headers(uid),
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

    # ---- PAYMENT METHOD ----

    def test_missing_payment_method_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self.client.post(
            f"/api/pos/businesses/{business['id']}/sales",
            json={"items": [{"product_id": product["id"], "quantity": 1}]},
            headers=self._auth_headers(1),
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.sales.sales, [])

    def test_invalid_payment_method_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(
            business["id"],
            {"payment_method": "bitcoin", "items": [{"product_id": product["id"], "quantity": 1}]},
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.sales.sales, [])

    def test_each_supported_payment_method_succeeds_and_is_returned(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        for method in ("cash", "card", "upi"):
            res = self._post(
                business["id"],
                {"payment_method": method, "items": [{"product_id": product["id"], "quantity": 1}]},
            )
            self.assertEqual(res.status_code, 201, method)
            self.assertEqual(res.get_json()["sale"]["payment_method"], method)

    # ---- PHASE 8: CUSTOMER ASSOCIATION ----

    def test_create_sale_without_customer_id_omitted_is_valid_and_customer_is_null(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 1}]})

        self.assertEqual(res.status_code, 201)
        self.assertIsNone(res.get_json()["sale"]["customer"])

    def test_create_sale_with_customer_id_explicitly_null_is_valid(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(
            business["id"],
            {"customer_id": None, "items": [{"product_id": product["id"], "quantity": 1}]},
        )

        self.assertEqual(res.status_code, 201)
        self.assertIsNone(res.get_json()["sale"]["customer"])

    def test_create_sale_with_a_valid_same_business_customer_returns_customer_info(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)
        customer = self.customers.add(business["id"], "Alice", "9876543210")

        res = self._post(
            business["id"],
            {
                "customer_id": customer["id"],
                "items": [{"product_id": product["id"], "quantity": 1}],
            },
        )

        self.assertEqual(res.status_code, 201)
        sale_customer = res.get_json()["sale"]["customer"]
        self.assertEqual(sale_customer["id"], customer["id"])
        self.assertEqual(sale_customer["name"], "Alice")
        self.assertEqual(sale_customer["phone"], "9876543210")

    def test_create_sale_preserves_payment_method_and_inventory_atomicity_with_a_customer(self):
        """A customer attached to the sale must not change any existing
        behavior -- payment_method is still returned, and tracked stock
        is still deducted atomically."""
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)
        self.inventory.track(product["id"], 10)
        customer = self.customers.add(business["id"], "Alice", "9876543210")

        res = self._post(
            business["id"],
            {
                "payment_method": "upi",
                "customer_id": customer["id"],
                "items": [{"product_id": product["id"], "quantity": 3}],
            },
        )

        self.assertEqual(res.status_code, 201)
        sale = res.get_json()["sale"]
        self.assertEqual(sale["payment_method"], "upi")
        self.assertEqual(sale["customer"]["id"], customer["id"])
        self.assertEqual(self.inventory.get(product["id"]), 7)

    def test_create_sale_rejects_nonexistent_customer_id(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(
            business["id"],
            {"customer_id": 999, "items": [{"product_id": product["id"], "quantity": 1}]},
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.sales.sales, [])

    def test_create_sale_rejects_customer_from_another_business(self):
        """Cross-business customer ids must never be usable, and the
        rejection must read the same as a nonexistent customer -- never a
        signal that could confirm another business's customer id exists."""
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        product = self.products.add(business_a["id"], "Widget", 100)
        other_customer = self.customers.add(business_b["id"], "Bob", "9999999999")

        res = self._post(
            business_a["id"],
            {
                "customer_id": other_customer["id"],
                "items": [{"product_id": product["id"], "quantity": 1}],
            },
        )

        nonexistent_res = self._post(
            business_a["id"],
            {"customer_id": 424242, "items": [{"product_id": product["id"], "quantity": 1}]},
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json(), nonexistent_res.get_json())
        self.assertEqual(self.sales.sales, [])

    def test_create_sale_rejects_string_customer_id(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(
            business["id"],
            {"customer_id": "1", "items": [{"product_id": product["id"], "quantity": 1}]},
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.sales.sales, [])

    def test_create_sale_rejects_boolean_customer_id(self):
        """bool is a subclass of int in Python -- {"customer_id": true}
        must not be silently accepted as customer id 1."""
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)
        self.customers.add(business["id"], "Alice", "9876543210")  # id 1

        res = self._post(
            business["id"],
            {"customer_id": True, "items": [{"product_id": product["id"], "quantity": 1}]},
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.sales.sales, [])

    def test_create_sale_rejects_float_customer_id(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(
            business["id"],
            {"customer_id": 1.5, "items": [{"product_id": product["id"], "quantity": 1}]},
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.sales.sales, [])

    # ---- INVENTORY DEDUCTION ----

    def test_untracked_product_is_never_blocked_by_stock(self):
        """No pos_inventory row at all -- the product has simply never had
        its stock tracked (no "receive stock" feature exists yet), which
        must behave exactly like it always has: the sale succeeds
        regardless of quantity."""
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 50}]})

        self.assertEqual(res.status_code, 201)
        self.assertIsNone(self.inventory.get(product["id"]))

    def test_tracked_product_with_sufficient_stock_is_deducted(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)
        self.inventory.track(product["id"], 10)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 3}]})

        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.inventory.get(product["id"]), 7)

    def test_deducting_exactly_all_remaining_stock_succeeds_and_leaves_zero(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)
        self.inventory.track(product["id"], 5)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 5}]})

        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.inventory.get(product["id"]), 0)

    def test_insufficient_stock_is_rejected_and_nothing_is_written(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)
        self.inventory.track(product["id"], 2)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 3}]})

        self.assertEqual(res.status_code, 409)
        self.assertIn("Widget", res.get_json()["error"])
        self.assertEqual(self.sales.sales, [])
        self.assertEqual(self.sales.items, [])
        # Stock must be completely untouched by a rejected sale.
        self.assertEqual(self.inventory.get(product["id"]), 2)

    def test_a_multi_item_sale_with_one_out_of_stock_line_rolls_back_entirely(self):
        """Atomicity across lines: product A has plenty of stock, product
        B does not -- the whole sale must fail, and A's stock must be
        exactly as untouched as B's."""
        business = self.businesses.create(1, "Shop A")
        a = self.products.add(business["id"], "A", 100)
        b = self.products.add(business["id"], "B", 200)
        self.inventory.track(a["id"], 100)
        self.inventory.track(b["id"], 1)

        res = self._post(
            business["id"],
            {"items": [
                {"product_id": a["id"], "quantity": 5},
                {"product_id": b["id"], "quantity": 2},
            ]},
        )

        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.sales.sales, [])
        self.assertEqual(self.inventory.get(a["id"]), 100, "an earlier, in-stock line must not be "
                                                             "deducted when a later line fails")
        self.assertEqual(self.inventory.get(b["id"]), 1)

    def test_multiple_tracked_products_in_one_sale_are_each_deducted_correctly(self):
        business = self.businesses.create(1, "Shop A")
        a = self.products.add(business["id"], "A", 100)
        b = self.products.add(business["id"], "B", 200)
        self.inventory.track(a["id"], 10)
        self.inventory.track(b["id"], 10)

        res = self._post(
            business["id"],
            {"items": [
                {"product_id": a["id"], "quantity": 4},
                {"product_id": b["id"], "quantity": 6},
            ]},
        )

        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.inventory.get(a["id"]), 6)
        self.assertEqual(self.inventory.get(b["id"]), 4)

    def test_a_mix_of_tracked_and_untracked_products_only_deducts_the_tracked_one(self):
        business = self.businesses.create(1, "Shop A")
        tracked = self.products.add(business["id"], "Tracked", 100)
        untracked = self.products.add(business["id"], "Untracked", 200)
        self.inventory.track(tracked["id"], 10)

        res = self._post(
            business["id"],
            {"items": [
                {"product_id": tracked["id"], "quantity": 4},
                {"product_id": untracked["id"], "quantity": 1000},
            ]},
        )

        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.inventory.get(tracked["id"]), 6)
        self.assertIsNone(self.inventory.get(untracked["id"]))

    # ---- PHASE 6: RECEIVE -> SALE INTERACTION ----

    def test_receiving_stock_then_starts_tracking_and_a_later_sale_deducts_correctly(self):
        """End-to-end proof of the exact example in the Phase 6 spec:
        untracked -> receive 10 -> is_tracked, quantity 10 -> sell 3 ->
        quantity 7. Exercises the real POST .../receive endpoint (not
        PosInventoryStore.track() directly, unlike every other test in
        this class), so this is the one test that actually proves receive
        and create_sale compose correctly through two real requests."""
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)
        self.assertIsNone(self.inventory.get(product["id"]))

        receive_res = self._receive(business["id"], product["id"], 10)
        self.assertEqual(receive_res.status_code, 200)
        self.assertEqual(receive_res.get_json()["inventory_item"]["quantity"], 10)
        self.assertIs(receive_res.get_json()["inventory_item"]["is_tracked"], True)

        sale_res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 3}]})

        self.assertEqual(sale_res.status_code, 201)
        self.assertEqual(self.inventory.get(product["id"]), 7)

    def test_receiving_more_stock_than_available_still_enforces_insufficient_stock_on_sale(self):
        business = self.businesses.create(1, "Shop A")
        product = self.products.add(business["id"], "Widget", 100)
        self._receive(business["id"], product["id"], 5)

        res = self._post(business["id"], {"items": [{"product_id": product["id"], "quantity": 6}]})

        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.sales.sales, [])
        self.assertEqual(self.inventory.get(product["id"]), 5)

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
        # Full pagination-metadata correctness (page/limit/total/has_next/
        # has_previous) is covered by tests/test_pos_sales_history.py --
        # this test only cares that an empty history returns an empty list.
        self.assertEqual(res.get_json()["sales"], [])

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
