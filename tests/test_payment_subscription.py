"""Tests for Razorpay verification, plan mapping, listing access, and debug routes."""
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import importlib.util

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "rzp_test_secret")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name, relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token, jwt_required

from config.payment_config import billed_term, get_plan_spec, PLAN_PRICES
from services.payment_service import verify_payment_service, success_payload
from services.subscription_access import is_subscription_active, legacy_add_business_gone

listing_mod = _load("listing_routes_iso", "routes/listing_routes.py")

payment_routes_mod = _load("payment_routes_iso", "routes/payment_routes.py")
payment_bp = payment_routes_mod.payment_bp


def _row(data):
    return SimpleNamespace(_mapping=data)


class PlanMappingTests(unittest.TestCase):
    def test_display_name_maps_to_internal_plan_and_role(self):
        display, spec = get_plan_spec("Business Basic")
        self.assertEqual(display, "Business Basic")
        self.assertEqual(spec["plan"], "business_basic")
        self.assertEqual(spec["role"], "business_basic")
        self.assertEqual(spec["amount_paise"], 99900)
        self.assertEqual(spec["duration_days"], 30)
        self.assertEqual(spec["business_limit"], 1)

    def test_internal_key_resolves(self):
        display, spec = get_plan_spec("business_premium")
        self.assertEqual(display, "Business Premium")
        self.assertEqual(spec["role"], "business_premium")

    def test_unknown_plan(self):
        display, spec = get_plan_spec("gold")
        self.assertIsNone(display)
        self.assertIsNone(spec)

    def test_billing_cycle_discounts(self):
        cycle, amount, days = billed_term(99900, "monthly")
        self.assertEqual((cycle, amount, days), ("monthly", 99900, 30))
        cycle, amount, days = billed_term(99900, "3months")
        self.assertEqual((cycle, amount, days), ("3months", 269730, 90))
        cycle, amount, days = billed_term(49900, "yearly")
        self.assertEqual((cycle, amount, days), ("yearly", 419160, 365))
        with self.assertRaises(ValueError):
            billed_term(99900, "weekly")


class LockedPaymentDB:
    """In-memory payments/users store with a row lock to simulate SELECT FOR UPDATE."""

    def __init__(self, payment, user):
        self.payment = dict(payment)
        self.user = dict(user)
        self.row_lock = threading.Lock()
        self.user_updates = 0
        self.payment_status_writes = []
        self.jobs = []

    def connect(self):
        return LockedConn(self)


class LockedConn:
    def __init__(self, store):
        self.store = store
        self._locked = False

    def execute(self, query, params=None):
        qs = str(query)
        params = params or {}
        res = MagicMock()
        if "FROM payments" in qs and "FOR UPDATE" in qs:
            self.store.row_lock.acquire()
            self._locked = True
            res.fetchone.return_value = _row(self.store.payment)
            return res
        if "FROM payments" in qs:
            res.fetchone.return_value = _row(self.store.payment)
            return res
        if "UPDATE users" in qs and "SET role" in qs:
            self.store.user_updates += 1
            self.store.user.update({
                "plan": params["plan"],
                "role": params["role"],
                "subscription_expiry": params["expiry_date"],
                "business_limit": params["blimit"],
            })
            res.rowcount = 1
            return res
        if "extra_businesses_purchased" in qs and "UPDATE users" in qs:
            self.store.user["extra_businesses_purchased"] = int(
                self.store.user.get("extra_businesses_purchased") or 0
            ) + 1
            res.rowcount = 1
            return res
        if "UPDATE payments" in qs:
            self.store.payment_status_writes.append(params.get("status"))
            if (self.store.payment.get("status") or "").lower() == "activated":
                res.rowcount = 0
                return res
            self.store.payment["payment_id"] = params["pid"]
            self.store.payment["order_id"] = params["oid"]
            self.store.payment["amount"] = params["amount"]
            self.store.payment["status"] = params["status"]
            self.store.payment["plan"] = params["plan"]
            res.rowcount = 1
            return res
        if "INSERT INTO referral_commission_jobs" in qs:
            pid = params.get("payment_id")
            if pid and not any(j.get("payment_id") == pid for j in self.store.jobs):
                self.store.jobs.append(dict(params))
            return res
        if "FROM users" in qs:
            res.fetchone.return_value = _row(self.store.user)
            return res
        res.fetchone.return_value = None
        return res

    def commit(self):
        self._release()

    def rollback(self):
        self._release()

    def close(self):
        self._release()

    def _release(self):
        if self._locked:
            self.store.row_lock.release()
            self._locked = False


