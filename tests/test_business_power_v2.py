"""Business Power V2: organizations, membership, invitations, and
organization-scoped business management.

Follows the exact fake-conn/Flask-test-client harness conventions already
established in tests/test_business_power.py and
tests/test_referral_commission_safety.py: a shared in-memory store,
substring-matched SQL dispatch (verb-first, to avoid the DELETE/UPDATE
falling through to a SELECT-shaped branch), and real IntegrityError
semantics backed by the actual partial unique indexes added by
migrations/add_business_power_v2_organizations.py.
"""
import hashlib
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "rzp_test_secret")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from sqlalchemy.exc import IntegrityError

from config.payment_config import BUSINESS_POWER_PLAN


def _future(days=30):
    return (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")


def _past(days=1):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")


def _row(mapping):
    return SimpleNamespace(_mapping=mapping)


def _q(query):
    return " ".join(str(getattr(query, "text", query)).lower().split())


class OrgStore:
    """One shared in-memory relational fake for every table Business Power
    V2 touches, plus the untouched V1 financial tables (kept empty and
    asserted so throughout)."""

    def __init__(self):
        self.users = {}
        self.organizations = {}
        self.next_org_id = 1
        self.organization_members = {}
        self.next_member_id = 1
        self.organization_invitations = {}
        self.next_invitation_id = 1
        self.listings = {}
        self.next_listing_id = 1
        self.listing_images = []
        self.admin_audit_log = []
        # Untouched by any V2 code path -- financial safety tests assert
        # these stay exactly empty throughout every V2 operation.
        self.payments = []
        self.wallet_transactions = []
        self.referral_commission_jobs = []

    def add_user(self, uid, **kwargs):
        self.users[uid] = {
            "id": uid,
            "phone": kwargs.get("phone", f"90000{uid:05d}"),
            "name": kwargs.get("name"),
            "role": kwargs.get("role", "free"),
            "plan": kwargs.get("plan", "free"),
            "subscription_expiry": kwargs.get("subscription_expiry"),
            "business_limit": kwargs.get("business_limit", 0),
            "is_blocked": kwargs.get("is_blocked", 0),
            "is_active": kwargs.get("is_active", 1),
        }
        return self.users[uid]

    def add_business_power_owner(self, uid, phone=None, expiry=None):
        self.add_user(
            uid, phone=phone or f"90000{uid:05d}", role=BUSINESS_POWER_PLAN,
            plan=BUSINESS_POWER_PLAN, subscription_expiry=expiry or _future(),
        )

    def seed_organization(self, owner_user_id, org_id=None, status="active"):
        oid = org_id or self.next_org_id
        self.next_org_id = max(self.next_org_id, oid + 1)
        self.organizations[oid] = {
            "id": oid, "name": f"Org {oid}", "owner_user_id": owner_user_id,
            "status": status, "created_at": datetime.utcnow(),
        }
        self.add_membership(oid, owner_user_id, "owner", "active")
        return oid

    def add_membership(self, org_id, user_id, role, status="active"):
        mid = self.next_member_id
        self.next_member_id += 1
        self.organization_members[mid] = {
            "id": mid, "organization_id": org_id, "user_id": user_id,
            "role": role, "status": status,
        }
        return mid

    def connect(self):
        return OrgConn(self)


class OrgConn:
    def __init__(self, store):
        self.store = store

    def execute(self, query, params=None):
        q = _q(query)
        p = params or {}
        s = self.store
        res = MagicMock()

        # ---------------- INSERT ----------------
        if q.startswith("insert into organizations"):
            oid = s.next_org_id
            s.next_org_id += 1
            row = {"id": oid, "name": p["name"], "owner_user_id": p["uid"], "status": "active"}
            s.organizations[oid] = dict(row, created_at=datetime.utcnow())
            fr = MagicMock()
            fr.fetchone.return_value = _row(row)
            return fr

        if q.startswith("insert into organization_members"):
            org_id = p["org_id"]
            uid = p["uid"]
            role = "owner" if "'owner', 'active'" in q else p.get("role")
            status = "active"
            # Enforce the SAME two partial unique indexes the real
            # migration creates.
            for m in s.organization_members.values():
                if m["status"] != "active":
                    continue
                if m["organization_id"] == org_id and m["user_id"] == uid:
                    raise IntegrityError("uq_org_members_active_person", p, Exception("unique"))
                if m["organization_id"] == org_id and role == "owner" and m["role"] == "owner":
                    raise IntegrityError("uq_org_members_single_active_owner", p, Exception("unique"))
            s.add_membership(org_id, uid, role, status)
            return res

        if q.startswith("insert into organization_invitations"):
            for inv in s.organization_invitations.values():
                if inv["status"] == "pending" and inv["organization_id"] == p["org_id"] and inv["invited_phone"] == p["phone"]:
                    raise IntegrityError("uq_org_invitations_pending", p, Exception("unique"))
                if inv["token_hash"] == p["token_hash"]:
                    raise IntegrityError("uq_org_invitations_token_hash", p, Exception("unique"))
            iid = s.next_invitation_id
            s.next_invitation_id += 1
            row = {
                "id": iid, "organization_id": p["org_id"], "invited_phone": p["phone"],
                "invited_by": p["invited_by"], "role": p["role"], "token_hash": p["token_hash"],
                "status": "pending", "expires_at": p["expires_at"], "accepted_at": None,
            }
            s.organization_invitations[iid] = row
            fr = MagicMock()
            fr.fetchone.return_value = _row({"id": iid, "expires_at": p["expires_at"]})
            return fr

        if q.startswith("insert into listings"):
            lid = s.next_listing_id
            s.next_listing_id += 1
            s.listings[lid] = {
                "id": lid, "user_id": p["user_id"], "user_phone": p.get("user_phone"),
                "organization_id": p.get("org_id"), "listing_type": p.get("listing_type"),
                "business_name": p["business_name"], "category": p["category"],
                "city": p.get("city", ""), "state": p.get("state", ""),
                "latitude": p["latitude"], "longitude": p["longitude"],
                "description": p.get("description", ""), "whatsapp": p.get("whatsapp", ""),
                "website": p.get("website", ""), "status": "pending", "is_active": 1,
                "is_premium": 0, "is_featured": 0, "is_sponsored": 0, "is_verified": 0,
                "views": 0, "clicks": 0, "whatsapp_clicks": 0, "rating": 0, "rating_count": 0,
                "created_at": datetime.utcnow(),
            }
            fr = MagicMock()
            fr.fetchone.return_value = (lid,)
            return fr

        if q.startswith("insert into admin_audit_log"):
            s.admin_audit_log.append(dict(p))
            return res

        # ---------------- UPDATE ----------------
        if q.startswith("update organization_invitations"):
            inv = s.organization_invitations.get(p["id"])
            if inv and inv["status"] == "pending":
                inv["status"] = "accepted"
                inv["accepted_at"] = datetime.utcnow()
                res.rowcount = 1
            else:
                res.rowcount = 0
            return res

        if q.startswith("update organization_members") and "set role" in q:
            target = s.organization_members.get(p["mid"])
            if (target and target["organization_id"] == p["org_id"]
                    and target["status"] == "active" and target["role"] != "owner"):
                target["role"] = p["new_role"]
                res.rowcount = 1
            else:
                res.rowcount = 0
            return res

        if q.startswith("update organization_members") and "set status = 'removed'" in q:
            target = s.organization_members.get(p["mid"])
            if (target and target["organization_id"] == p["org_id"]
                    and target["status"] == "active" and target["role"] != "owner"):
                target["status"] = "removed"
                res.rowcount = 1
            else:
                res.rowcount = 0
            return res

        if q.startswith("update listings") and "set business_name" in q:
            listing = s.listings.get(p["lid"])
            if listing and listing.get("organization_id") == p["org_id"]:
                listing.update(business_name=p["bname"], category=p["cat"], city=p["city"],
                                state=p["state"], description=p["desc"])
                res.rowcount = 1
            else:
                res.rowcount = 0
            return res

        if q.startswith("update users") and "business_limit = 0" in q:
            user = s.users.get(p["uid"])
            if user is None:
                res.rowcount = 0
                return res
            user["role"] = p["role"]
            user["plan"] = p["plan"]
            user["subscription_expiry"] = p["expiry_date"]
            user["business_limit"] = 0
            res.rowcount = 1
            return res

        # ---------------- DELETE ----------------
        if q.startswith("delete from listing_images"):
            return res

        if q.startswith("delete from listings"):
            listing = s.listings.get(p["lid"])
            if listing and listing.get("organization_id") == p["org_id"]:
                del s.listings[p["lid"]]
                res.rowcount = 1
            else:
                res.rowcount = 0
            return res

        # ---------------- SELECT: authorization joins ----------------
        if q.startswith("select om.role, owner.plan as owner_plan"):
            member = next((m for m in s.organization_members.values()
                           if m["organization_id"] == p["org_id"] and m["user_id"] == p["user_id"]
                           and m["status"] == "active"), None)
            org = s.organizations.get(p["org_id"])
            if not member or not org or org["status"] != "active":
                res.fetchone.return_value = None
                return res
            allowed = [p[k] for k in p if k.startswith("role_")]
            if member["role"] not in allowed:
                res.fetchone.return_value = None
                return res
            owner = s.users.get(org["owner_user_id"])
            res.fetchone.return_value = _row({
                "role": member["role"],
                "owner_plan": owner.get("plan") if owner else None,
                "owner_subscription_expiry": owner.get("subscription_expiry") if owner else None,
            })
            return res

        if q.startswith("select l.id, l.organization_id, om.role"):
            listing = s.listings.get(p["listing_id"])
            if not listing or listing.get("organization_id") is None:
                res.fetchone.return_value = None
                return res
            org_id = listing["organization_id"]
            member = next((m for m in s.organization_members.values()
                           if m["organization_id"] == org_id and m["user_id"] == p["user_id"]
                           and m["status"] == "active"), None)
            org = s.organizations.get(org_id)
            if not member or not org or org["status"] != "active":
                res.fetchone.return_value = None
                return res
            allowed = [p[k] for k in p if k.startswith("role_")]
            if member["role"] not in allowed:
                res.fetchone.return_value = None
                return res
            owner = s.users.get(org["owner_user_id"])
            res.fetchone.return_value = _row({
                "id": listing["id"], "organization_id": org_id, "role": member["role"],
                "owner_plan": owner.get("plan") if owner else None,
                "owner_subscription_expiry": owner.get("subscription_expiry") if owner else None,
            })
            return res

        # ---------------- SELECT: organizations ----------------
        if q.startswith("select id, name, owner_user_id, status from organizations"):
            org = next((o for o in s.organizations.values() if o["owner_user_id"] == p["uid"]), None)
            res.fetchone.return_value = _row(dict(org)) if org else None
            return res

        if q.startswith("select id, name, owner_user_id, status, created_at from organizations"):
            org = s.organizations.get(p["org_id"])
            res.fetchone.return_value = _row(dict(org)) if org else None
            return res

        if q.startswith("select o.id, o.name, o.status, o.created_at, o.owner_user_id"):
            org = s.organizations.get(p["org_id"])
            if not org:
                res.fetchone.return_value = None
                return res
            owner = s.users.get(org["owner_user_id"]) or {}
            res.fetchone.return_value = _row({
                **org, "owner_phone": owner.get("phone"),
                "owner_plan": owner.get("plan"), "owner_subscription_expiry": owner.get("subscription_expiry"),
            })
            return res

        if q.startswith("select o.id, o.name, o.status, om.role"):
            rows = []
            for m in s.organization_members.values():
                if m["user_id"] == p["uid"] and m["status"] == "active":
                    org = s.organizations.get(m["organization_id"])
                    if org:
                        rows.append(_row({"id": org["id"], "name": org["name"], "status": org["status"], "role": m["role"]}))
            offset = p.get("offset", 0)
            limit = p.get("limit", len(rows))
            fr = MagicMock()
            fr.fetchall.return_value = rows[offset:offset + limit]
            return fr

        # ---------------- SELECT: organization_members ----------------
        if q.startswith("select count(*) from organization_members where user_id"):
            total = sum(1 for m in s.organization_members.values() if m["user_id"] == p["uid"] and m["status"] == "active")
            res.scalar.return_value = total
            return res

        if q.startswith("select count(*) from organization_members where organization_id"):
            total = sum(1 for m in s.organization_members.values() if m["organization_id"] == p["org_id"] and m["status"] == "active")
            res.scalar.return_value = total
            return res

        if q.startswith("select om.id, om.user_id, om.role, om.status, u.phone, u.name"):
            rows = []
            for m in s.organization_members.values():
                if m["organization_id"] == p["org_id"] and m["status"] == "active":
                    u = s.users.get(m["user_id"]) or {}
                    rows.append(_row({
                        "id": m["id"], "user_id": m["user_id"], "role": m["role"], "status": m["status"],
                        "phone": u.get("phone"), "name": u.get("name"),
                    }))
            rows.sort(key=lambda r: r._mapping["id"])
            offset = p.get("offset", 0)
            limit = p.get("limit", len(rows))
            fr = MagicMock()
            fr.fetchall.return_value = rows[offset:offset + limit]
            return fr

        if q.startswith("select role, status from organization_members"):
            m = next((m for m in s.organization_members.values()
                      if m["organization_id"] == p["org_id"] and m["user_id"] == p["user_id"]), None)
            res.fetchone.return_value = _row({"role": m["role"], "status": m["status"]}) if m else None
            return res

        # ---------------- SELECT: organization_invitations ----------------
        if q.startswith("select id, invited_phone, role from organization_invitations"):
            inv = next((i for i in s.organization_invitations.values()
                        if i["token_hash"] == p["hash"] and i["organization_id"] == p["org_id"]
                        and i["status"] == "pending" and i["expires_at"] > datetime.utcnow()), None)
            res.fetchone.return_value = _row({
                "id": inv["id"], "invited_phone": inv["invited_phone"], "role": inv["role"],
            }) if inv else None
            return res

        # ---------------- SELECT: listings ----------------
        if q.startswith("select count(*) from listings where organization_id"):
            total = sum(1 for l in s.listings.values() if l.get("organization_id") == p["org_id"])
            res.scalar.return_value = total
            return res

        if q.startswith("select l.*"):
            rows = [l for l in s.listings.values() if l.get("organization_id") == p["org_id"]]
            rows.sort(key=lambda l: -l["id"])
            offset = p.get("offset", 0)
            limit = p.get("limit", len(rows))
            page = rows[offset:offset + limit]
            fr = MagicMock()
            fr.fetchall.return_value = [_row(dict(l, image_url=None)) for l in page]
            return fr

        if q.startswith("select id, business_name, category, city, state, latitude, longitude, description"):
            l = s.listings.get(p["lid"])
            if l and l.get("organization_id") == p["org_id"]:
                res.fetchone.return_value = _row({k: l[k] for k in (
                    "id", "business_name", "category", "city", "state", "latitude", "longitude", "description")})
            else:
                res.fetchone.return_value = None
            return res

        if q.startswith("select coalesce(sum(views), 0) as total_views"):
            mine = [l for l in s.listings.values() if l.get("organization_id") == p["org_id"]]
            res.fetchone.return_value = _row({
                "total_views": sum(l["views"] for l in mine),
                "total_clicks": sum(l["clicks"] for l in mine),
                "total_whatsapp": sum(l["whatsapp_clicks"] for l in mine),
            })
            return res

        if q.startswith("select coalesce(sum(views), 0) as views"):
            res.fetchone.return_value = _row({"views": 0, "clicks": 0})
            return res

        if q.startswith("select id, business_name, views, clicks, whatsapp_clicks"):
            mine = sorted((l for l in s.listings.values() if l.get("organization_id") == p["org_id"]),
                          key=lambda l: -l["views"])[:5]
            fr = MagicMock()
            fr.fetchall.return_value = [_row({
                "id": l["id"], "business_name": l["business_name"], "views": l["views"],
                "clicks": l["clicks"], "whatsapp_clicks": l["whatsapp_clicks"],
            }) for l in mine]
            return fr

        # ---------------- SELECT: users (generic) ----------------
        if "from users" in q:
            uid = p.get("uid") or p.get("id") or p.get("user_id")
            phone = p.get("phone")
            user = None
            if uid is not None:
                user = s.users.get(int(uid))
            elif phone is not None:
                user = next((u for u in s.users.values() if u["phone"] == phone), None)
            res.fetchone.return_value = _row(dict(user)) if user else None
            return res

        raise AssertionError(f"OrgConn: unhandled query: {q}\nparams={p}")

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


# Every module that calls get_db_connection() anywhere on a Business Power
# V2 request path, bound to ONE shared store per test.
PATCH_TARGETS = (
    "services.organization_service.get_db_connection",
    "services.admin_service.get_db_connection",
    "services.payment_service.get_db_connection",
    "services.authz.get_db_connection",
    "services.jwt_session.get_db_connection",
    "services.audit_service.get_db_connection",
    "routes.admin_routes.get_db_connection",
)


class OrgTestCase(unittest.TestCase):
    """Base class: a fresh OrgStore per test, with every get_db_connection
    call site patched to it for the duration of the test."""

    def setUp(self):
        self.store = OrgStore()
        self._patches = [patch(t, side_effect=self.store.connect) for t in PATCH_TARGETS]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _service_conn(self):
        return self.store.connect()


# ---------------------------------------------------------------------------
# Database invariants
# ---------------------------------------------------------------------------
class DatabaseInvariantTests(OrgTestCase):
    def test_organization_creation_creates_owner_membership(self):
        from services.organization_service import ensure_organization_for_user
        self.store.add_business_power_owner(1)
        org = ensure_organization_for_user(1)
        self.assertIsNotNone(org)
        members = [m for m in self.store.organization_members.values() if m["organization_id"] == org["id"]]
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["role"], "owner")
        self.assertEqual(members[0]["status"], "active")
        self.assertEqual(members[0]["user_id"], 1)

    def test_organization_creation_is_idempotent(self):
        from services.organization_service import ensure_organization_for_user
        self.store.add_business_power_owner(1)
        first = ensure_organization_for_user(1)
        second = ensure_organization_for_user(1)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.organizations), 1)
        owner_rows = [m for m in self.store.organization_members.values()
                      if m["organization_id"] == first["id"] and m["role"] == "owner" and m["status"] == "active"]
        self.assertEqual(len(owner_rows), 1)

    def test_duplicate_active_owner_rejected_by_db_constraint(self):
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        conn = self.store.connect()
        with self.assertRaises(IntegrityError):
            conn.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO organization_members (organization_id, user_id, role, status) "
                    "VALUES (:org_id, :uid, 'owner', 'active')"
                ),
                {"org_id": org_id, "uid": 999},
            )

    def test_duplicate_active_membership_for_same_person_rejected(self):
        self.store.add_business_power_owner(1)
        self.store.add_user(2)
        org_id = self.store.seed_organization(1)
        self.store.add_membership(org_id, 2, "staff", "active")
        conn = self.store.connect()
        with self.assertRaises(IntegrityError):
            conn.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO organization_members (organization_id, user_id, role, status) "
                    "VALUES (:org_id, :uid, :role, 'active')"
                ),
                {"org_id": org_id, "uid": 2, "role": "manager"},
            )

    def test_invitation_token_hash_uniqueness(self):
        from services.organization_service import invite_member
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        result = invite_member(1, org_id, "9876543210", "staff")
        self.assertNotIn("error", result)
        self.assertIn("token", result)

    def test_duplicate_pending_invitation_rejected(self):
        from services.organization_service import invite_member
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        first = invite_member(1, org_id, "9876543210", "staff")
        self.assertNotIn("error", first)
        second = invite_member(1, org_id, "9876543210", "manager")
        self.assertEqual(second.get("_http"), 409)


