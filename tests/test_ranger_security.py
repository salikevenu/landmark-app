"""Access-control tests for the Rank/Ranger Network feature.

Verifies actual HTTP-layer authorization (not just "the frontend hides the
link"): normal users must get 403 from every admin Ranger endpoint, admins
(JWT role claim + live DB re-check) must get through, and the user-facing
/api/rank/* routes must never accept or leak another user's id.
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from routes.rank_routes import rank_bp
from routes.admin_ranger_routes import admin_ranger_bp


def _conn(row_mapping):
    class Conn:
        def execute(self, query, params=None):
            res = MagicMock()
            res.fetchone.return_value = (
                SimpleNamespace(_mapping=row_mapping) if row_mapping is not None else None
            )
            res.fetchall.return_value = []
            res.rowcount = 0
            res.scalar.return_value = 0
            return res

        def close(self):
            return None

        def commit(self):
            return None

        def rollback(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Conn()


def _make_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
        JWT_TOKEN_LOCATION=["headers"],
        JWT_COOKIE_CSRF_PROTECT=False,
        TESTING=True,
    )
    JWTManager(app)
    app.register_blueprint(rank_bp)
    app.register_blueprint(admin_ranger_bp)
    return app


class RankUserApiSecurityTests(unittest.TestCase):
    """Requirement: user APIs never accept/return another user's data."""

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    def test_rank_me_requires_auth(self):
        res = self.client.get("/api/rank/me")
        self.assertEqual(res.status_code, 401)

    def test_rank_me_route_has_no_id_parameter(self):
        """Structural IDOR immunity: no /api/rank/<id> route exists at all."""
        rules = [str(r) for r in self.app.url_map.iter_rules()]
        rank_rules = [r for r in rules if r.startswith("/api/rank")]
        for rule in rank_rules:
            self.assertNotIn("<", rule, f"user rank route must not take an id parameter: {rule}")

    def test_rank_me_response_never_contains_hierarchy_fields(self):
        row = {
            "rank": "member", "verified_users_count": 2, "active_subscribers_count": 1,
            "qualified_members_count": 0, "qualified_guides_count": 0, "qualified_leaders_count": 0,
            "updated_at": "2026-01-01",
        }
        with patch("services.rank_service.get_db_connection", return_value=_conn(row)):
            res = self.client.get("/api/rank/me", headers={"Authorization": f"Bearer {self._token(1)}"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        flat_text = str(body).lower()
        for forbidden in ("referred_by", "downline", "upline", "sponsor", "children", "parent", "tree", "network"):
            self.assertNotIn(forbidden, flat_text, f"leaked forbidden field/term: {forbidden}")

    def test_rank_achievements_and_rewards_require_auth(self):
        self.assertEqual(self.client.get("/api/rank/achievements").status_code, 401)
        self.assertEqual(self.client.get("/api/rank/rewards").status_code, 401)

    def test_rank_me_response_never_contains_global_pool_fields(self):
        """Global Leader reward-pool/budget data (10.4) must never reach a
        normal user's own /api/rank/me response, only the admin-only
        /api/admin/ranger/leader-pool endpoint."""
        row = {
            "rank": "leader", "verified_users_count": 500, "active_subscribers_count": 100,
            "qualified_members_count": 30, "qualified_guides_count": 10, "qualified_leaders_count": 0,
            "updated_at": "2026-01-01",
        }
        with patch("services.rank_service.get_db_connection", return_value=_conn(row)):
            res = self.client.get("/api/rank/me", headers={"Authorization": f"Bearer {self._token(1)}"})
        body = res.get_json()
        flat_text = str(body).lower()
        for forbidden in (
            "leader_pool_cap_inr", "leader_pool_allocated_inr", "leader_pool_remaining_inr",
            "eligible_leader_count", "rewarded_leader_count", "budget_exhausted_leader_count",
            "pool_cap", "pool_allocated", "pool_remaining",
        ):
            self.assertNotIn(forbidden, flat_text, f"leaked global pool field: {forbidden}")

    def test_rank_rewards_response_never_contains_global_pool_fields(self):
        with patch("services.rank_service.get_db_connection", return_value=_conn(None)):
            res = self.client.get("/api/rank/rewards", headers={"Authorization": f"Bearer {self._token(1)}"})
        body = res.get_json()
        flat_text = str(body).lower()
        for forbidden in ("leader_pool", "eligible_leader_count", "budget_exhausted", "pool_cap", "pool_remaining"):
            self.assertNotIn(forbidden, flat_text, f"leaked global pool field: {forbidden}")

    def test_rank_apis_never_contain_member_or_guide_global_pool_fields(self):
        """Same isolation guarantee (10.4-style) extended to the new
        Member/Guide revenue-backed pools: global pool status must only be
        reachable via the admin-only /api/admin/ranger/{member,guide}-pool
        endpoints, never through a normal user's own rank/reward views."""
        row = {
            "rank": "member", "verified_users_count": 10, "active_subscribers_count": 2,
            "qualified_members_count": 0, "qualified_guides_count": 0, "qualified_leaders_count": 0,
            "updated_at": "2026-01-01",
        }
        with patch("services.rank_service.get_db_connection", return_value=_conn(row)):
            me_res = self.client.get("/api/rank/me", headers={"Authorization": f"Bearer {self._token(1)}"})
        with patch("services.rank_service.get_db_connection", return_value=_conn(None)):
            rewards_res = self.client.get("/api/rank/rewards", headers={"Authorization": f"Bearer {self._token(1)}"})
        flat_text = (str(me_res.get_json()) + str(rewards_res.get_json())).lower()
        for forbidden in (
            "member_pool", "guide_pool", "ranger_pool", "eligible_count", "rewarded_count",
            "pool_amount", "pool_allocated", "pool_remaining", "budget_exhausted_count",
        ):
            self.assertNotIn(forbidden, flat_text, f"leaked global pool field: {forbidden}")


class RangerAdminAccessTests(unittest.TestCase):
    """Requirement: only admin_required (JWT claim + DB re-check) may pass."""

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()

    def _token(self, uid, role="free"):
        with self.app.app_context():
            return create_access_token(identity=str(uid), additional_claims={"role": role})

    ADMIN_GET_ENDPOINTS = [
        "/api/admin/ranger/overview",
        "/api/admin/ranger/network",
        "/api/admin/ranger/users/5",
        "/api/admin/ranger/export.csv",
        "/api/admin/ranger/config",
        "/api/admin/ranger/leader-pool",
        "/api/admin/ranger/member-pool",
        "/api/admin/ranger/guide-pool",
        "/api/admin/ranger/ranger-pool",
    ]
    ADMIN_POST_ENDPOINTS = [
        "/api/admin/ranger/recompute",
        "/api/admin/ranger/rewards/1/approve",
        "/api/admin/ranger/rewards/1/reject",
        "/api/admin/ranger/rewards/1/paid",
    ]

    def test_no_auth_at_all_is_401_on_every_admin_ranger_endpoint(self):
        for url in self.ADMIN_GET_ENDPOINTS:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 401, f"{url} should require auth")
        for url in self.ADMIN_POST_ENDPOINTS:
            res = self.client.post(url)
            self.assertEqual(res.status_code, 401, f"{url} should require auth")

    def test_normal_user_role_claim_denied_on_every_admin_ranger_endpoint(self):
        """Examples from the spec: GET hierarchy, GET a user's network,
        GET upline/downline equivalents — all must fail for a normal user."""
        token = self._token(42, role="free")
        headers = {"Authorization": f"Bearer {token}"}
        for url in self.ADMIN_GET_ENDPOINTS:
            res = self.client.get(url, headers=headers)
            self.assertEqual(res.status_code, 403, f"{url} should be admin-only")
        for url in self.ADMIN_POST_ENDPOINTS:
            res = self.client.post(url, headers=headers)
            self.assertEqual(res.status_code, 403, f"{url} should be admin-only")

    def test_jwt_role_claims_admin_but_db_disagrees_is_still_denied(self):
        """Forged/stale JWT claim alone must not be enough — the live DB
        re-check (services.authz.db_user_is_admin) must also pass."""
        token = self._token(42, role="admin")
        headers = {"Authorization": f"Bearer {token}"}
        with patch("services.authz.get_db_connection", return_value=_conn({"role": "free"})):
            res = self.client.get("/api/admin/ranger/overview", headers=headers)
        self.assertEqual(res.status_code, 403)

    def test_admin_with_db_confirmed_role_can_access_overview(self):
        token = self._token(1, role="admin")
        headers = {"Authorization": f"Bearer {token}"}
        overview_row = {"rank": "member", "c": 0, "total": 0, "user_id": 1}
        with patch("services.authz.get_db_connection", return_value=_conn({"role": "admin"})), \
             patch("services.rank_service.get_db_connection", return_value=_conn(overview_row)):
            res = self.client.get("/api/admin/ranger/overview", headers=headers)
        self.assertEqual(res.status_code, 200)

    def test_admin_can_view_another_users_network_detail(self):
        """The one place cross-user data legitimately flows — must be admin-gated."""
        token = self._token(1, role="admin")
        headers = {"Authorization": f"Bearer {token}"}
        user_row = {
            "id": 5, "phone": "9000000005", "name": "Test", "referral_code": "ABCDEF12",
            "referred_by": None, "created_at": "2026-01-01", "is_active": 1, "is_blocked": 0,
            "plan": "free", "subscription_expiry": None, "rank": "member",
            "verified_users_count": 0, "active_subscribers_count": 0,
            "qualified_members_count": 0, "qualified_guides_count": 0, "qualified_leaders_count": 0,
            "stats_updated_at": None,
        }
        with patch("services.authz.get_db_connection", return_value=_conn({"role": "admin"})), \
             patch("services.rank_service.get_db_connection", return_value=_conn(user_row)):
            res = self.client.get("/api/admin/ranger/users/5", headers=headers)
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        # Empty network (scenario 18 at the API layer): no children/parent -> handled, not an error.
        self.assertEqual(body["children"], [])
        self.assertIsNone(body["parent"])

    def test_admin_user_detail_missing_user_is_404_not_500(self):
        token = self._token(1, role="admin")
        headers = {"Authorization": f"Bearer {token}"}
        with patch("services.authz.get_db_connection", return_value=_conn({"role": "admin"})), \
             patch("services.rank_service.get_db_connection", return_value=_conn(None)):
            res = self.client.get("/api/admin/ranger/users/9999", headers=headers)
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