class ListingAccessTests(unittest.TestCase):
    def test_unpaid_free_blocked(self):
        self.assertFalse(is_subscription_active({"plan": "free", "subscription_expiry": None}))

    def test_paid_active_allowed(self):
        expiry = (datetime.utcnow() + timedelta(days=20)).strftime("%Y-%m-%d")
        self.assertTrue(is_subscription_active({
            "plan": "business_basic",
            "subscription_expiry": expiry,
        }))

    def test_expired_blocked(self):
        expiry = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertFalse(is_subscription_active({
            "plan": "business_basic",
            "subscription_expiry": expiry,
        }))

    def test_service_provider_active_matches_listing_api(self):
        expiry = (datetime.utcnow() + timedelta(days=10)).strftime("%Y-%m-%d")
        self.assertTrue(is_subscription_active({
            "plan": "service_provider",
            "subscription_expiry": expiry,
        }))
        src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        self.assertIn(
            "requires_active_plan('service_provider', 'business_basic', 'business_premium')",
            src,
        )


class FrontendContractTests(unittest.TestCase):
    def test_success_payload_has_success_true(self):
        _, spec = get_plan_spec("Business Basic")
        body = success_payload("Subscription activated", spec, "2026-09-13")
        self.assertTrue(body["success"])
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["plan"], "business_basic")
        self.assertEqual(body["role"], "business_basic")
        self.assertEqual(body["redirect"], "/dashboard")