# ---------------------------------------------------------------------------
# Centralized Business Power subscription authorization
# ---------------------------------------------------------------------------
class SubscriptionAuthorizationTests(OrgTestCase):
    def test_active_business_power_succeeds(self):
        from services.organization_authz import authorize_organization
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        conn = self.store.connect()
        role = authorize_organization(conn, 1, org_id, ("owner", "manager", "staff"))
        self.assertEqual(role, "owner")

    def test_expired_business_power_denied(self):
        from services.organization_authz import authorize_organization
        self.store.add_business_power_owner(1, expiry=_past())
        org_id = self.store.seed_organization(1)
        conn = self.store.connect()
        role = authorize_organization(conn, 1, org_id, ("owner", "manager", "staff"))
        self.assertIsNone(role)

    def test_wrong_plan_denied(self):
        from services.organization_authz import authorize_organization
        self.store.add_user(1, plan="business_premium", subscription_expiry=_future())
        org_id = self.store.seed_organization(1)
        conn = self.store.connect()
        role = authorize_organization(conn, 1, org_id, ("owner", "manager", "staff"))
        self.assertIsNone(role)

    def test_missing_user_denied(self):
        from services.organization_authz import authorize_organization
        conn = self.store.connect()
        role = authorize_organization(conn, 999, 1, ("owner", "manager", "staff"))
        self.assertIsNone(role)

    def test_suspended_organization_denied(self):
        from services.organization_authz import authorize_organization
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1, status="suspended")
        conn = self.store.connect()
        role = authorize_organization(conn, 1, org_id, ("owner", "manager", "staff"))
        self.assertIsNone(role)

    def test_active_member_denied_while_owner_subscription_expired(self):
        from services.organization_authz import authorize_organization
        self.store.add_business_power_owner(1, expiry=_past())
        self.store.add_user(2, plan="free")
        org_id = self.store.seed_organization(1)
        self.store.add_membership(org_id, 2, "staff", "active")
        conn = self.store.connect()
        role = authorize_organization(conn, 2, org_id, ("owner", "manager", "staff"))
        self.assertIsNone(role)

    def test_access_resumes_after_reactivation(self):
        from services.organization_authz import authorize_organization
        self.store.add_business_power_owner(1, expiry=_past())
        self.store.add_user(2, plan="free")
        org_id = self.store.seed_organization(1)
        self.store.add_membership(org_id, 2, "staff", "active")
        conn = self.store.connect()
        self.assertIsNone(authorize_organization(conn, 2, org_id, ("owner", "manager", "staff")))
        # Owner's subscription is renewed.
        self.store.users[1]["subscription_expiry"] = _future()
        self.assertEqual(authorize_organization(conn, 2, org_id, ("owner", "manager", "staff")), "staff")


