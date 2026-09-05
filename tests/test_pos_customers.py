"""POS customers foundation: GET/POST /api/pos/businesses/<id>/customers
(routes/pos_routes.py).

No real database connection — pos_routes.get_db_connection is patched with
an in-memory fake, matching tests/test_pos_products.py's pattern. The fake
customer store raises sqlalchemy.exc.IntegrityError on a duplicate
(business_id, phone) insert, simulating the real unique index, so the
409-on-duplicate test exercises the actual code path (an IntegrityError
catch), not just a pre-check the fake happens to agree with.
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
from sqlalchemy.exc import IntegrityError

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


class PosCustomerStore:
    """Raises IntegrityError on a duplicate (business_id, phone) insert —
    simulating the real `uq_pos_customers_business_phone` unique index."""

    def __init__(self):
        self.rows = []
        self.next_id = 1

    def create(self, business_id, name, phone):
        if any(r["business_id"] == business_id and r["phone"] == phone for r in self.rows):
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        row = {
            "id": self.next_id,
            "business_id": business_id,
            "name": name,
            "phone": phone,
            "created_at": datetime(2026, 9, 4) + timedelta(seconds=self.next_id),
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def for_business(self, business_id):
        return sorted(
            (r for r in self.rows if r["business_id"] == business_id), key=lambda r: r["id"]
        )


class FakeConn:
    def __init__(self, businesses, customers):
        self.businesses = businesses
        self.customers = customers

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if q.startswith("select id from pos_businesses"):
            row = self.businesses.owned_by(params.get("business_id"), params.get("uid"))
            return FakeResult(row=FakeRow(dict(row)) if row else None)

        if q.startswith("select id, name, phone, created_at from pos_customers"):
            rows = self.customers.for_business(params.get("business_id"))
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])

        if q.startswith("insert into pos_customers"):
            row = self.customers.create(
                params["business_id"], params["name"], params["phone"]
            )
            return FakeResult(row=FakeRow(dict(row)))

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        return None

    def rollback(self):
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


class PosCustomersTests(unittest.TestCase):
    def setUp(self):
        self.businesses = PosBusinessStore()
        self.customers = PosCustomerStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        patcher = patch(
            "routes.pos_routes.get_db_connection",
            lambda: FakeConn(self.businesses, self.customers),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _get(self, business_id, uid=1):
        return self.client.get(
            f"/api/pos/businesses/{business_id}/customers", headers=self._auth_headers(uid)
        )

    def _post(self, business_id, body, uid=1):
        return self.client.post(
            f"/api/pos/businesses/{business_id}/customers",
            json=body,
            headers=self._auth_headers(uid),
        )

    # ---- AUTHENTICATION ----

    def test_get_unauthenticated_is_rejected(self):
        res = self.client.get("/api/pos/businesses/1/customers")
        self.assertEqual(res.status_code, 401)

    def test_post_unauthenticated_is_rejected(self):
        res = self.client.post("/api/pos/businesses/1/customers", json={"name": "A", "phone": "9876543210"})
        self.assertEqual(res.status_code, 401)

    # ---- BUSINESS ISOLATION ----

    def test_get_nonexistent_business_returns_404(self):
        res = self._get(999)
        self.assertEqual(res.status_code, 404)

    def test_post_nonexistent_business_returns_404(self):
        res = self._post(999, {"name": "A", "phone": "9876543210"})
        self.assertEqual(res.status_code, 404)

    def test_get_wrong_owner_business_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        res = self._get(business["id"], uid=2)
        self.assertEqual(res.status_code, 404)

    def test_post_wrong_owner_business_returns_404(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], {"name": "A", "phone": "9876543210"}, uid=2)
        self.assertEqual(res.status_code, 404)

    def test_customer_from_business_a_never_visible_through_business_b(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")
        self._post(business_a["id"], {"name": "Alice", "phone": "9876543210"})

        res_b = self._get(business_b["id"])

        self.assertEqual(res_b.get_json(), {"customers": []})

    # ---- GET ----

    def test_successful_list(self):
        business = self.businesses.create(1, "Shop A")
        self._post(business["id"], {"name": "Alice", "phone": "9876543210"})

        res = self._get(business["id"])

        self.assertEqual(res.status_code, 200)
        customers = res.get_json()["customers"]
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0]["name"], "Alice")
        self.assertEqual(customers[0]["phone"], "9876543210")
        self.assertIn("id", customers[0])
        self.assertIn("created_at", customers[0])

    def test_empty_list(self):
        business = self.businesses.create(1, "Shop A")

        res = self._get(business["id"])

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"customers": []})

    # ---- POST: success ----

    def test_successful_create(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(business["id"], {"name": "Alice", "phone": "9876543210"})

        self.assertEqual(res.status_code, 201)
        customer = res.get_json()["customer"]
        self.assertEqual(customer["name"], "Alice")
        self.assertEqual(customer["phone"], "9876543210")
        self.assertIn("id", customer)
        self.assertIn("created_at", customer)

    def test_phone_is_normalized_via_clean_phone(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(business["id"], {"name": "Alice", "phone": "+91-98765-43210"})

        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["customer"]["phone"], "9876543210")

    def test_name_is_trimmed(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(business["id"], {"name": "  Alice  ", "phone": "9876543210"})

        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["customer"]["name"], "Alice")

    def test_client_supplied_id_and_created_at_are_ignored(self):
        business = self.businesses.create(1, "Shop A")

        res = self._post(
            business["id"],
            {"name": "Alice", "phone": "9876543210", "id": 999, "created_at": "2000-01-01"},
        )

        customer = res.get_json()["customer"]
        self.assertEqual(customer["id"], 1)
        self.assertNotEqual(customer["created_at"], "2000-01-01")

    # ---- POST: validation ----

    def test_missing_body_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self.client.post(
            f"/api/pos/businesses/{business['id']}/customers",
            headers=self._auth_headers(1),
        )
        self.assertEqual(res.status_code, 400)

    def test_non_object_body_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], ["not", "an", "object"])
        self.assertEqual(res.status_code, 400)

    def test_missing_name_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], {"phone": "9876543210"})
        self.assertEqual(res.status_code, 400)

    def test_null_name_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], {"name": None, "phone": "9876543210"})
        self.assertEqual(res.status_code, 400)

    def test_empty_name_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], {"name": "", "phone": "9876543210"})
        self.assertEqual(res.status_code, 400)

    def test_whitespace_name_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], {"name": "   ", "phone": "9876543210"})
        self.assertEqual(res.status_code, 400)

    def test_missing_phone_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], {"name": "Alice"})
        self.assertEqual(res.status_code, 400)

    def test_invalid_phone_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], {"name": "Alice", "phone": "12345"})
        self.assertEqual(res.status_code, 400)

    def test_phone_with_wrong_leading_digit_returns_400(self):
        business = self.businesses.create(1, "Shop A")
        res = self._post(business["id"], {"name": "Alice", "phone": "1234567890"})
        self.assertEqual(res.status_code, 400)

    # ---- POST: duplicate policy ----

    def test_duplicate_normalized_phone_in_same_business_returns_409(self):
        business = self.businesses.create(1, "Shop A")
        self._post(business["id"], {"name": "Alice", "phone": "9876543210"})

        res = self._post(business["id"], {"name": "Alice Again", "phone": "+91 98765 43210"})

        self.assertEqual(res.status_code, 409)
        self.assertNotIn("customer", res.get_json())

    def test_same_phone_in_two_different_businesses_both_succeed(self):
        business_a = self.businesses.create(1, "Shop A")
        business_b = self.businesses.create(1, "Shop B")

        res_a = self._post(business_a["id"], {"name": "Alice", "phone": "9876543210"})
        res_b = self._post(business_b["id"], {"name": "Alice", "phone": "9876543210"})

        self.assertEqual(res_a.status_code, 201)
        self.assertEqual(res_b.status_code, 201)


if __name__ == "__main__":
    unittest.main()
