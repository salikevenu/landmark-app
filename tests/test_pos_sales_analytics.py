"""Sales Analytics V1.7: GET /api/pos/businesses/<id>/sales/analytics
(routes/pos_routes.py:get_sales_analytics).

No real database connection -- pos_routes.get_db_connection is patched
with an in-memory fake, matching tests/test_pos_sales_history.py's
established pattern. Sales are seeded directly (bypassing POST /sales,
already covered by test_pos_sales.py) so this file focuses purely on the
aggregation/filtering/isolation behavior of the analytics endpoint
itself.
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
    """Directly seeded sale rows -- this file tests the analytics
    endpoint's aggregation/filtering in isolation from POST /sales'
    creation flow (already covered by tests/test_pos_sales.py)."""

    def __init__(self):
        self.sales = []
        self._next_sale_id = 1

    def add_sale(self, business_id, total_amount=1000, payment_method="cash",
                 created_at=None, customer_id=None):
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
        return sale_id

    def filtered(self, business_id, from_date=None, to_exclusive=None,
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


class FakeConn:
    """`state` carries the fake user/subscription entitlement rows (like
    tests/test_pos_entitlement_enforcement.py) so a test can flip
    has_access without re-patching. `query_count` lets the N+1 tests
    assert the number of executed queries stays fixed regardless of how
    many sales match."""

    def __init__(self, businesses, sales, state):
        self.businesses = businesses
        self.sales = sales
        self.state = state
        self.query_count = 0

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}
        self.query_count += 1

        if q.startswith("select plan, subscription_expiry from users"):
            user_row = self.state["user_row"]
            return FakeResult(row=FakeRow(dict(user_row)) if user_row else None)

        if q.startswith("select pos_plan, status, expires_at from pos_subscriptions"):
            sub_row = self.state["subscription_row"]
            return FakeResult(row=FakeRow(dict(sub_row)) if sub_row else None)

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("select now() as as_of"):
            return FakeResult(row=FakeRow({"as_of": datetime(2026, 9, 12, 10, 0, 0)}))

        if q.startswith("select count(*) as sale_count, coalesce(sum(total_amount), 0) as total_revenue"):
            matching = self.sales.filtered(
                params.get("business_id"),
                from_date=params.get("from_date"),
                to_exclusive=params.get("to_exclusive"),
                payment_method=params.get("payment_method"),
                customer_id=params.get("customer_id"),
            )
            sale_count = len(matching)
            total_revenue = sum(s["total_amount"] for s in matching)
            average_sale = round(total_revenue / sale_count) if sale_count else 0
            return FakeResult(row=FakeRow({
                "sale_count": sale_count,
                "total_revenue": total_revenue,
                "average_sale": average_sale,
            }))

        if q.startswith("select payment_method, count(*) as sale_count, "
                         "coalesce(sum(total_amount), 0) as total_amount"):
            matching = [
                s for s in self.sales.filtered(
                    params.get("business_id"),
                    from_date=params.get("from_date"),
                    to_exclusive=params.get("to_exclusive"),
                    payment_method=params.get("payment_method"),
                    customer_id=params.get("customer_id"),
                )
                if s["payment_method"] is not None
            ]
            groups = {}
            for s in matching:
                g = groups.setdefault(s["payment_method"], {"sale_count": 0, "total_amount": 0})
                g["sale_count"] += 1
                g["total_amount"] += s["total_amount"]
            rows = [
                {"payment_method": k, **v} for k, v in sorted(groups.items())
            ]
            return FakeResult(rows=[FakeRow(r) for r in rows])

        if q.startswith("select created_at::date as sale_date, count(*) as sale_count, "
                         "coalesce(sum(total_amount), 0) as total_amount"):
            matching = self.sales.filtered(
                params.get("business_id"),
                from_date=params.get("from_date"),
                to_exclusive=params.get("to_exclusive"),
                payment_method=params.get("payment_method"),
                customer_id=params.get("customer_id"),
            )
            groups = {}
            for s in matching:
                d = s["created_at"].date()
                g = groups.setdefault(d, {"sale_count": 0, "total_amount": 0})
                g["sale_count"] += 1
                g["total_amount"] += s["total_amount"]
            rows = [{"sale_date": k, **v} for k, v in sorted(groups.items())]
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


BUSINESS_POWER_USER = {"plan": "business_power", "subscription_expiry": "2099-01-01"}
NOT_BUSINESS_POWER = {"plan": "free", "subscription_expiry": None}


class PosSalesAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.sales = PosSalesFixture()
        self.app = _make_app()
        self.client = self.app.test_client()
        self.state = {"user_row": dict(BUSINESS_POWER_USER), "subscription_row": None}
        self.conn = FakeConn(self.businesses, self.sales, self.state)
        patcher = patch("routes.pos_routes.get_db_connection", lambda: self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _auth_headers_raw_identity(self, raw_identity):
        with self.app.app_context():
            token = create_access_token(identity=raw_identity)
        return {"Authorization": f"Bearer {token}"}

    def _get(self, business_id, query="", uid=1, auth=True):
        headers = self._auth_headers(uid) if auth else {}
        suffix = f"?{query}" if query else ""
        return self.client.get(
            f"/api/pos/businesses/{business_id}/sales/analytics{suffix}", headers=headers
        )

    # ---- AUTH / OWNERSHIP / ENTITLEMENT ----

    def test_unauthenticated_is_rejected(self):
        res = self.client.get("/api/pos/businesses/1/sales/analytics")
        self.assertEqual(res.status_code, 401)

    def test_invalid_jwt_identity_is_rejected(self):
        res = self.client.get(
            "/api/pos/businesses/1/sales/analytics",
            headers=self._auth_headers_raw_identity("not-a-number"),
        )
        self.assertEqual(res.status_code, 401)

    def test_nonexistent_business_returns_404(self):
        res = self._get(999)
        self.assertEqual(res.status_code, 404)

    def test_another_users_business_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], uid=2)
        self.assertEqual(res.status_code, 404)

    def test_entitlement_denied_returns_403(self):
        self.state["user_row"] = dict(NOT_BUSINESS_POWER)
        self.state["subscription_row"] = None
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"])

        self.assertEqual(res.status_code, 403)

    # ---- SUMMARY AGGREGATION ----

    def test_empty_sales_returns_zero_summary_and_empty_breakdowns(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"])

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["summary"], {"sale_count": 0, "total_revenue": 0, "average_sale": 0})
        self.assertEqual(body["payment_methods"], [])
        self.assertEqual(body["daily_sales"], [])
        self.assertIn("as_of", body)

    def test_one_sale_is_reflected_in_summary(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=1500)

        body = self._get(business["id"]).get_json()

        self.assertEqual(body["summary"]["sale_count"], 1)
        self.assertEqual(body["summary"]["total_revenue"], 1500)
        self.assertEqual(body["summary"]["average_sale"], 1500)

    def test_multiple_sales_sum_correctly(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=1000, created_at=datetime(2026, 1, 1))
        self.sales.add_sale(business["id"], total_amount=2000, created_at=datetime(2026, 1, 2))
        self.sales.add_sale(business["id"], total_amount=3000, created_at=datetime(2026, 1, 3))

        body = self._get(business["id"]).get_json()

        self.assertEqual(body["summary"]["sale_count"], 3)
        self.assertEqual(body["summary"]["total_revenue"], 6000)

    def test_average_sale_is_total_revenue_divided_by_sale_count_rounded(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=100, created_at=datetime(2026, 1, 1))
        self.sales.add_sale(business["id"], total_amount=200, created_at=datetime(2026, 1, 2))
        self.sales.add_sale(business["id"], total_amount=201, created_at=datetime(2026, 1, 3))

        body = self._get(business["id"]).get_json()

        # (100 + 200 + 201) / 3 = 167.0 -- exact rounding, but the
        # convention (round-half-to-even vs up) only matters when it
        # doesn't divide evenly; this proves the calculation is at least
        # correct to the nearest integer.
        self.assertEqual(body["summary"]["average_sale"], 167)

    # ---- PAYMENT METHOD AGGREGATION ----

    def test_payment_method_breakdown_groups_and_sums_correctly(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=1000, payment_method="cash",
                             created_at=datetime(2026, 1, 1))
        self.sales.add_sale(business["id"], total_amount=500, payment_method="cash",
                             created_at=datetime(2026, 1, 2))
        self.sales.add_sale(business["id"], total_amount=2000, payment_method="upi",
                             created_at=datetime(2026, 1, 3))

        body = self._get(business["id"]).get_json()

        by_method = {p["payment_method"]: p for p in body["payment_methods"]}
        self.assertEqual(by_method["cash"], {"payment_method": "cash", "sale_count": 2, "total_amount": 1500})
        self.assertEqual(by_method["upi"], {"payment_method": "upi", "sale_count": 1, "total_amount": 2000})

    def test_a_sale_with_no_payment_method_is_excluded_from_the_breakdown(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=1000, payment_method=None)

        body = self._get(business["id"]).get_json()

        self.assertEqual(body["payment_methods"], [])
        self.assertEqual(body["summary"]["sale_count"], 1, "still counted in the overall summary")

    # ---- DAILY AGGREGATION ----

    def test_daily_sales_groups_by_calendar_date(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=100, created_at=datetime(2026, 1, 1, 9, 0))
        self.sales.add_sale(business["id"], total_amount=200, created_at=datetime(2026, 1, 1, 18, 0))
        self.sales.add_sale(business["id"], total_amount=300, created_at=datetime(2026, 1, 2, 12, 0))

        body = self._get(business["id"]).get_json()

        by_date = {d["date"]: d for d in body["daily_sales"]}
        self.assertEqual(by_date["2026-01-01"]["sale_count"], 2)
        self.assertEqual(by_date["2026-01-01"]["total_amount"], 300)
        self.assertEqual(by_date["2026-01-02"]["sale_count"], 1)
        self.assertEqual(by_date["2026-01-02"]["total_amount"], 300)

    def test_daily_sales_never_fabricates_a_day_with_no_sales(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1))
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 5))

        body = self._get(business["id"], "from=2026-01-01&to=2026-01-10").get_json()

        # Only the 2 days with real sales -- never a zero-filled row for
        # every day in the requested range.
        self.assertEqual(len(body["daily_sales"]), 2)

    # ---- DATE FILTERING ----

    def test_from_boundary_is_inclusive(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 5, 0, 0, 0))

        body = self._get(business["id"], "from=2026-01-05").get_json()

        self.assertEqual(body["summary"]["sale_count"], 1)

    def test_to_boundary_is_inclusive_through_end_of_day(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 5, 23, 59, 59))

        body = self._get(business["id"], "to=2026-01-05").get_json()

        self.assertEqual(body["summary"]["sale_count"], 1)

    def test_sales_outside_the_date_range_are_excluded(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 4, 23, 59, 59))
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 11, 0, 0, 0))
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 7, 12, 0, 0))

        body = self._get(business["id"], "from=2026-01-05&to=2026-01-10").get_json()

        self.assertEqual(body["summary"]["sale_count"], 1)

    # ---- OTHER FILTERS ----

    def test_payment_method_filter_narrows_the_summary_too(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=1000, payment_method="cash",
                             created_at=datetime(2026, 1, 1))
        self.sales.add_sale(business["id"], total_amount=2000, payment_method="upi",
                             created_at=datetime(2026, 1, 2))

        body = self._get(business["id"], "payment_method=upi").get_json()

        self.assertEqual(body["summary"]["sale_count"], 1)
        self.assertEqual(body["summary"]["total_revenue"], 2000)

    def test_customer_id_filter_narrows_results(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=1000, customer_id=5,
                             created_at=datetime(2026, 1, 1))
        self.sales.add_sale(business["id"], total_amount=2000, customer_id=6,
                             created_at=datetime(2026, 1, 2))
        self.sales.add_sale(business["id"], total_amount=3000, customer_id=None,
                             created_at=datetime(2026, 1, 3))

        body = self._get(business["id"], "customer_id=5").get_json()

        self.assertEqual(body["summary"]["sale_count"], 1)
        self.assertEqual(body["summary"]["total_revenue"], 1000)

    def test_combined_filters_all_apply_together(self):
        business = self.businesses.create(1, "Shop A")
        # Matches every filter.
        self.sales.add_sale(business["id"], total_amount=1000, payment_method="upi",
                             customer_id=12, created_at=datetime(2026, 1, 15))
        # Wrong payment method.
        self.sales.add_sale(business["id"], total_amount=2000, payment_method="cash",
                             customer_id=12, created_at=datetime(2026, 1, 15))
        # Wrong customer.
        self.sales.add_sale(business["id"], total_amount=3000, payment_method="upi",
                             customer_id=13, created_at=datetime(2026, 1, 15))
        # Outside date range.
        self.sales.add_sale(business["id"], total_amount=4000, payment_method="upi",
                             customer_id=12, created_at=datetime(2026, 2, 15))

        body = self._get(
            business["id"],
            "from=2026-01-01&to=2026-01-31&payment_method=upi&customer_id=12",
        ).get_json()

        self.assertEqual(body["summary"]["sale_count"], 1)
        self.assertEqual(body["summary"]["total_revenue"], 1000)

    # ---- BUSINESS ISOLATION ----

    def test_business_isolation(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        self.sales.add_sale(business_a["id"], total_amount=1000)
        self.sales.add_sale(business_b["id"], total_amount=99999)

        body_a = self._get(business_a["id"]).get_json()

        self.assertEqual(body_a["summary"]["sale_count"], 1)
        self.assertEqual(body_a["summary"]["total_revenue"], 1000)

    # ---- VALIDATION ----

    def test_malformed_from_returns_400(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"], "from=not-a-date")

        self.assertEqual(res.status_code, 400)

    def test_malformed_to_returns_400(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"], "to=not-a-date")

        self.assertEqual(res.status_code, 400)

    def test_from_after_to_returns_400(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"], "from=2026-02-01&to=2026-01-01")

        self.assertEqual(res.status_code, 400)

    # ---- PERFORMANCE / SHAPE ----

    def test_query_count_does_not_grow_with_the_number_of_matching_sales(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1))
        self._get(business["id"])
        small_query_count = self.conn.query_count

        for i in range(2, 60):
            self.sales.add_sale(business["id"], created_at=datetime(2026, 1, 1) + timedelta(days=i))
        self.conn.query_count = 0
        self._get(business["id"])
        large_query_count = self.conn.query_count

        self.assertEqual(small_query_count, large_query_count)
        self.assertLessEqual(large_query_count, 6)

    def test_response_never_includes_individual_sale_rows(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add_sale(business["id"], total_amount=1000, payment_method="cash",
                             created_at=datetime(2026, 1, 1))

        body = self._get(business["id"]).get_json()

        self.assertNotIn("sales", body)
        self.assertEqual(set(body.keys()), {"summary", "payment_methods", "daily_sales", "as_of"})
        for entry in body["payment_methods"]:
            self.assertEqual(set(entry.keys()), {"payment_method", "sale_count", "total_amount"})
        for entry in body["daily_sales"]:
            self.assertEqual(set(entry.keys()), {"date", "sale_count", "total_amount"})


if __name__ == "__main__":
    unittest.main()