class VerifyPaymentServiceTests(unittest.TestCase):
    def setUp(self):
        self._ensure = patch("services.payment_service.ensure_referral_commission_schema")
        self._ensure.start()
        self.addCleanup(self._ensure.stop)

    def _client(self, signature_ok=True, order=None, payment=None):
        client = MagicMock()
        if signature_ok:
            client.utility.verify_payment_signature.return_value = True
        else:
            client.utility.verify_payment_signature.side_effect = Exception("bad sig")
        order = order or {}
        client.order.fetch.return_value = order
        pay = payment or {
            "id": "pay_1",
            "order_id": "order_1",
            "status": "captured",
            "amount": order.get("amount", 99900),
            "notes": order.get("notes") or {},
        }
        client.payment.fetch.return_value = pay
        return client

    def _conn(self, order_row=None):
        conn = MagicMock()

        def execute(query, params=None):
            qs = str(query)
            res = MagicMock()
            if "FROM payments" in qs:
                res.fetchone.return_value = order_row
            else:
                res.fetchone.return_value = None
            return res

        conn.execute.side_effect = execute
        return conn

    def _paid_order(self, amount=99900, plan="business_basic"):
        return {
            "status": "paid",
            "amount": amount,
            "notes": {"plan": plan, "user_id": "42"},
        }

    def test_invalid_signature_rejected(self):
        with patch("services.payment_service.get_razorpay_client", return_value=self._client(False)), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection") as gdb:
            result = verify_payment_service({
                "razorpay_order_id": "order_1",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": "nope",
            }, "42")
        self.assertFalse(result["success"])
        self.assertIn("signature", result["error"].lower())
        gdb.assert_not_called()

    def test_amount_mismatch_rejected(self):
        order = self._paid_order(amount=100)
        row = _row({
            "id": 1, "user_id": 42, "order_id": "order_1",
            "payment_id": "order_1", "amount": 99900, "status": "created",
            "plan": "business_basic",
        })
        with patch("services.payment_service.get_razorpay_client", return_value=self._client(True, order)), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", return_value=self._conn(row)):
            result = verify_payment_service({
                "razorpay_order_id": "order_1",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": "sig",
            }, "42")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Amount mismatch")

    def test_successful_verify_activates_once(self):
        store = LockedPaymentDB(
            {
                "id": 9, "user_id": 42, "order_id": "order_1",
                "payment_id": "order_1", "amount": 99900, "status": "created",
                "plan": "business_basic",
            },
            {"plan": "free", "role": "user", "subscription_expiry": None},
        )
        with patch("services.payment_service.get_razorpay_client", return_value=self._client(True, self._paid_order())), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", side_effect=store.connect):
            result = verify_payment_service({
                "razorpay_order_id": "order_1",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": "sig",
            }, "42")
        self.assertTrue(result["success"])
        self.assertEqual(result["plan"], "business_basic")
        self.assertEqual(result["role"], "business_basic")
        self.assertTrue(result["expiry"])
        self.assertEqual(store.user_updates, 1)
        self.assertEqual(store.payment["status"], "activated")
        self.assertEqual(store.user["plan"], "business_basic")

    def test_duplicate_verify_does_not_extend_expiry(self):
        store = LockedPaymentDB(
            {
                "id": 9, "user_id": 42, "order_id": "order_1",
                "payment_id": "order_1", "amount": 99900, "status": "created",
                "plan": "business_basic",
            },
            {"plan": "free", "role": "user", "subscription_expiry": None},
        )
        payload = {
            "razorpay_order_id": "order_1",
            "razorpay_payment_id": "pay_1",
            "razorpay_signature": "sig",
        }
        with patch("services.payment_service.get_razorpay_client", return_value=self._client(True, self._paid_order())), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", side_effect=store.connect):
            first = verify_payment_service(payload, "42")
            second = verify_payment_service(payload, "42")
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(store.user_updates, 1)
        self.assertEqual(first["expiry"], second["expiry"])
        self.assertEqual(store.payment["status"], "activated")

    def test_captured_but_not_activated_is_recoverable(self):
        store = LockedPaymentDB(
            {
                "id": 9, "user_id": 42, "order_id": "order_1",
                "payment_id": "pay_1", "amount": 99900, "status": "captured",
                "plan": "business_basic",
            },
            {"plan": "free", "role": "user", "subscription_expiry": None},
        )
        with patch("services.payment_service.get_razorpay_client", return_value=self._client(True, self._paid_order())), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", side_effect=store.connect):
            result = verify_payment_service({
                "razorpay_order_id": "order_1",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": "sig",
            }, "42")
        self.assertTrue(result["success"])
        self.assertFalse(result.get("duplicate"))
        self.assertEqual(store.user_updates, 1)
        self.assertEqual(store.payment["status"], "activated")
        self.assertEqual(store.user["plan"], "business_basic")

    def test_concurrent_verify_activates_once(self):
        store = LockedPaymentDB(
            {
                "id": 9, "user_id": 42, "order_id": "order_1",
                "payment_id": "order_1", "amount": 99900, "status": "created",
                "plan": "business_basic",
            },
            {"plan": "free", "role": "user", "subscription_expiry": None},
        )
        results = [None, None]

        def run(idx):
            with patch("services.payment_service.get_razorpay_client", return_value=self._client(True, self._paid_order())), \
                 patch("services.payment_service.ensure_payments_plan_column"), \
                 patch("services.payment_service.get_db_connection", side_effect=store.connect):
                results[idx] = verify_payment_service({
                    "razorpay_order_id": "order_1",
                    "razorpay_payment_id": "pay_1",
                    "razorpay_signature": "sig",
                }, "42")

        t1 = threading.Thread(target=run, args=(0,))
        t2 = threading.Thread(target=run, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertTrue(results[0]["success"])
        self.assertTrue(results[1]["success"])
        self.assertEqual(store.user_updates, 1)
        self.assertEqual(store.payment["status"], "activated")
        duplicates = sum(1 for r in results if r.get("duplicate"))
        self.assertEqual(duplicates, 1)

    def test_frontend_plan_is_ignored(self):
        store = LockedPaymentDB(
            {
                "id": 3, "user_id": 42, "order_id": "order_9",
                "payment_id": "order_9", "amount": 49900, "status": "created",
                "plan": "service_provider",
            },
            {"plan": "free", "role": "user", "subscription_expiry": None},
        )
        order = self._paid_order(amount=49900, plan="service_provider")
        pay = {
            "id": "pay_9",
            "order_id": "order_9",
            "status": "captured",
            "amount": 49900,
            "notes": order.get("notes") or {},
        }
        with patch("services.payment_service.get_razorpay_client", return_value=self._client(True, order, pay)), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", side_effect=store.connect):
            result = verify_payment_service({
                "razorpay_order_id": "order_9",
                "razorpay_payment_id": "pay_9",
                "razorpay_signature": "sig",
                "plan": "Business Premium",
            }, "42")
        self.assertTrue(result["success"])
        self.assertEqual(result["plan"], "service_provider")
        self.assertEqual(store.user["plan"], "service_provider")
        self.assertEqual(store.user["role"], "service_provider")


class OwnershipAwarePaymentDB:
    """In-memory payments store that honors user_id=:uid (unlike LockedPaymentDB)."""

    def __init__(self, payment, user):
        self.payment = dict(payment)
        self.user = dict(user)
        self.user_updates = []
        self.wallet_inserts = []
        self.payment_status_writes = []

    def connect(self):
        return OwnershipAwareConn(self)


class OwnershipAwareConn:
    def __init__(self, store):
        self.store = store

    def execute(self, query, params=None):
        qs = str(query)
        params = params or {}
        res = MagicMock()
        if "INSERT INTO wallet_transactions" in qs:
            self.store.wallet_inserts.append(dict(params))
            return res
        if "FROM payments" in qs:
            res.fetchone.return_value = self._owned_payment(qs, params)
            return res
        if "UPDATE users" in qs and "SET role" in qs:
            self.store.user_updates.append(dict(params))
            self.store.user.update({
                "plan": params.get("plan"),
                "role": params.get("role"),
                "subscription_expiry": params.get("expiry_date"),
                "business_limit": params.get("blimit"),
            })
            return res
        if "UPDATE payments" in qs:
            self.store.payment_status_writes.append(params.get("status"))
            self.store.payment["payment_id"] = params.get("pid", self.store.payment["payment_id"])
            self.store.payment["order_id"] = params.get("oid", self.store.payment["order_id"])
            if params.get("amount") is not None:
                self.store.payment["amount"] = params["amount"]
            if params.get("status") is not None:
                self.store.payment["status"] = params["status"]
            if params.get("plan") is not None:
                self.store.payment["plan"] = params["plan"]
            return res
        if "FROM users" in qs:
            uid = params.get("uid")
            if uid is not None and int(uid) == int(self.store.user.get("id", -1)):
                res.fetchone.return_value = _row(self.store.user)
            else:
                res.fetchone.return_value = None
            return res
        res.fetchone.return_value = None
        return res

    def _owned_payment(self, qs, params):
        if ":uid" in qs or "uid" in params:
            try:
                if int(params["uid"]) != int(self.store.payment["user_id"]):
                    return None
            except (KeyError, TypeError, ValueError):
                return None
        oid = params.get("oid")
        pid = params.get("pid")
        if oid is not None or pid is not None:
            stored_oid = self.store.payment.get("order_id")
            stored_pid = self.store.payment.get("payment_id")
            match = (
                oid in (stored_oid, stored_pid)
                or pid == stored_pid
            )
            if not match:
                return None
        return _row(self.store.payment)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class CrossUserVerifyPaymentTests(unittest.TestCase):
    """User B must not activate User A's order even with a valid Razorpay signature."""

    OWNER_ID = 42
    ATTACKER_ID = 99

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(payment_bp, url_prefix="/api/payment")
        self.client = self.app.test_client()

    def _token(self, identity):
        with self.app.app_context():
            return create_access_token(identity=str(identity))

    def _store(self):
        return OwnershipAwarePaymentDB(
            {
                "id": 1,
                "user_id": self.OWNER_ID,
                "order_id": "order_a",
                "payment_id": "order_a",
                "amount": 99900,
                "status": "created",
                "plan": "business_basic",
            },
            {
                "id": self.OWNER_ID,
                "plan": "free",
                "role": "user",
                "subscription_expiry": None,
                "referred_by": 7,
                "first_sub_commission_paid": 0,
            },
        )

    def _rzp(self, notes_user_id):
        client = MagicMock()
        client.utility.verify_payment_signature.return_value = True
        client.order.fetch.return_value = {
            "status": "paid",
            "amount": 99900,
            "notes": {"plan": "business_basic", "user_id": str(notes_user_id)},
        }
        client.payment.fetch.return_value = {
            "status": "captured",
            "order_id": "order_a",
            "amount": 99900,
            "notes": {"plan": "business_basic", "user_id": str(notes_user_id)},
        }
        client.order.create.side_effect = AssertionError("must not create a Razorpay order")
        return client

    def _post(self, identity, notes_user_id, store):
        rzp = self._rzp(notes_user_id)
        with patch("services.payment_service.get_razorpay_client", return_value=rzp), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch.object(payment_routes_mod, "get_db_connection", side_effect=store.connect), \
             patch("services.referral_commission.get_db_connection", side_effect=store.connect), \
             patch.object(payment_routes_mod, "after_payment_finalized") as credit:
            res = self.client.post(
                "/api/payment/verify-payment",
                json={
                    "razorpay_order_id": "order_a",
                    "razorpay_payment_id": "pay_a",
                    "razorpay_signature": "valid-but-mocked",
                },
                headers={"Authorization": f"Bearer {self._token(identity)}"},
            )
        return res, credit, rzp

    def _assert_no_side_effects(self, store, credit):
        self.assertEqual(store.payment["status"], "created")
        self.assertEqual(store.user["plan"], "free")
        self.assertIsNone(store.user["subscription_expiry"])
        self.assertEqual(store.user_updates, [])
        self.assertEqual(store.wallet_inserts, [])
        credit.assert_not_called()

    def test_user_b_rejected_when_notes_identify_owner_a(self):
        store = self._store()
        res, credit, rzp = self._post(self.ATTACKER_ID, self.OWNER_ID, store)
        body = res.get_json()
        self.assertFalse(body["success"])
        self.assertIn("belong", body["error"].lower())
        self._assert_no_side_effects(store, credit)
        rzp.utility.verify_payment_signature.assert_called()
        rzp.order.create.assert_not_called()

    def test_user_b_rejected_when_notes_claim_b_but_row_owned_by_a(self):
        store = self._store()
        res, credit, rzp = self._post(self.ATTACKER_ID, self.ATTACKER_ID, store)
        body = res.get_json()
        self.assertEqual(res.status_code, 400)
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "Order not found for this account")
        self._assert_no_side_effects(store, credit)
        rzp.utility.verify_payment_signature.assert_called()
        rzp.order.create.assert_not_called()

    def test_user_a_can_verify_own_order_with_same_mocks(self):
        store = self._store()
        res, credit, rzp = self._post(self.OWNER_ID, self.OWNER_ID, store)
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(body["success"])
        self.assertEqual(store.payment["status"], "activated")
        self.assertEqual(store.user["plan"], "business_basic")
        self.assertEqual(len(store.user_updates), 1)
        self.assertEqual(store.wallet_inserts, [])
        credit.assert_called_once()
        rzp.utility.verify_payment_signature.assert_called()
        rzp.order.create.assert_not_called()


class PaymentRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(payment_bp, url_prefix="/api/payment")
        self.client = self.app.test_client()

    def test_unauthenticated_create_order_blocked(self):
        res = self.client.post("/api/payment/create-order", json={"plan": "Business Basic"})
        self.assertEqual(res.status_code, 401)

    def test_unauthenticated_verify_blocked(self):
        res = self.client.post("/api/payment/verify-payment", json={})
        self.assertEqual(res.status_code, 401)

    def test_debug_order_disabled(self):
        res = self.client.post("/api/payment/create-order-debug", json={"plan": "Business Basic"})
        self.assertEqual(res.status_code, 404)
        self.assertFalse(res.get_json().get("success"))

    def test_unsigned_webhook_rejected(self):
        res = self.client.post(
            "/api/payment/razorpay/webhook",
            data=b"{}",
            content_type="application/json",
        )
        self.assertIn(res.status_code, (400, 403, 503))

    def test_authenticated_create_order(self):
        rzp = MagicMock()
        rzp.order.create.return_value = {"id": "order_abc"}
        conn = MagicMock()
        with self.app.app_context():
            token = create_access_token(identity="7")
        with patch.object(payment_routes_mod, "get_razorpay_client", return_value=rzp), \
             patch.object(payment_routes_mod, "ensure_payments_plan_column"), \
             patch.object(payment_routes_mod, "get_db_connection", return_value=conn):
            res = self.client.post(
                "/api/payment/create-order",
                json={"plan": "Business Basic"},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["order_id"], "order_abc")
        self.assertEqual(body["amount"], PLAN_PRICES["Business Basic"])
        notes = rzp.order.create.call_args[0][0]["notes"]
        self.assertEqual(notes["plan"], "business_basic")
        self.assertEqual(notes["user_id"], "7")
        self.assertEqual(notes["billing_cycle"], "monthly")

    def test_authenticated_create_order_yearly(self):
        rzp = MagicMock()
        rzp.order.create.return_value = {"id": "order_year"}
        conn = MagicMock()
        with self.app.app_context():
            token = create_access_token(identity="7")
        with patch.object(payment_routes_mod, "get_razorpay_client", return_value=rzp), \
             patch.object(payment_routes_mod, "ensure_payments_plan_column"), \
             patch.object(payment_routes_mod, "get_db_connection", return_value=conn):
            res = self.client.post(
                "/api/payment/create-order",
                json={"plan": "Business Basic", "billing_cycle": "yearly"},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["amount"], 839160)
        self.assertEqual(body["billing_cycle"], "yearly")
        payload = rzp.order.create.call_args[0][0]
        self.assertEqual(payload["amount"], 839160)
        self.assertEqual(payload["notes"]["billing_cycle"], "yearly")
        self.assertEqual(payload["notes"]["duration_days"], "365")