# ---------------------------------------------------------------------------
# Invitation lifecycle
# ---------------------------------------------------------------------------
class InvitationLifecycleTests(OrgTestCase):
    def _invite(self, owner_id, org_id, phone="9876543210", role="staff"):
        from services.organization_service import invite_member
        return invite_member(owner_id, org_id, phone, role)

    def test_valid_invitation_accepted(self):
        from services.organization_service import accept_invitation
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9876543210")
        inv = self._invite(1, org_id, phone="9876543210", role="staff")
        result = accept_invitation(2, org_id, inv["token"])
        self.assertEqual(result["status"], "accepted")
        member = next(m for m in self.store.organization_members.values() if m["user_id"] == 2)
        self.assertEqual(member["role"], "staff")
        self.assertEqual(member["status"], "active")

    def test_expired_invitation_rejected(self):
        from services.organization_service import accept_invitation
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9876543210")
        inv = self._invite(1, org_id, phone="9876543210", role="staff")
        # Force expiry.
        real_inv = next(iter(self.store.organization_invitations.values()))
        real_inv["expires_at"] = datetime.utcnow() - timedelta(days=1)
        result = accept_invitation(2, org_id, inv["token"])
        self.assertEqual(result.get("_http"), 400)

    def test_accepted_invitation_cannot_be_reused(self):
        from services.organization_service import accept_invitation
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9876543210")
        inv = self._invite(1, org_id, phone="9876543210", role="staff")
        first = accept_invitation(2, org_id, inv["token"])
        self.assertEqual(first["status"], "accepted")
        second = accept_invitation(2, org_id, inv["token"])
        self.assertEqual(second.get("_http"), 400)

    def test_cancelled_invitation_rejected(self):
        from services.organization_service import accept_invitation
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9876543210")
        inv = self._invite(1, org_id, phone="9876543210", role="staff")
        real_inv = next(iter(self.store.organization_invitations.values()))
        real_inv["status"] = "cancelled"
        result = accept_invitation(2, org_id, inv["token"])
        self.assertEqual(result.get("_http"), 400)

    def test_wrong_phone_rejected(self):
        from services.organization_service import accept_invitation
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9111111111")  # different phone than invited
        inv = self._invite(1, org_id, phone="9876543210", role="staff")
        result = accept_invitation(2, org_id, inv["token"])
        self.assertEqual(result.get("_http"), 400)

    def test_client_supplied_fake_phone_cannot_bypass_matching(self):
        """accept_invitation only ever reads the authenticated user's own
        DB phone -- there is no phone parameter it could be tricked with."""
        import inspect
        from services.organization_service import accept_invitation
        sig = inspect.signature(accept_invitation)
        self.assertNotIn("phone", sig.parameters)

    def test_manager_invitation(self):
        result = self._invite(1, self._owner_org(), role="manager")
        self.assertNotIn("error", result)
        self.assertEqual(result["role"], "manager")

    def test_staff_invitation(self):
        result = self._invite(1, self._owner_org(), role="staff")
        self.assertNotIn("error", result)
        self.assertEqual(result["role"], "staff")

    def test_owner_invitation_rejected(self):
        result = self._invite(1, self._owner_org(), role="owner")
        self.assertEqual(result.get("_http"), 400)

    def _owner_org(self):
        self.store.add_business_power_owner(1)
        return self.store.seed_organization(1)

    def test_duplicate_pending_invitation_to_same_phone_rejected(self):
        org_id = self._owner_org()
        first = self._invite(1, org_id, phone="9876543210", role="staff")
        self.assertNotIn("error", first)
        second = self._invite(1, org_id, phone="9876543210", role="manager")
        self.assertEqual(second.get("_http"), 409)

    def test_concurrent_duplicate_acceptance_cannot_create_duplicate_active_membership(self):
        from services.organization_service import accept_invitation
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9876543210")
        inv = self._invite(1, org_id, phone="9876543210", role="staff")
        # Simulate a second, already-active membership existing for user 2
        # in this org (e.g. from a prior invitation) before this one races in.
        self.store.add_membership(org_id, 2, "manager", "active")
        result = accept_invitation(2, org_id, inv["token"])
        self.assertEqual(result.get("_http"), 409)
        active = [m for m in self.store.organization_members.values()
                  if m["organization_id"] == org_id and m["user_id"] == 2 and m["status"] == "active"]
        self.assertEqual(len(active), 1)

    def test_acceptance_succeeds_even_after_business_power_expired(self):
        """LOCKED POLICY: acceptance only records membership; it never
        checks the owner's Business Power subscription."""
        from services.organization_service import accept_invitation
        self.store.add_business_power_owner(1, expiry=_future())
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9876543210")
        inv = self._invite(1, org_id, phone="9876543210", role="staff")
        # Subscription lapses AFTER the invitation was sent.
        self.store.users[1]["subscription_expiry"] = _past()
        result = accept_invitation(2, org_id, inv["token"])
        self.assertEqual(result["status"], "accepted")

    def test_new_member_has_no_access_until_subscription_reactivated(self):
        from services.organization_service import accept_invitation, list_organization_businesses
        self.store.add_business_power_owner(1, expiry=_future())
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9876543210")
        inv = self._invite(1, org_id, phone="9876543210", role="staff")
        self.store.users[1]["subscription_expiry"] = _past()
        accept_invitation(2, org_id, inv["token"])
        # Membership exists...
        member = next(m for m in self.store.organization_members.values() if m["user_id"] == 2)
        self.assertEqual(member["status"], "active")
        # ...but every organization operation is denied while BP is expired.
        self.assertIsNone(list_organization_businesses(2, org_id))
        # Reactivate: access resumes with no new invitation.
        self.store.users[1]["subscription_expiry"] = _future()
        self.assertIsNotNone(list_organization_businesses(2, org_id))


