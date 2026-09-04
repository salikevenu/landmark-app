"""POS business identity: GET/POST /api/pos/businesses (routes/pos_routes.py).

No real database connection — pos_routes.get_db_connection is patched with
an in-memory fake, matching tests/test_referral_attribution.py's pattern.
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
        # A real datetime, not a string — matches what psycopg returns for
        # a TIMESTAMP column, since routes/pos_routes.py now calls
        # .isoformat() on it directly.
        row = {
            "id": self.next_id,
            "owner_user_id": owner_user_id,
            "name": name,
            "created_at": datetime(2026, 9, 4) + timedelta(seconds=self.next_id),
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def for_owner(self, owner_user_id):
        return sorted(
            (r for r in self.rows if r["owner_user_id"] == owner_user_id),
            key=lambda r: r["id"],
        )


class FakeConn:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}
        if q.startswith("select") and "from pos_businesses" in q:
            rows = self.store.for_owner(params.get("uid"))
            return FakeResult(rows=[FakeRow(dict(r)) for r in rows])
        if q.startswith("insert into pos_businesses"):
            row = self.store.create(params["uid"], params["name"])
            return FakeResult(FakeRow(dict(row)))
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


class PosBusinessesTests(unittest.TestCase):
    def setUp(self):
        self.store = PosBusinessStore()
        self.app = _make_app()
        self.client = self.app.test_client()
        patcher = patch("routes.pos_routes.get_db_connection", lambda: FakeConn(self.store))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth_headers(self, uid):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def test_unauthenticated_get_is_rejected(self):
        res = self.client.get("/api/pos/businesses")
        self.assertEqual(res.status_code, 401)

    def test_unauthenticated_post_is_rejected(self):
        res = self.client.post("/api/pos/businesses", json={"name": "My Shop"})
        self.assertEqual(res.status_code, 401)

    def test_new_user_gets_empty_list(self):
        res = self.client.get("/api/pos/businesses", headers=self._auth_headers(1))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"businesses": []})

    def test_create_business(self):
        res = self.client.post(
            "/api/pos/businesses", json={"name": "My Shop"}, headers=self._auth_headers(1)
        )
        self.assertEqual(res.status_code, 201)
        business = res.get_json()["business"]
        self.assertEqual(business["name"], "My Shop")
        self.assertIn("id", business)
        self.assertIn("created_at", business)

    def test_created_business_appears_in_list(self):
        headers = self._auth_headers(1)
        self.client.post("/api/pos/businesses", json={"name": "My Shop"}, headers=headers)

        res = self.client.get("/api/pos/businesses", headers=headers)

        businesses = res.get_json()["businesses"]
        self.assertEqual(len(businesses), 1)
        self.assertEqual(businesses[0]["name"], "My Shop")

    def test_two_businesses_can_belong_to_same_user(self):
        headers = self._auth_headers(1)
        self.client.post("/api/pos/businesses", json={"name": "Shop A"}, headers=headers)
        self.client.post("/api/pos/businesses", json={"name": "Shop B"}, headers=headers)

        res = self.client.get("/api/pos/businesses", headers=headers)

        names = {b["name"] for b in res.get_json()["businesses"]}
        self.assertEqual(names, {"Shop A", "Shop B"})

    def test_user_cannot_see_another_users_businesses(self):
        self.client.post(
            "/api/pos/businesses", json={"name": "User1 Shop"}, headers=self._auth_headers(1)
        )

        res = self.client.get("/api/pos/businesses", headers=self._auth_headers(2))

        self.assertEqual(res.get_json(), {"businesses": []})

    def test_each_user_sees_only_their_own_business_when_both_have_data(self):
        headers1 = self._auth_headers(1)
        headers2 = self._auth_headers(2)
        self.client.post("/api/pos/businesses", json={"name": "User1 Shop"}, headers=headers1)
        self.client.post("/api/pos/businesses", json={"name": "User2 Shop"}, headers=headers2)

        res1 = self.client.get("/api/pos/businesses", headers=headers1)
        res2 = self.client.get("/api/pos/businesses", headers=headers2)

        names1 = [b["name"] for b in res1.get_json()["businesses"]]
        names2 = [b["name"] for b in res2.get_json()["businesses"]]
        self.assertEqual(names1, ["User1 Shop"])
        self.assertEqual(names2, ["User2 Shop"])

    def test_post_rejects_missing_name(self):
        res = self.client.post("/api/pos/businesses", json={}, headers=self._auth_headers(1))
        self.assertEqual(res.status_code, 400)

    def test_post_rejects_empty_name(self):
        res = self.client.post(
            "/api/pos/businesses", json={"name": "   "}, headers=self._auth_headers(1)
        )
        self.assertEqual(res.status_code, 400)

    def test_post_rejects_integer_name(self):
        res = self.client.post(
            "/api/pos/businesses", json={"name": 123}, headers=self._auth_headers(1)
        )
        self.assertEqual(res.status_code, 400)

    def test_post_rejects_boolean_name(self):
        res = self.client.post(
            "/api/pos/businesses", json={"name": True}, headers=self._auth_headers(1)
        )
        self.assertEqual(res.status_code, 400)

    def test_post_rejects_list_name(self):
        res = self.client.post(
            "/api/pos/businesses", json={"name": ["My Shop"]}, headers=self._auth_headers(1)
        )
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