class ReferralAndWalletWiringTests(unittest.TestCase):
    def test_first_bonus_source_matches_saturday_payout(self):
        commission = (ROOT / "services" / "referral_commission.py").read_text(encoding="utf-8")
        payout = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("'referral_first_bonus'", commission)
        self.assertNotIn("'5%_base_+_5%_activation'", commission)
        self.assertIn("release_locked_referral_payouts", payout)
        self.assertIn("release_locked_referral_payouts", commission)
        self.assertIn("'referral_first_bonus'", commission)
        self.assertIn("'referral_recurring'", commission)
        self.assertIn("FOR UPDATE SKIP LOCKED", commission)

    def test_wallet_transactions_do_not_select_description(self):
        wallet_routes = (ROOT / "routes" / "wallet_routes.py").read_text(encoding="utf-8")
        wallet_service = (ROOT / "services" / "wallet_service.py").read_text(encoding="utf-8")
        self.assertIn("SELECT id, amount, type, source, status, created_at", wallet_routes)
        self.assertNotIn("description", wallet_service.split("def get_wallet_transactions")[1].split("def add_pending")[0])

    def test_login_passes_ref_to_send_otp(self):
        html = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        self.assertIn("/api/auth/send-otp' + referralQuery()", html)

    def test_user_browse_queries_listings(self):
        src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        browse = src.split("def api_browse")[1].split("def ")[0]
        self.assertIn("FROM listings", browse)
        self.assertNotIn("FROM businesses", browse)