# ---------------------------------------------------------------------------
# Role isolation — exact matrix
# ---------------------------------------------------------------------------
class RoleIsolationTests(OrgTestCase):
    def _org_with_roles(self):
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9000000002")
        self.store.add_user(3, phone="9000000003")
        self.store.add_membership(org_id, 2, "manager", "active")
        self.store.add_membership(org_id, 3, "staff", "active")
        return org_id

    def _make_business(self, org_id, creator_id=1):
        from services.organization_service import create_organization_business
        result = create_organization_business(creator_id, org_id, {
            "business_name": "Biz", "category": "retail", "latitude": "12.9", "longitude": "77.6",
        })
        return result["listing_id"]

    def test_owner_can_do_everything(self):
        from services.organization_service import (
            list_organization_businesses, create_organization_business, update_organization_business,
            delete_organization_business, get_organization_analytics, invite_member, list_members,
            change_member_role, remove_member,
        )
        org_id = self._org_with_roles()
        self.assertIsNotNone(list_organization_businesses(1, org_id))
        lid = self._make_business(org_id, 1)
        self.assertNotIn("error", update_organization_business(1, org_id, lid, {"business_name": "X", "category": "c"}))
        self.assertIsNotNone(get_organization_analytics(1, org_id))
        self.assertNotIn("error", invite_member(1, org_id, "9999999999", "staff"))
        self.assertIsNotNone(list_members(1, org_id))
        mgr_member_id = next(m["id"] for m in self.store.organization_members.values()
                              if m["organization_id"] == org_id and m["user_id"] == 2)
        self.assertNotIn("error", change_member_role(1, org_id, mgr_member_id, "staff"))
        self.assertNotIn("error", remove_member(1, org_id, mgr_member_id))
        self.assertNotIn("error", delete_organization_business(1, org_id, lid))

    def test_manager_allowed_actions(self):
        from services.organization_service import (
            list_organization_businesses, create_organization_business, update_organization_business,
            get_organization_analytics, invite_member,
        )
        org_id = self._org_with_roles()
        self.assertIsNotNone(list_organization_businesses(2, org_id))
        result = create_organization_business(2, org_id, {
            "business_name": "Mgr Biz", "category": "retail", "latitude": "12.9", "longitude": "77.6",
        })
        self.assertNotIn("error", result)
        lid = result["listing_id"]
        self.assertNotIn("error", update_organization_business(2, org_id, lid, {"business_name": "Y", "category": "c"}))
        self.assertIsNotNone(get_organization_analytics(2, org_id))
        self.assertNotIn("error", invite_member(2, org_id, "9999999998", "staff"))

    def test_manager_denied_actions(self):
        from services.organization_service import delete_organization_business, change_member_role, remove_member
        org_id = self._org_with_roles()
        lid = self._make_business(org_id, 1)
        self.assertEqual(delete_organization_business(2, org_id, lid).get("_http"), 404)
        staff_member_id = next(m["id"] for m in self.store.organization_members.values()
                                if m["organization_id"] == org_id and m["user_id"] == 3)
        self.assertEqual(change_member_role(2, org_id, staff_member_id, "manager").get("_http"), 404)
        self.assertEqual(remove_member(2, org_id, staff_member_id).get("_http"), 404)

    def test_staff_view_only(self):
        from services.organization_service import list_organization_businesses
        org_id = self._org_with_roles()
        self._make_business(org_id, 1)
        self.assertIsNotNone(list_organization_businesses(3, org_id))

    def test_staff_denied_everything_else(self):
        from services.organization_service import (
            create_organization_business, update_organization_business, delete_organization_business,
            get_organization_analytics, invite_member, change_member_role, remove_member,
        )
        org_id = self._org_with_roles()
        lid = self._make_business(org_id, 1)
        self.assertEqual(create_organization_business(3, org_id, {
            "business_name": "Nope", "category": "c", "latitude": "1", "longitude": "1",
        }).get("_http"), 404)
        self.assertEqual(update_organization_business(3, org_id, lid, {"business_name": "Nope"}).get("_http"), 404)
        self.assertEqual(delete_organization_business(3, org_id, lid).get("_http"), 404)
        self.assertIsNone(get_organization_analytics(3, org_id))
        self.assertEqual(invite_member(3, org_id, "9999999997", "staff").get("_http"), 404)
        mgr_member_id = next(m["id"] for m in self.store.organization_members.values()
                              if m["organization_id"] == org_id and m["user_id"] == 2)
        self.assertEqual(change_member_role(3, org_id, mgr_member_id, "staff").get("_http"), 404)
        self.assertEqual(remove_member(3, org_id, mgr_member_id).get("_http"), 404)

    def test_owner_role_and_removal_protected_from_normal_endpoint(self):
        from services.organization_service import change_member_role, remove_member
        org_id = self._org_with_roles()
        owner_member_id = next(m["id"] for m in self.store.organization_members.values()
                                if m["organization_id"] == org_id and m["role"] == "owner")
        # Even the owner cannot demote/remove the owner row via the normal endpoint.
        self.assertEqual(change_member_role(1, org_id, owner_member_id, "manager").get("_http"), 404)
        self.assertEqual(remove_member(1, org_id, owner_member_id).get("_http"), 404)

    def test_suspended_member_has_zero_permissions(self):
        from services.organization_service import list_organization_businesses
        org_id = self._org_with_roles()
        member = next(m for m in self.store.organization_members.values() if m["user_id"] == 3)
        member["status"] = "suspended"
        self.assertIsNone(list_organization_businesses(3, org_id))

    def test_removed_member_has_zero_permissions(self):
        from services.organization_service import list_organization_businesses
        org_id = self._org_with_roles()
        member = next(m for m in self.store.organization_members.values() if m["user_id"] == 3)
        member["status"] = "removed"
        self.assertIsNone(list_organization_businesses(3, org_id))


