"""POS dashboard summary: GET /api/pos/businesses/<id>/dashboard/summary
(routes/pos_routes.py:get_dashboard_summary).

No real database connection -- pos_routes.get_db_connection is patched
with an in-memory fake, matching tests/test_pos_sales.py and
tests/test_pos_entitlement_enforcement.py's established pattern.

Boundary correctness (today/week) is exercised entirely through a fixed,
test-chosen `now` that the fake uses to reproduce PostgreSQL's own
date_trunc('day'/'week', NOW()) semantics -- never a real sleep() and
never the Python test process's own wall clock. day_start/week_start are
computed the same way in both the fake and the test's own expectations,
so a wrong boundary in the endpoint's SQL (e.g. using CURRENT_DATE
instead of date_trunc, or a non-ISO week start) would show up as a
mismatch here even though nothing here talks to a real Postgres.
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


class PosSalesStore:
    """Sale rows keyed only by what the summary query actually needs --
    business_id, total_amount, created_at. Deliberately unrelated to
    tests/test_pos_sales.py's richer store (line items, snapshotting):
    the summary endpoint never reads pos_sale_items at all."""

    def __init__(self):
        self.rows = []

    def add(self, business_id, total_amount, created_at):
        self.rows.append({
            "business_id": business_id,
            "total_amount": total_amount,
            "created_at": created_at,
        })

    def aggregate_since(self, business_id, start):
        matching = [
            r for r in self.rows
            if r["business_id"] == business_id and r["created_at"] >= start
        ]
        return len(matching), sum(r["total_amount"] for r in matching)


def _day_start(now):
    """Mirrors PostgreSQL's date_trunc('day', NOW())."""
    return datetime(now.year, now.month, now.day)


def _week_start(now):
    """Mirrors PostgreSQL's date_trunc('week', NOW()) -- ISO week, Monday
    00:00:00 (Python's date.weekday() is already Monday=0, same as
    Postgres's ISO week convention)."""
    return _day_start(now) - timedelta(days=now.weekday())


NOT_BUSINESS_POWER = {"plan": "free", "subscription_expiry": None}
BUSINESS_POWER_USER = {"plan": "business_power", "subscription_expiry": "2099-01-01"}


def _starter(status="active", expires_at=None):
    return {"pos_plan": "starter", "status": status, "expires_at": expires_at}


class FakeConn:
    """`state["now"]` stands in for PostgreSQL's NOW() -- test-settable,
    read live at query time (not captured at FakeConn construction), so a
    test can pick its own fixed instant without any real time passing."""

    def __init__(self, businesses, sales, state):
        self.businesses = businesses
        self.sales = sales
        self.state = state

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}
        now = self.state["now"]

        if q.startswith("select plan, subscription_expiry from users"):
            user_row = self.state["user_row"]
            return FakeResult(row=FakeRow(dict(user_row)) if user_row else None)

        if q.startswith("select pos_plan, status, expires_at from pos_subscriptions"):
            sub_row = self.state["subscription_row"]
            return FakeResult(row=FakeRow(dict(sub_row)) if sub_row else None)

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if "count(*) as sale_count" in q and "from pos_sales" in q:
            if "date_trunc('day'" in q:
                start = _day_start(now)
            elif "date_trunc('week'" in q:
                start = _week_start(now)
            else:
                raise AssertionError(f"query has no recognized boundary: {q}")

            count, total = self.sales.aggregate_since(params.get("business_id"), start)
            mapping = {"sale_count": count, "total_amount": total}
            if "as_of" in q:
                mapping["as_of"] = now
            return FakeResult(row=FakeRow(mapping))

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


class PosDashboardSummaryTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.sales = PosSalesStore()
        # A fixed instant standing in for PostgreSQL's NOW() -- arbitrary,
        # but Thursday-ish in the middle of its own ISO week so both a
        # "yesterday" and a "tomorrow" fall inside the same week for the
        # boundary tests below to place sales around.
        self.now = datetime(2026, 3, 12, 15, 30, 0)
        self.state = {
            "user_row": dict(NOT_BUSINESS_POWER),
            "subscription_row": _starter(),
            "now": self.now,
        }
        self.app = _make_app()
        self.client = self.app.test_client()
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(self.businesses, self.sales, self.state),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _get(self, business_id, uid=1, auth=True):
        headers = self._auth_headers(uid) if auth else {}
        return self.client.get(
            f"/api/pos/businesses/{business_id}/dashboard/summary", headers=headers
        )

    # ---- AUTH / OWNERSHIP ----

    def test_unauthenticated_is_rejected(self):
        res = self.client.get("/api/pos/businesses/1/dashboard/summary")
        self.assertEqual(res.status_code, 401)

    def test_another_owners_business_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], uid=2)
        self.assertEqual(res.status_code, 404)

    def test_nonexistent_business_returns_404(self):
        res = self._get(999)
        self.assertEqual(res.status_code, 404)

    def test_another_owners_business_and_nonexistent_business_get_identical_response(self):
        business = self.businesses.create(1, "Shop A")
        other_owner_res = self._get(business["id"], uid=2)
        nonexistent_res = self._get(999, uid=1)
        self.assertEqual(other_owner_res.status_code, nonexistent_res.status_code)
        self.assertEqual(other_owner_res.get_json(), nonexistent_res.get_json())

    # ---- ENTITLEMENT ----

    def test_no_pos_entitlement_returns_403(self):
        business = self.businesses.create(1, "Shop A")
        self.state["subscription_row"] = None  # not business power, no subscription
        res = self._get(business["id"])
        self.assertEqual(res.status_code, 403)

    def test_business_power_owner_is_granted_access(self):
        business = self.businesses.create(1, "Shop A")
        self.state["user_row"] = dict(BUSINESS_POWER_USER)
        self.state["subscription_row"] = None
        res = self._get(business["id"])
        self.assertEqual(res.status_code, 200)

    # ---- ZERO SALES ----

    def test_zero_sales_returns_zero_values(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"])
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["today"], {"sale_count": 0, "total_amount": 0})
        self.assertEqual(body["week"], {"sale_count": 0, "total_amount": 0})

    # ---- BOUNDARIES ----

    def test_sale_earlier_today_counts_in_today_and_week(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add(business["id"], 500, _day_start(self.now) + timedelta(hours=1))

        res = self._get(business["id"])

        body = res.get_json()
        self.assertEqual(body["today"], {"sale_count": 1, "total_amount": 500})
        self.assertEqual(body["week"], {"sale_count": 1, "total_amount": 500})

    def test_sale_from_yesterday_excluded_from_today_but_counts_in_week(self):
        business = self.businesses.create(1, "Shop A")
        yesterday = _day_start(self.now) - timedelta(seconds=1)
        # Only valid when yesterday is still inside this ISO week --
        # guaranteed by self.now being a Thursday (day_start - 1s is
        # still Wednesday, same week).
        self.assertGreaterEqual(yesterday, _week_start(self.now))
        self.sales.add(business["id"], 700, yesterday)

        res = self._get(business["id"])

        body = res.get_json()
        self.assertEqual(body["today"], {"sale_count": 0, "total_amount": 0})
        self.assertEqual(body["week"], {"sale_count": 1, "total_amount": 700})

    def test_sale_from_last_week_excluded_from_both(self):
        business = self.businesses.create(1, "Shop A")
        last_week = _week_start(self.now) - timedelta(seconds=1)
        self.sales.add(business["id"], 900, last_week)

        res = self._get(business["id"])

        body = res.get_json()
        self.assertEqual(body["today"], {"sale_count": 0, "total_amount": 0})
        self.assertEqual(body["week"], {"sale_count": 0, "total_amount": 0})

    def test_sale_exactly_at_day_boundary_is_included(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add(business["id"], 100, _day_start(self.now))

        res = self._get(business["id"])

        self.assertEqual(res.get_json()["today"]["sale_count"], 1)

    def test_sale_exactly_at_week_boundary_is_included(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add(business["id"], 100, _week_start(self.now))

        res = self._get(business["id"])

        self.assertEqual(res.get_json()["week"]["sale_count"], 1)

    # ---- COUNT / TOTAL CORRECTNESS ----

    def test_correct_sale_count_and_total_for_multiple_sales_today(self):
        business = self.businesses.create(1, "Shop A")
        today_start = _day_start(self.now)
        self.sales.add(business["id"], 1000, today_start + timedelta(hours=1))
        self.sales.add(business["id"], 2500, today_start + timedelta(hours=2))
        self.sales.add(business["id"], 1500, today_start + timedelta(hours=3))

        res = self._get(business["id"])

        body = res.get_json()
        self.assertEqual(body["today"], {"sale_count": 3, "total_amount": 5000})
        self.assertEqual(body["week"], {"sale_count": 3, "total_amount": 5000})

    def test_amounts_are_returned_in_minor_units_unmodified(self):
        business = self.businesses.create(1, "Shop A")
        self.sales.add(business["id"], 123456, _day_start(self.now))

        res = self._get(business["id"])

        self.assertEqual(res.get_json()["today"]["total_amount"], 123456)

    def test_business_isolation_another_businesss_sales_are_excluded(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        self.sales.add(business_a["id"], 1000, self.now)
        self.sales.add(business_b["id"], 9999, self.now)

        res = self._get(business_a["id"])

        body = res.get_json()
        self.assertEqual(body["today"], {"sale_count": 1, "total_amount": 1000})

    # ---- SERVER TIME IS AUTHORITATIVE ----

    def test_as_of_reflects_the_databases_now_not_a_client_supplied_value(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"])

        self.assertEqual(res.get_json()["as_of"], self.now.isoformat())

    def test_changing_the_databases_now_shifts_which_sales_count_as_today(self):
        business = self.businesses.create(1, "Shop A")
        sale_time = self.now
        self.sales.add(business["id"], 100, sale_time)

        res_same_day = self._get(business["id"])
        self.assertEqual(res_same_day.get_json()["today"]["sale_count"], 1)

        # Advance the database's own clock (never the test process's real
        # clock) past midnight -- the same sale must no longer count as
        # "today" once NOW() has moved to the next day.
        self.state["now"] = sale_time + timedelta(days=1)
        res_next_day = self._get(business["id"])
        self.assertEqual(res_next_day.get_json()["today"]["sale_count"], 0)

    # ---- SUCCESS SHAPE ----

    def test_authenticated_owner_success_response_shape(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"])

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(set(body.keys()), {"today", "week", "as_of"})
        self.assertEqual(set(body["today"].keys()), {"sale_count", "total_amount"})
        self.assertEqual(set(body["week"].keys()), {"sale_count", "total_amount"})


if __name__ == "__main__":
    unittest.main()
