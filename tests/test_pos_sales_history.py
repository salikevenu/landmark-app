"""Sales History V1.1: GET /api/pos/businesses/<id>/sales pagination,
ordering, date-range/payment-method filtering, and N+1 elimination
(routes/pos_routes.py:list_sales).

No real database connection -- pos_routes.get_db_connection is patched
with an in-memory fake, matching tests/test_pos_sales.py's established
pattern. Sale/item rows are seeded directly (not via POST /sales, which
is already covered by test_pos_sales.py) so this file can focus purely
on history/pagination/filtering behavior.
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


class PosSalesFixture:
    """Directly seeded sale/item rows -- this file tests GET /sales'
    history/pagination/filtering logic in isolation from POST /sales'
    creation flow (already covered by tests/test_pos_sales.py)."""

    def __init__(self):
        self.sales = []
        self.items = []
        self._next_sale_id = 1

    def add_sale(self, business_id, total_amount=1000, payment_method="cash",
                 created_at=None, items=None, customer_id=None):
        sale_id = self._next_sale_id
        self._next_sale_id += 1
        self.sales.append({
            "id": sale_id,
            "business_id": business_id,
            "total_amount": total_amount,
            "payment_method": payment_method,
            "customer_id": customer_id,
            "created_at": created_at or datetime(2026, 1, 1, 12, 0, 0),
        })
        for item in (items or []):
            self.items.append({"sale_id": sale_id, **item})
        return sale_id

    def filtered_for_business(self, business_id, from_date=None, to_exclusive=None,
                               payment_method=None, customer_id=None):
        def matches(sale):
            if sale["business_id"] != business_id:
                return False
            if from_date is not None and sale["created_at"].date() < from_date:
                return False
            if to_exclusive is not None and sale["created_at"].date() >= to_exclusive:
                return False
            if payment_method is not None and sale["payment_method"] != payment_method:
                return False
            if customer_id is not None and sale.get("customer_id") != customer_id:
                return False
            return True

        return [s for s in self.sales if matches(s)]


class PosCustomerStore:
    """Phase 8: mirrors pos_customers for list_sales' customer LEFT JOIN."""

    def __init__(self):
        self.rows = []
        self.next_id = 1

    def add(self, business_id, name, phone):
        row = {
            "id": self.next_id,
            "business_id": business_id,
            "name": name,
            "phone": phone,
            "created_at": datetime(2026, 1, 1) + timedelta(seconds=self.next_id),
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def find(self, customer_id, business_id):
        return next(
            (r for r in self.rows if r["id"] == customer_id and r["business_id"] == business_id),
            None,
        )


class FakeConn:
    """`state` carries the fake user/subscription entitlement rows (like
    tests/test_pos_entitlement_enforcement.py) so a test can flip
    has_access without re-patching. `count_calls` tracks how many times
    each query *kind* actually executed -- the N+1 tests assert on it
    directly rather than inferring it from response shape alone."""

    def __init__(self, businesses, sales, state, customers=None):
        self.businesses = businesses
        self.sales = sales
        self.state = state
        self.customers = customers if customers is not None else PosCustomerStore()
        self.count_calls = {"count": 0, "list": 0, "items": 0}

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if q.startswith("select plan, subscription_expiry from users"):
            user_row = self.state["user_row"]
            return FakeResult(row=FakeRow(dict(user_row)) if user_row else None)

        if q.startswith("select pos_plan, status, expires_at from pos_subscriptions"):
            sub_row = self.state["subscription_row"]
            return FakeResult(row=FakeRow(dict(sub_row)) if sub_row else None)

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("select count(*) as total from pos_sales"):
            self.count_calls["count"] += 1
            matching = self.sales.filtered_for_business(
                params.get("business_id"),
                from_date=params.get("from_date"),
                to_exclusive=params.get("to_exclusive"),
                payment_method=params.get("payment_method"),
                customer_id=params.get("customer_id"),
            )
            matching = self._apply_customer_search(matching, params.get("customer_search"))
            return FakeResult(row=FakeRow({"total": len(matching)}))

        if q.startswith("select s.id, s.total_amount, s.payment_method, s.created_at,"):
            self.count_calls["list"] += 1
            matching = self.sales.filtered_for_business(
                params.get("business_id"),
                from_date=params.get("from_date"),
                to_exclusive=params.get("to_exclusive"),
                payment_method=params.get("payment_method"),
                customer_id=params.get("customer_id"),
            )
            matching = self._apply_customer_search(matching, params.get("customer_search"))
            ordered = sorted(matching, key=lambda s: (s["created_at"], s["id"]), reverse=True)
            limit = params.get("limit")
            offset = params.get("offset", 0) or 0
            page = ordered[offset:offset + limit] if limit is not None else ordered
            # Mirrors the real query's LEFT JOIN pos_customers -- same
            # bulk-per-page shape, never one lookup per sale (this is
            # exactly what test_query_count_does_not_grow_with_the_
            # number_of_sales_on_the_page/test_fetching_a_page_of_many_
            # sales_issues_exactly_one_items_query guard against).
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
            self.count_calls["items"] += 1
            sale_ids = set(params.get("sale_ids") or [])
            rows = sorted(
                (i for i in self.sales.items if i["sale_id"] in sale_ids),
                key=lambda i: i["sale_id"],
            )
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def _apply_customer_search(self, matching, customer_search):
        """Mirrors the real query's `(c.name ILIKE :customer_search OR
        c.phone ILIKE :customer_search)` -- customer_search arrives here
        already wrapped as "%term%" (see how create_sale/list_sales binds
        it), matching this fake's own convention of taking params exactly
        as the real bind values would be. A walk-in sale (no customer)
        never matches, same as NULL ILIKE ... being falsy for real."""
        if customer_search is None:
            return matching
        term = customer_search.strip("%").lower()

        def customer_matches(sale):
            customer_id = sale.get("customer_id")
            if customer_id is None:
                return False
            customer = self.customers.find(customer_id, sale["business_id"])
            if customer is None:
                return False
            return term in customer["name"].lower() or term in customer["phone"].lower()

        return [s for s in matching if customer_matches(s)]

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


BUSINESS_POWER_USER = {"plan": "business_power", "subscription_expiry": "2099-01-01"}
NOT_BUSINESS_POWER = {"plan": "free", "subscription_expiry": None}


class PosSalesHistoryTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.sales = PosSalesFixture()
        self.customers = PosCustomerStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        self.state = {"user_row": dict(BUSINESS_POWER_USER), "subscription_row": None}
        self.conn = FakeConn(self.businesses, self.sales, self.state, customers=self.customers)
        patcher = patch("routes.pos_routes.get_db_connection", lambda: self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _get(self, business_id, query="", uid=1, auth=True):
        headers = self._auth_headers(uid) if auth else {}
        suffix = f"?{query}" if query else ""
        return self.client.get(
            f"/api/pos/businesses/{business_id}/sales{suffix}", headers=headers
        )

    # ---- AUTH / OWNERSHIP / ENTITLEMENT ----

    def test_unauthenticated_is_rejected(self):
        res = self.client.get("/api/pos/businesses/1/sales")
        self.assertEqual(res.status_code, 401)

    def test_another_owners_business_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], uid=2)
        self.assertEqual(res.status_code, 404)

    def test_nonexistent_business_returns_404(self):
        res = self._get(999)
        self.assertEqual(res.status_code, 404)

    def test_another_owner_and_nonexistent_business_get_identical_responses(self):
        business = self.businesses.create(1, "Shop A")
        other_owner_res = self._get(business["id"], uid=2)
        nonexistent_res = self._get(999, uid=1)
        self.assertEqual(other_owner_res.status_code, nonexistent_res.status_code)
        self.assertEqual(other_owner_res.get_json(), nonexistent_res.get_json())

    def test_no_pos_entitlement_returns_403(self):
        business = self.businesses.create(1, "Shop A")
        self.state["user_row"] = dict(NOT_BUSINESS_POWER)
        self.state["subscription_row"] = None
        res = self._get(business["id"])
        self.assertEqual(res.status_code, 403)

    # ---- DEFAULTS / VALIDATION ----

    def test_defaults_to_page_1_limit_20(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"])
        body = res.get_json()
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["limit"], 20)

    def test_page_zero_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "page=0")
        self.assertEqual(res.status_code, 400)

    def test_negative_page_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "page=-1")
        self.assertEqual(res.status_code, 400)

    def test_non_integer_page_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "page=abc")
        self.assertEqual(res.status_code, 400)

    def test_limit_zero_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "limit=0")
        self.assertEqual(res.status_code, 400)

    def test_limit_above_100_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "limit=101")
        self.assertEqual(res.status_code, 400)

    def test_limit_of_exactly_100_is_allowed(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "limit=100")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["limit"], 100)

    def test_non_integer_limit_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "limit=abc")
        self.assertEqual(res.status_code, 400)

    def test_invalid_from_date_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "from=not-a-date")
        self.assertEqual(res.status_code, 400)

    def test_invalid_to_date_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "to=2026-13-40")
        self.assertEqual(res.status_code, 400)

    def test_invalid_payment_method_is_rejected(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], "payment_method=bitcoin")
        self.assertEqual(res.status_code, 400)

    # ---- ORDERING ----

    def test_default_order_is_newest_first(self):
        business = self.businesses.create(1, "Shop A")
        older = self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1))
        newer = self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 5))

        res = self._get(business["id"])

        ids = [s["id"] for s in res.get_json()["sales"]]
        self.assertEqual(ids, [newer, older])

    def test_same_created_at_breaks_the_tie_by_id_descending(self):
        business = self.businesses.create(1, "Shop A")
        same_time = datetime(2026, 1, 1, 12, 0, 0)
        first = self.sales.add_sale(business["id"], created_at=same_time)
        second = self.sales.add_sale(business["id"], created_at=same_time)

        res = self._get(business["id"])

        ids = [s["id"] for s in res.get_json()["sales"]]
        self.assertEqual(ids, [second, first])

    # ---- PAGINATION ----

    def test_pagination_metadata_shape(self):
        business = self.businesses.create(1, "Shop A")
        for i in range(5):
            self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i))

        res = self._get(business["id"], "page=1&limit=2")

        body = res.get_json()
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["total"], 5)
        self.assertTrue(body["has_next"])
        self.assertFalse(body["has_previous"])
        self.assertEqual(len(body["sales"]), 2)

    def test_second_page_has_previous_true(self):
        business = self.businesses.create(1, "Shop A")
        for i in range(5):
            self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i))

        res = self._get(business["id"], "page=2&limit=2")

        body = res.get_json()
        self.assertTrue(body["has_previous"])
        self.assertTrue(body["has_next"])
        self.assertEqual(len(body["sales"]), 2)

    def test_last_page_has_next_false(self):
        business = self.businesses.create(1, "Shop A")
        for i in range(5):
            self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i))

        res = self._get(business["id"], "page=3&limit=2")

        body = res.get_json()
        self.assertFalse(body["has_next"])
        self.assertTrue(body["has_previous"])
        self.assertEqual(len(body["sales"]), 1)

    def test_pages_do_not_overlap_and_cover_every_sale_exactly_once(self):
        business = self.businesses.create(1, "Shop A")
        expected_ids = set()
        for i in range(7):
            sale_id = self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i))
            expected_ids.add(sale_id)

        seen_ids = []
        page = 1
        while True:
            body = self._get(business["id"], f"page={page}&limit=3").get_json()
            seen_ids.extend(s["id"] for s in body["sales"])
            if not body["has_next"]:
                break
            page += 1

        self.assertEqual(set(seen_ids), expected_ids)
        self.assertEqual(len(seen_ids), len(expected_ids), "no sale should appear on two pages")

    def test_page_beyond_available_data_returns_an_empty_page_not_an_error(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"])

        res = self._get(business["id"], "page=999&limit=20")

        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["sales"], [])
        self.assertEqual(body["total"], 1)
        self.assertFalse(body["has_next"])
        self.assertTrue(body["has_previous"])

    def test_empty_history_returns_an_empty_page(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"])

        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["sales"], [])
        self.assertEqual(body["total"], 0)
        self.assertFalse(body["has_next"])
        self.assertFalse(body["has_previous"])

    # ---- DATE FILTERING ----

    def test_from_excludes_sales_before_it(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 4, 23, 59, 59))
        included = self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 5, 0, 0, 0))

        res = self._get(business["id"], "from=2026-01-05")

        ids = [s["id"] for s in res.get_json()["sales"]]
        self.assertEqual(ids, [included])

    def test_to_includes_the_entire_specified_day(self):
        business = self.businesses.create(1, "Shop A")
        included = self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 5, 23, 59, 59))
        excluded = self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 6, 0, 0, 0))

        res = self._get(business["id"], "to=2026-01-05")

        ids = [s["id"] for s in res.get_json()["sales"]]
        self.assertEqual(ids, [included])
        self.assertNotIn(excluded, ids)

    def test_from_and_to_together_form_an_inclusive_range(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1, 12, 0, 0))
        in_range_1 = self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 5, 0, 0, 0))
        in_range_2 = self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 10, 23, 59, 59))
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 15, 0, 0, 0))

        res = self._get(business["id"], "from=2026-01-05&to=2026-01-10")

        ids = {s["id"] for s in res.get_json()["sales"]}
        self.assertEqual(ids, {in_range_1, in_range_2})

    def test_a_date_range_with_no_matching_sales_returns_an_empty_page(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1))

        res = self._get(business["id"], "from=2027-01-01&to=2027-01-31")

        body = res.get_json()
        self.assertEqual(body["sales"], [])
        self.assertEqual(body["total"], 0)

    # ---- PAYMENT METHOD FILTERING ----

    def test_payment_method_filter_returns_only_matching_sales(self):
        business = self.businesses.create(1, "Shop A")
        cash_sale = self.sales.add_sale(business["id"], payment_method="cash")
        self.sales.add_sale(business["id"], payment_method="card")
        self.sales.add_sale(business["id"], payment_method="upi")

        res = self._get(business["id"], "payment_method=cash")

        body = res.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["sales"][0]["id"], cash_sale)
        self.assertEqual(body["sales"][0]["payment_method"], "cash")

    def test_payment_method_combined_with_date_range(self):
        business = self.businesses.create(1, "Shop A")
        matching = self.sales.add_sale(
            business["id"], payment_method="upi", created_at=datetime(2026, 1, 5)
        )
        # Wrong payment method, same date.
        self.sales.add_sale(business["id"], payment_method="cash", created_at=datetime(2026, 1, 5))
        # Right payment method, outside the date range.
        self.sales.add_sale(business["id"], payment_method="upi", created_at=datetime(2026, 2, 1))

        res = self._get(business["id"], "payment_method=upi&from=2026-01-01&to=2026-01-31")

        ids = [s["id"] for s in res.get_json()["sales"]]
        self.assertEqual(ids, [matching])

    # ---- BUSINESS ISOLATION ----

    def test_sales_from_another_business_are_never_included(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        self.sales.add_sale(business_a["id"])
        self.sales.add_sale(business_b["id"])

        res = self._get(business_a["id"])

        self.assertEqual(res.get_json()["total"], 1)

    # ---- ITEM GROUPING / _sale_payload COMPATIBILITY ----

    def test_items_are_grouped_under_the_correct_sale_no_cross_contamination(self):
        business = self.businesses.create(1, "Shop A")
        sale_a = self.sales.add_sale(
            business["id"],
            created_at=datetime(2026, 1, 2),
            items=[{
                "product_id": 1, "product_name": "A Widget", "unit_price": 100,
                "quantity": 1, "line_total": 100,
            }],
        )
        sale_b = self.sales.add_sale(
            business["id"],
            created_at=datetime(2026, 1, 3),
            items=[
                {"product_id": 2, "product_name": "B Widget", "unit_price": 200,
                 "quantity": 2, "line_total": 400},
                {"product_id": 3, "product_name": "C Widget", "unit_price": 50,
                 "quantity": 1, "line_total": 50},
            ],
        )

        res = self._get(business["id"])

        by_id = {s["id"]: s for s in res.get_json()["sales"]}
        self.assertEqual(len(by_id[sale_a]["items"]), 1)
        self.assertEqual(by_id[sale_a]["items"][0]["product_name"], "A Widget")
        self.assertEqual(len(by_id[sale_b]["items"]), 2)
        self.assertEqual(
            {i["product_name"] for i in by_id[sale_b]["items"]},
            {"B Widget", "C Widget"},
        )

    def test_a_sale_with_no_items_gets_an_empty_items_list_not_an_error(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"])

        res = self._get(business["id"])

        self.assertEqual(res.get_json()["sales"][0]["items"], [])

    def test_sale_payload_preserves_existing_fields(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(
            business["id"],
            total_amount=2500,
            payment_method="card",
            created_at=datetime(2026, 3, 4, 10, 30, 0),
            items=[{
                "product_id": 9, "product_name": "Widget", "unit_price": 2500,
                "quantity": 1, "line_total": 2500,
            }],
        )

        res = self._get(business["id"])

        sale = res.get_json()["sales"][0]
        self.assertEqual(
            set(sale.keys()),
            {"id", "total_amount", "payment_method", "created_at", "customer", "items"},
        )
        self.assertEqual(sale["total_amount"], 2500)
        self.assertEqual(sale["payment_method"], "card")
        self.assertEqual(sale["created_at"], "2026-03-04T10:30:00")
        self.assertIsNone(sale["customer"], "no customer_id was given -- must be null, not omitted")
        item = sale["items"][0]
        self.assertEqual(
            set(item.keys()),
            {"product_id", "product_name", "unit_price", "quantity", "line_total"},
        )

    # ---- PHASE 8: CUSTOMER ASSOCIATION ----

    def test_historical_sale_with_customer_reports_name_and_phone(self):
        business = self.businesses.create(1, "Shop A")
        customer = self.customers.add(business["id"], "Alice", "9876543210")
        self.sales.add_sale(business["id"], customer_id=customer["id"])

        res = self._get(business["id"])

        sale_customer = res.get_json()["sales"][0]["customer"]
        self.assertEqual(sale_customer["id"], customer["id"])
        self.assertEqual(sale_customer["name"], "Alice")
        self.assertEqual(sale_customer["phone"], "9876543210")

    def test_historical_sale_without_customer_reports_null(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"])

        res = self._get(business["id"])

        self.assertIsNone(res.get_json()["sales"][0]["customer"])

    def test_a_page_mixing_customer_and_walk_in_sales_reports_each_correctly(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        bob = self.customers.add(business["id"], "Bob", "9111111111")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1), customer_id=alice["id"])
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 2))  # walk-in
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 3), customer_id=bob["id"])

        res = self._get(business["id"])

        sales = res.get_json()["sales"]  # newest first
        self.assertEqual(sales[0]["customer"]["name"], "Bob")
        self.assertIsNone(sales[1]["customer"])
        self.assertEqual(sales[2]["customer"]["name"], "Alice")

    def test_customer_association_never_reintroduces_an_n_plus_1_query(self):
        """Each sale on the page has a *different* customer -- if
        customer info were ever fetched with one query per sale (instead
        of the single LEFT JOIN), this is exactly the scenario that would
        expose it."""
        business = self.businesses.create(1, "Shop A")
        for i in range(10):
            customer = self.customers.add(business["id"], f"Customer {i}", f"90000000{i:02d}")
            self.sales.add_sale(
                business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i),
                customer_id=customer["id"],
            )

        res = self._get(business["id"], "limit=10")

        sales = res.get_json()["sales"]
        self.assertEqual(len(sales), 10)
        self.assertTrue(all(s["customer"] is not None for s in sales))
        self.assertEqual(
            self.conn.count_calls["list"], 1,
            "customer info must come from the same single list query, never one per sale",
        )

    def test_a_customer_from_another_business_never_appears_on_this_businesss_sales(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        # Same numeric id as business_a's own first customer would get,
        # but scoped to business_b -- proves the join is business-scoped,
        # not just id-matched.
        self.customers.add(business_b["id"], "Not Alice's Business", "9000000000")
        alice = self.customers.add(business_a["id"], "Alice", "9876543210")
        self.sales.add_sale(business_a["id"], customer_id=alice["id"])

        res = self._get(business_a["id"])

        sale_customer = res.get_json()["sales"][0]["customer"]
        self.assertEqual(sale_customer["name"], "Alice")

    # ---- N+1 PROTECTION ----

    def test_fetching_a_page_of_many_sales_issues_exactly_one_items_query(self):
        business = self.businesses.create(1, "Shop A")
        for i in range(15):
            self.sales.add_sale(
                business["id"],
                created_at=datetime(2026, 1, 1) + timedelta(days=i),
                items=[{
                    "product_id": 1, "product_name": "Widget", "unit_price": 100,
                    "quantity": 1, "line_total": 100,
                }],
            )

        res = self._get(business["id"], "limit=15")

        self.assertEqual(len(res.get_json()["sales"]), 15)
        self.assertEqual(
            self.conn.count_calls["items"], 1,
            "items must be fetched in one bulk query regardless of page size",
        )
        self.assertEqual(self.conn.count_calls["count"], 1)
        self.assertEqual(self.conn.count_calls["list"], 1)

    def test_an_empty_page_issues_zero_items_queries(self):
        business = self.businesses.create(1, "Shop A")

        self._get(business["id"])

        self.assertEqual(
            self.conn.count_calls["items"], 0,
            "no items query should run at all when the page has no sales",
        )

    def test_query_count_does_not_grow_with_the_number_of_sales_on_the_page(self):
        business = self.businesses.create(1, "Shop A")
        for i in range(2):
            self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i))
        self._get(business["id"], "limit=2")
        small_page_items_calls = self.conn.count_calls["items"]

        for i in range(2, 40):
            self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i))
        self.conn.count_calls["items"] = 0
        self._get(business["id"], "limit=40")
        large_page_items_calls = self.conn.count_calls["items"]

        self.assertEqual(small_page_items_calls, 1)
        self.assertEqual(large_page_items_calls, 1)

    # ---- PHASE 9: CUSTOMER FILTER & SEARCH ----

    def test_customer_id_omitted_returns_all_customers_sales(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1), customer_id=alice["id"])
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 2))  # walk-in

        res = self._get(business["id"])

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["total"], 2)

    def test_valid_customer_id_filters_to_only_that_customers_sales(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        bob = self.customers.add(business["id"], "Bob", "9111111111")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1), customer_id=alice["id"])
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 2), customer_id=bob["id"])
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 3))  # walk-in

        res = self._get(business["id"], f"customer_id={alice['id']}")

        body = res.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["sales"][0]["customer"]["name"], "Alice")

    def test_nonexistent_customer_id_returns_an_empty_page_not_an_error(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"])

        res = self._get(business["id"], "customer_id=999999")

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["sales"], [])
        self.assertEqual(body["total"], 0)

    def test_cross_business_customer_id_returns_the_identical_empty_page(self):
        """Never distinguishable from a nonexistent id -- both must
        resolve to the exact same response, so a caller learns nothing
        about whether the id exists at all in another business."""
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        other_customer = self.customers.add(business_b["id"], "Bob", "9111111111")
        self.sales.add_sale(business_a["id"])

        res_cross_business = self._get(business_a["id"], f"customer_id={other_customer['id']}")
        res_nonexistent = self._get(business_a["id"], "customer_id=999999")

        self.assertEqual(res_cross_business.status_code, 200)
        self.assertEqual(res_cross_business.get_json(), res_nonexistent.get_json())

    def test_invalid_customer_id_string_rejected(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"], "customer_id=abc")

        self.assertEqual(res.status_code, 400)

    def test_bool_looking_customer_id_rejected(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"], "customer_id=true")

        self.assertEqual(res.status_code, 400)

    def test_float_looking_customer_id_rejected(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"], "customer_id=1.5")

        self.assertEqual(res.status_code, 400)

    def test_customer_search_matches_by_name(self):
        business = self.businesses.create(1, "Shop A")
        raju = self.customers.add(business["id"], "Raju", "9876543210")
        self.customers.add(business["id"], "Bob", "9111111111")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1), customer_id=raju["id"])

        res = self._get(business["id"], "customer_search=raju")

        body = res.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["sales"][0]["customer"]["name"], "Raju")

    def test_customer_search_is_case_insensitive(self):
        business = self.businesses.create(1, "Shop A")
        raju = self.customers.add(business["id"], "Raju", "9876543210")
        self.sales.add_sale(business["id"], customer_id=raju["id"])

        for term in ("RAJU", "raju", "RaJu"):
            res = self._get(business["id"], f"customer_search={term}")
            self.assertEqual(res.get_json()["total"], 1, term)

    def test_customer_search_matches_by_phone(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        self.customers.add(business["id"], "Bob", "9111111111")
        self.sales.add_sale(business["id"], customer_id=alice["id"])

        res = self._get(business["id"], "customer_search=8765432")

        body = res.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["sales"][0]["customer"]["phone"], "9876543210")

    def test_customer_search_with_no_match_returns_an_empty_page(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        self.sales.add_sale(business["id"], customer_id=alice["id"])

        res = self._get(business["id"], "customer_search=nonexistent")

        body = res.get_json()
        self.assertEqual(body["sales"], [])
        self.assertEqual(body["total"], 0)

    def test_customer_search_never_matches_a_walk_in_sale(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"])  # walk-in, no customer at all

        res = self._get(business["id"], "customer_search=a")

        self.assertEqual(res.get_json()["total"], 0)

    def test_customer_id_and_customer_search_combine_with_and(self):
        business = self.businesses.create(1, "Shop A")
        raju = self.customers.add(business["id"], "Raju", "9876543210")
        priya = self.customers.add(business["id"], "Priya", "9111111111")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1), customer_id=raju["id"])
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 2), customer_id=priya["id"])

        # Matches Raju by search, but the customer_id points at Priya --
        # AND semantics means neither sale should match.
        res = self._get(business["id"], f"customer_id={priya['id']}&customer_search=raju")

        self.assertEqual(res.get_json()["total"], 0)

        # Consistent (matching) filters together still return the sale.
        res_consistent = self._get(
            business["id"], f"customer_id={raju['id']}&customer_search=raju"
        )
        self.assertEqual(res_consistent.get_json()["total"], 1)

    def test_customer_filter_combines_with_payment_method_filter(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        self.sales.add_sale(
            business["id"], created_at=datetime(2026, 1, 1),
            customer_id=alice["id"], payment_method="cash",
        )
        self.sales.add_sale(
            business["id"], created_at=datetime(2026, 1, 2),
            customer_id=alice["id"], payment_method="upi",
        )

        res = self._get(business["id"], f"customer_id={alice['id']}&payment_method=upi")

        body = res.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["sales"][0]["payment_method"], "upi")

    def test_customer_filter_combines_with_date_range_filter(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        self.sales.add_sale(
            business["id"], created_at=datetime(2026, 1, 5), customer_id=alice["id"],
        )
        self.sales.add_sale(
            business["id"], created_at=datetime(2026, 2, 5), customer_id=alice["id"],
        )

        res = self._get(
            business["id"], f"customer_id={alice['id']}&from=2026-01-01&to=2026-01-31"
        )

        self.assertEqual(res.get_json()["total"], 1)

    def test_customer_filter_paginates_correctly(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        bob = self.customers.add(business["id"], "Bob", "9111111111")
        for i in range(5):
            self.sales.add_sale(
                business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i),
                customer_id=alice["id"],
            )
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 10), customer_id=bob["id"])

        page1 = self._get(business["id"], f"customer_id={alice['id']}&page=1&limit=2")
        page2 = self._get(business["id"], f"customer_id={alice['id']}&page=2&limit=2")
        page3 = self._get(business["id"], f"customer_id={alice['id']}&page=3&limit=2")

        self.assertEqual(len(page1.get_json()["sales"]), 2)
        self.assertEqual(len(page2.get_json()["sales"]), 2)
        self.assertEqual(len(page3.get_json()["sales"]), 1)
        all_ids = {s["id"] for p in (page1, page2, page3) for s in p.get_json()["sales"]}
        self.assertEqual(len(all_ids), 5, "every Alice sale appears exactly once across pages")

    def test_pagination_metadata_is_correct_after_customer_filtering(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        for i in range(3):
            self.sales.add_sale(
                business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i),
                customer_id=alice["id"],
            )
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 10))  # walk-in, excluded

        res = self._get(business["id"], f"customer_id={alice['id']}&page=1&limit=2")

        body = res.get_json()
        self.assertEqual(body["total"], 3)
        self.assertTrue(body["has_next"])
        self.assertFalse(body["has_previous"])
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["limit"], 2)

    def test_ordering_remains_created_at_desc_id_desc_under_customer_filter(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        same_moment = datetime(2026, 1, 1, 12, 0, 0)
        first_id = self.sales.add_sale(
            business["id"], created_at=same_moment, customer_id=alice["id"],
        )
        second_id = self.sales.add_sale(
            business["id"], created_at=same_moment, customer_id=alice["id"],
        )

        res = self._get(business["id"], f"customer_id={alice['id']}")

        ids = [s["id"] for s in res.get_json()["sales"]]
        self.assertEqual(ids, [second_id, first_id], "same created_at ties break by id DESC")

    def test_customer_filter_maintains_business_isolation(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        alice_a = self.customers.add(business_a["id"], "Alice", "9876543210")
        # A same-named customer in a different business, with their own id.
        alice_b = self.customers.add(business_b["id"], "Alice", "9111111111")
        self.sales.add_sale(business_a["id"], customer_id=alice_a["id"])
        self.sales.add_sale(business_b["id"], customer_id=alice_b["id"])

        res_a = self._get(business_a["id"], "customer_search=alice")
        res_b = self._get(business_b["id"], "customer_search=alice")

        self.assertEqual(res_a.get_json()["total"], 1)
        self.assertEqual(res_a.get_json()["sales"][0]["customer"]["phone"], "9876543210")
        self.assertEqual(res_b.get_json()["total"], 1)
        self.assertEqual(res_b.get_json()["sales"][0]["customer"]["phone"], "9111111111")

    def test_customer_filtering_never_reintroduces_an_n_plus_1_query(self):
        business = self.businesses.create(1, "Shop A")
        alice = self.customers.add(business["id"], "Alice", "9876543210")
        for i in range(10):
            self.sales.add_sale(
                business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i),
                customer_id=alice["id"],
            )

        res = self._get(business["id"], f"customer_id={alice['id']}&limit=10")

        self.assertEqual(len(res.get_json()["sales"]), 10)
        self.assertEqual(self.conn.count_calls["count"], 1)
        self.assertEqual(self.conn.count_calls["list"], 1)
        self.assertEqual(self.conn.count_calls["items"], 1)

    def test_empty_filtered_page_performs_zero_item_queries(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"])  # walk-in, will not match

        res = self._get(business["id"], "customer_search=nobody")

        self.assertEqual(res.get_json()["sales"], [])
        self.assertEqual(
            self.conn.count_calls["items"], 0,
            "no items query should run at all when the filtered page has no sales",
        )


if __name__ == "__main__":
    unittest.main()