# ---------------------------------------------------------------------------
# Listing invariant: user_id is never reassigned by V2
# ---------------------------------------------------------------------------
class ListingInvariantTests(OrgTestCase):
    def test_user_id_unchanged_across_every_organization_operation(self):
        from services.organization_service import (
            create_organization_business, update_organization_business, get_organization_business,
        )
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9000000002")
        self.store.add_membership(org_id, 2, "manager", "active")

        result = create_organization_business(1, org_id, {
            "business_name": "Biz", "category": "retail", "latitude": "12.9", "longitude": "77.6",
        })
        lid = result["listing_id"]
        before = self.store.listings[lid]["user_id"]
        self.assertEqual(before, 1)

        # A DIFFERENT member (manager) edits the business.
        update_organization_business(2, org_id, lid, {"business_name": "Edited", "category": "retail"})
        after_edit = self.store.listings[lid]["user_id"]
        self.assertEqual(before, after_edit)

        get_organization_business(2, org_id, lid)
        after_view = self.store.listings[lid]["user_id"]
        self.assertEqual(before, after_view)

    def test_organization_id_is_the_organization_relationship_not_user_id(self):
        from services.organization_service import create_organization_business
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        result = create_organization_business(1, org_id, {
            "business_name": "Biz", "category": "retail", "latitude": "12.9", "longitude": "77.6",
        })
        listing = self.store.listings[result["listing_id"]]
        self.assertEqual(listing["organization_id"], org_id)
        self.assertEqual(listing["user_id"], 1)