class LegacyAddBusinessTests(unittest.TestCase):
    def test_free_user_cannot_bypass_via_add_business(self):
        body, code = legacy_add_business_gone()
        self.assertEqual(code, 410)
        self.assertEqual(body["canonical"], "/api/listing/create-listing")
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("legacy_add_business_gone", src)
        self.assertNotIn('if plan == "free": return True', src)

        app = Flask(__name__)
        app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(app)

        @app.route("/api/add-business", methods=["POST"])
        @jwt_required()
        def api_add_business():
            payload, status = legacy_add_business_gone()
            return payload, status

        client = app.test_client()
        with app.app_context():
            token = create_access_token(identity="1")
        res = client.post("/api/add-business", json={"name": "Shop"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 410)
        self.assertFalse(res.get_json().get("success"))


class CreateListingFlowTests(unittest.TestCase):
    def test_form_posts_to_canonical_api(self):
        html = (ROOT / "templates" / "users" / "create_listing.html").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/listing/create-listing"', html)
        self.assertNotIn("/api/listing/api/create-listing", html)

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        self.app.config["UPLOAD_FOLDER"] = tempfile.mkdtemp()
        JWTManager(self.app)
        self.app.register_blueprint(listing_mod.listing_bp, url_prefix="/api/listing")
        self.client = self.app.test_client()

    def _token(self):
        with self.app.app_context():
            return create_access_token(identity="42")

    def test_paid_user_listing_created(self):
        expiry = (datetime.utcnow() + timedelta(days=20)).strftime("%Y-%m-%d")
        user_row = _row({
            "id": 42, "role": "business_basic", "plan": "business_basic",
            "subscription_expiry": expiry, "is_active": 1,
        })
        conn = MagicMock()

        def execute(query, params=None):
            qs = str(query)
            res = MagicMock()
            if "FROM users" in qs:
                res.fetchone.return_value = user_row
            elif "COUNT(*)" in qs:
                res.fetchone.return_value = SimpleNamespace(_mapping={"cnt": 0})
            elif "INSERT INTO listings" in qs:
                res.fetchone.return_value = (77,)
            return res

        conn.execute.side_effect = execute
        with patch.object(listing_mod, "get_db_connection", return_value=conn):
            res = self.client.post(
                "/api/listing/create-listing",
                data={
                    "business_name": "Cafe",
                    "category": "food",
                    "latitude": "12.9",
                    "longitude": "77.6",
                },
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        self.assertEqual(res.status_code, 201)
        body = res.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["listing_id"], 77)
        conn.commit.assert_called()

    def test_expired_subscription_blocked(self):
        expiry = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        user_row = _row({
            "id": 42, "role": "business_basic", "plan": "business_basic",
            "subscription_expiry": expiry, "is_active": 1,
        })
        conn = MagicMock()
        resm = MagicMock()
        resm.fetchone.return_value = user_row
        conn.execute.return_value = resm
        with patch.object(listing_mod, "get_db_connection", return_value=conn):
            res = self.client.post(
                "/api/listing/create-listing",
                data={
                    "business_name": "Cafe",
                    "category": "food",
                    "latitude": "12.9",
                    "longitude": "77.6",
                },
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        self.assertEqual(res.status_code, 403)
        self.assertIn("subscription", res.get_json()["error"].lower())

    def test_service_provider_listing_api_allowed(self):
        expiry = (datetime.utcnow() + timedelta(days=10)).strftime("%Y-%m-%d")
        user_row = _row({
            "id": 42, "role": "service_provider", "plan": "service_provider",
            "subscription_expiry": expiry, "is_active": 1,
        })
        conn = MagicMock()

        def execute(query, params=None):
            qs = str(query)
            res = MagicMock()
            if "FROM users" in qs:
                res.fetchone.return_value = user_row
            elif "COUNT(*)" in qs:
                res.fetchone.return_value = SimpleNamespace(_mapping={"cnt": 0})
            elif "INSERT INTO listings" in qs:
                res.fetchone.return_value = (12,)
            return res

        conn.execute.side_effect = execute
        with patch.object(listing_mod, "get_db_connection", return_value=conn):
            res = self.client.post(
                "/api/listing/create-listing",
                data={
                    "business_name": "Plumber",
                    "category": "services",
                    "latitude": "12.9",
                    "longitude": "77.6",
                    "listing_type": "service",
                },
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.get_json()["success"])


if __name__ == "__main__":
    unittest.main()