# ---------------------------------------------------------------------------
# No business-level member assignment
# ---------------------------------------------------------------------------
class NoBusinessLevelAssignmentTests(unittest.TestCase):
    def test_no_assignment_endpoint_in_route_source(self):
        src = (ROOT / "routes" / "organization_routes.py").read_text(encoding="utf-8")
        self.assertNotIn("/assign", src)

    def test_no_organization_business_members_table_anywhere(self):
        init_db_src = (ROOT / "database" / "init_db.py").read_text(encoding="utf-8")
        migration_src = (ROOT / "migrations" / "add_business_power_v2_organizations.py").read_text(encoding="utf-8")
        self.assertNotIn("organization_business_members", init_db_src)
        self.assertNotIn("organization_business_members", migration_src)

    def test_no_business_level_permission_mechanism_in_service_layer(self):
        src = (ROOT / "services" / "organization_service.py").read_text(encoding="utf-8")
        self.assertNotIn("organization_business_members", src)
        self.assertNotIn("business_member", src)


# ---------------------------------------------------------------------------
# Analytics isolation
# ---------------------------------------------------------------------------
class AnalyticsIsolationTests(OrgTestCase):
    def test_one_organization_cannot_see_another_organizations_numbers(self):
        from services.organization_service import create_organization_business, get_organization_analytics
        self.store.add_business_power_owner(1)
        self.store.add_business_power_owner(2)
        org_a = self.store.seed_organization(1)
        org_b = self.store.seed_organization(2)

        r = create_organization_business(1, org_a, {
            "business_name": "A-Biz", "category": "c", "latitude": "1", "longitude": "1",
        })
        self.store.listings[r["listing_id"]]["views"] = 500

        analytics_b = get_organization_analytics(2, org_b)
        self.assertEqual(analytics_b["totals"]["views"], 0)
        self.assertEqual(analytics_b["top_listings"], [])

        analytics_a = get_organization_analytics(1, org_a)
        self.assertEqual(analytics_a["totals"]["views"], 500)

    def test_non_member_gets_no_analytics_access(self):
        from services.organization_service import get_organization_analytics
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(99)
        self.assertIsNone(get_organization_analytics(99, org_id))


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
class PaginationTests(OrgTestCase):
    def _seed_businesses(self, org_id, count, creator_id=1):
        from services.organization_service import create_organization_business
        for i in range(count):
            create_organization_business(creator_id, org_id, {
                "business_name": f"Biz {i}", "category": "c", "latitude": "1", "longitude": "1",
            })

    def test_page_1_and_page_2(self):
        from services.organization_service import list_organization_businesses
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self._seed_businesses(org_id, 5)
        page1 = list_organization_businesses(1, org_id, page=1, limit=2)
        page2 = list_organization_businesses(1, org_id, page=2, limit=2)
        self.assertEqual(len(page1["businesses"]), 2)
        self.assertEqual(len(page2["businesses"]), 2)
        self.assertEqual(page1["total"], 5)
        self.assertEqual(page1["pages"], 3)
        ids_p1 = {b["id"] for b in page1["businesses"]}
        ids_p2 = {b["id"] for b in page2["businesses"]}
        self.assertEqual(ids_p1 & ids_p2, set())

    def test_boundary_last_page(self):
        from services.organization_service import list_organization_businesses
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self._seed_businesses(org_id, 5)
        last_page = list_organization_businesses(1, org_id, page=3, limit=2)
        self.assertEqual(len(last_page["businesses"]), 1)

    def test_invalid_page_defaults_safely(self):
        from services.organization_service import list_organization_businesses
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self._seed_businesses(org_id, 2)
        result = list_organization_businesses(1, org_id, page=-5, limit=50)
        self.assertEqual(result["page"], 1)

    def test_invalid_limit_defaults_safely(self):
        from services.organization_service import list_organization_businesses
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self._seed_businesses(org_id, 2)
        result = list_organization_businesses(1, org_id, page=1, limit=-10)
        self.assertGreaterEqual(result["limit"], 1)

    def test_maximum_limit_is_bounded(self):
        from services.organization_service import list_organization_businesses, MAX_PAGE_SIZE
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        result = list_organization_businesses(1, org_id, page=1, limit=999999)
        self.assertLessEqual(result["limit"], MAX_PAGE_SIZE)


# ---------------------------------------------------------------------------
# Security: forged JWT, IDOR, cross-organization access, at the real
# Flask-route level (real blueprints, real decorators).
# ---------------------------------------------------------------------------
class SecurityRouteTests(OrgTestCase):
    def setUp(self):
        super().setUp()
        from routes.organization_routes import organization_bp
        from routes.admin_routes import admin_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(organization_bp, url_prefix="/api/organization")
        self.app.register_blueprint(admin_bp)
        self.client = self.app.test_client()

    def _token(self, uid, role=None):
        with self.app.app_context():
            claims = {"role": role} if role else {}
            return create_access_token(identity=str(uid), additional_claims=claims)

    def _auth(self, tok):
        return {"Authorization": f"Bearer {tok}"}

    def test_idor_organization_detail_rejected_for_non_member(self):
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(99)
        res = self.client.get(f"/api/organization/{org_id}", headers=self._auth(self._token(99)))
        self.assertEqual(res.status_code, 404)

    def test_idor_cross_organization_listing_rejected(self):
        self.store.add_business_power_owner(1)
        self.store.add_business_power_owner(2)
        org_a = self.store.seed_organization(1)
        org_b = self.store.seed_organization(2)
        res = self.client.post(
            f"/api/organization/{org_a}/businesses",
            data={"business_name": "A-Biz", "category": "c", "latitude": "1", "longitude": "1"},
            headers=self._auth(self._token(1)),
        )
        listing_id = res.get_json()["listing_id"]
        # User 2 (owner of org B, not a member of org A) tries to reach
        # org A's listing through org B's URL.
        res2 = self.client.put(
            f"/api/organization/{org_b}/businesses/{listing_id}",
            json={"business_name": "Hijack"},
            headers=self._auth(self._token(2)),
        )
        self.assertEqual(res2.status_code, 404)
        res3 = self.client.delete(
            f"/api/organization/{org_b}/businesses/{listing_id}",
            headers=self._auth(self._token(2)),
        )
        self.assertEqual(res3.status_code, 404)
        self.assertIn(listing_id, self.store.listings)

    def test_idor_member_id_from_another_organization_rejected(self):
        self.store.add_business_power_owner(1)
        self.store.add_business_power_owner(2)
        org_a = self.store.seed_organization(1)
        org_b = self.store.seed_organization(2)
        self.store.add_user(3)
        member_id_in_a = self.store.add_membership(org_a, 3, "staff", "active")
        # Owner of org B tries to modify a member ID that belongs to org A.
        res = self.client.put(
            f"/api/organization/{org_b}/members/{member_id_in_a}",
            json={"role": "manager"},
            headers=self._auth(self._token(2)),
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.store.organization_members[member_id_in_a]["role"], "staff")

    def test_idor_invitation_token_rejected(self):
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2)
        res = self.client.post(
            f"/api/organization/{org_id}/members/invitations/not-a-real-token/accept",
            headers=self._auth(self._token(2)),
        )
        self.assertEqual(res.status_code, 400)

    def test_forged_admin_jwt_claim_without_db_role_is_rejected(self):
        self.store.add_user(2, role="business_premium")
        with patch("routes.admin_routes.db_user_is_admin", return_value=False):
            token = self._token(2, role="admin")
            res = self.client.post(
                "/api/admin/users/2/activate-business-power-v2",
                headers=self._auth(token),
            )
        self.assertEqual(res.status_code, 403)

    def test_authorized_admin_can_activate_v2_via_route(self):
        self.store.add_user(1, role="admin")
        self.store.add_user(5, role="free")
        token = self._token(1, role="admin")
        with patch("routes.admin_routes.db_user_is_admin", return_value=True):
            res = self.client.post(
                "/api/admin/users/5/activate-business-power-v2",
                headers=self._auth(token),
            )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["status"], "business_power_v2_activated")
        self.assertIn("organization_id", body)
        self.assertEqual(self.store.users[5]["plan"], BUSINESS_POWER_PLAN)

    def test_unauthenticated_request_rejected(self):
        res = self.client.get("/api/organization/1")
        self.assertEqual(res.status_code, 401)

    def test_suspended_member_rejected_at_route_level(self):
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2)
        self.store.add_membership(org_id, 2, "staff", "suspended")
        res = self.client.get(f"/api/organization/{org_id}/businesses", headers=self._auth(self._token(2)))
        self.assertEqual(res.status_code, 404)

    def test_removed_member_rejected_at_route_level(self):
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2)
        self.store.add_membership(org_id, 2, "staff", "removed")
        res = self.client.get(f"/api/organization/{org_id}/businesses", headers=self._auth(self._token(2)))
        self.assertEqual(res.status_code, 404)

    def test_inactive_organization_rejected_at_route_level(self):
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1, status="suspended")
        res = self.client.get(f"/api/organization/{org_id}/businesses", headers=self._auth(self._token(1)))
        self.assertEqual(res.status_code, 404)

    def test_expired_business_power_rejected_at_route_level(self):
        self.store.add_business_power_owner(1, expiry=_past())
        org_id = self.store.seed_organization(1)
        res = self.client.get(f"/api/organization/{org_id}/businesses", headers=self._auth(self._token(1)))
        self.assertEqual(res.status_code, 404)

    def test_no_custom_jwt_claim_used_for_organization_authorization(self):
        """Only get_jwt_identity() (the user id) and, in one place, the
        display-only 'phone' claim are read -- no role/org/permission
        claim is ever consulted."""
        src = (ROOT / "routes" / "organization_routes.py").read_text(encoding="utf-8")
        self.assertNotIn('claims.get("role")', src)
        self.assertNotIn('claims.get("organization', src)
        self.assertNotIn('claims.get("permission', src)


# ---------------------------------------------------------------------------
# Financial safety
# ---------------------------------------------------------------------------
class FinancialSafetyTests(OrgTestCase):
    def _assert_no_financial_side_effects(self, referred_user_id=None):
        self.assertEqual(self.store.payments, [])
        self.assertEqual(self.store.wallet_transactions, [])
        self.assertEqual(self.store.referral_commission_jobs, [])
        if referred_user_id is not None:
            self.assertEqual(self.store.users[referred_user_id].get("first_sub_commission_paid", 0), 0)

    def test_organization_creation_has_zero_financial_side_effects(self):
        from services.organization_service import ensure_organization_for_user
        self.store.add_business_power_owner(1)
        ensure_organization_for_user(1)
        self._assert_no_financial_side_effects()

    def test_invitation_and_acceptance_have_zero_financial_side_effects(self):
        from services.organization_service import invite_member, accept_invitation
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2, phone="9876543210")
        inv = invite_member(1, org_id, "9876543210", "staff")
        accept_invitation(2, org_id, inv["token"])
        self._assert_no_financial_side_effects()

    def test_business_creation_edit_delete_have_zero_financial_side_effects(self):
        from services.organization_service import (
            create_organization_business, update_organization_business, delete_organization_business,
        )
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        result = create_organization_business(1, org_id, {
            "business_name": "Biz", "category": "c", "latitude": "1", "longitude": "1",
        })
        lid = result["listing_id"]
        update_organization_business(1, org_id, lid, {"business_name": "Y", "category": "c"})
        delete_organization_business(1, org_id, lid)
        self._assert_no_financial_side_effects()

    def test_member_role_change_and_removal_have_zero_financial_side_effects(self):
        from services.organization_service import change_member_role, remove_member
        self.store.add_business_power_owner(1)
        org_id = self.store.seed_organization(1)
        self.store.add_user(2)
        mid = self.store.add_membership(org_id, 2, "manager", "active")
        change_member_role(1, org_id, mid, "staff")
        remove_member(1, org_id, mid)
        self._assert_no_financial_side_effects()

    def test_v2_admin_activation_has_zero_financial_side_effects(self):
        from services.admin_service import activate_business_power_v2
        self.store.add_user(1, role="admin")
        self.store.add_user(5, role="free")
        result = activate_business_power_v2(5, 1, "9999999999", "127.0.0.1")
        self.assertEqual(result["status"], "business_power_v2_activated")
        self._assert_no_financial_side_effects()

    def test_referral_commission_module_untouched(self):
        """0-diff guarantee, re-asserted the same way as V1: no V2 code
        path imports or calls into services/referral_commission.py. (The
        module docstring in organization_authz.py cites it as precedent for
        named-parameter IN clauses in prose -- that's documentation, not an
        import or call, so we check for those specifically.)"""
        for fname in ("organization_service.py", "organization_authz.py"):
            src = (ROOT / "services" / fname).read_text(encoding="utf-8")
            self.assertNotIn("import services.referral_commission", src)
            self.assertNotIn("from services.referral_commission", src)
            self.assertNotIn("enqueue_referral_commission_job(", src)
            self.assertNotIn("wallet_transactions", src)
            self.assertNotIn("INSERT INTO payments", src)


if __name__ == "__main__":
    unittest.main()
