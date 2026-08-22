"""Stage 2C payment/subscription lifecycle hardening tests."""
import hashlib
import hmac
import json
import os
import sys
import threading
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
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsec_test"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from config.payment_config import (
    EXTRA_BUSINESS_AMOUNT_PAISE,
    billed_term,
    duration_days_for_stored_amount,
    get_plan_spec,
)
from services.payment_service import (
    finalize_extra_business_order,
    finalize_paid_order,
    mark_payment_failed,
    verify_extra_business_payment,
    verify_payment_service,
)
from tests.test_payment_subscription import LockedPaymentDB, _row, payment_routes_mod, payment_bp


def _rzp_client(order_id, payment_id, amount, user_id="42", status="captured",
                order_status="paid", notes=None, signature_ok=True):
    notes = notes or {"plan": "business_basic", "user_id": str(user_id)}
    client = MagicMock()
    if signature_ok:
        client.utility.verify_payment_signature.return_value = True
    else:
        client.utility.verify_payment_signature.side_effect = Exception("bad sig")
    client.order.fetch.return_value = {
        "status": order_status,
        "amount": amount,
        "notes": notes,
    }
    client.payment.fetch.return_value = {
        "id": payment_id,
        "order_id": order_id,
        "status": status,
        "amount": amount,
        "notes": notes,
    }
    return client


def _store(status="created", amount=99900, plan="business_basic", pid="order_1"):
    return LockedPaymentDB(
        {
            "id": 9, "user_id": 42, "order_id": "order_1",
            "payment_id": pid, "amount": amount, "status": status, "plan": plan,
        },
        {
            "plan": "free", "role": "user", "subscription_expiry": None,
            "extra_businesses_purchased": 0,
        },
    )


class DurationFromAmountTests(unittest.TestCase):
    def test_known_terms(self):
        self.assertEqual(duration_days_for_stored_amount(99900, 99900), ("monthly", 30))
        cycle, days = duration_days_for_stored_amount(99900, 839160)
        self.assertEqual((cycle, days), ("yearly", 365))
        self.assertEqual(duration_days_for_stored_amount(99900, 1), (None, None))


class CreateOrderHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(payment_bp, url_prefix="/api/payment")
        self.client = self.app.test_client()

    def _token(self, uid="7"):
        with self.app.app_context():
            return create_access_token(identity=uid)

    def test_amount_tampering_ignored(self):
        rzp = MagicMock()
        rzp.order.create.return_value = {"id": "order_abc"}
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch.object(payment_routes_mod, "get_razorpay_client", return_value=rzp), \
             patch.object(payment_routes_mod, "ensure_payments_plan_column"), \
             patch.object(payment_routes_mod, "get_db_connection", return_value=conn):
            res = self.client.post(
                "/api/payment/create-order",
                json={
                    "plan": "Business Basic",
                    "amount": 100,
                    "price": 1,
                    "user_id": "999",
                    "duration": 3650,
                    "commission": 50,
                },
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["amount"], 99900)
        self.assertEqual(body["user_id"], "7")
        self.assertEqual(rzp.order.create.call_args[0][0]["amount"], 99900)
        self.assertEqual(rzp.order.create.call_args[0][0]["notes"]["user_id"], "7")

    def test_invalid_plan(self):
        conn = MagicMock()
        with patch.object(payment_routes_mod, "get_db_connection", return_value=conn):
            res = self.client.post(
                "/api/payment/create-order",
                json={"plan": "gold"},
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid plan", res.get_json()["error"])

    def test_invalid_billing_term(self):
        res = self.client.post(
            "/api/payment/create-order",
            json={"plan": "Business Basic", "billing_cycle": "weekly"},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        self.assertEqual(res.status_code, 400)

    def test_duplicate_create_order_reuses_created(self):
        rzp = MagicMock()
        existing = _row({
            "order_id": "order_existing", "amount": 99900,
            "plan": "business_basic", "status": "created",
        })
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = existing
        with patch.object(payment_routes_mod, "get_razorpay_client", return_value=rzp), \
             patch.object(payment_routes_mod, "ensure_payments_plan_column"), \
             patch.object(payment_routes_mod, "get_db_connection", return_value=conn):
            res = self.client.post(
                "/api/payment/create-order",
                json={"plan": "Business Basic"},
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["order_id"], "order_existing")
        self.assertTrue(res.get_json().get("reused"))
        rzp.order.create.assert_not_called()

    def test_create_order_debug_still_404(self):
        res = self.client.post("/api/payment/create-order-debug", json={"plan": "Business Basic"})
        self.assertEqual(res.status_code, 404)


class VerifyHardeningTests(unittest.TestCase):
    def setUp(self):
        self._ensure = patch("services.payment_service.ensure_referral_commission_schema")
        self._ensure.start()
        self.addCleanup(self._ensure.stop)

    def test_wrong_user_jwt(self):
        store = _store()
        client = _rzp_client("order_1", "pay_1", 99900, user_id="42")
        with patch("services.payment_service.get_razorpay_client", return_value=client), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", side_effect=store.connect):
            result = verify_payment_service({
                "razorpay_order_id": "order_1",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": "sig",
            }, "99")
        self.assertFalse(result["success"])
        self.assertIn("belong", result["error"].lower())
        self.assertEqual(store.user_updates, 0)

    def test_wrong_order_on_payment(self):
        store = _store()
        client = _rzp_client("order_OTHER", "pay_1", 99900)
        client.order.fetch.return_value = {
            "status": "paid", "amount": 99900,
            "notes": {"plan": "business_basic", "user_id": "42"},
        }
        client.payment.fetch.return_value = {
            "id": "pay_1", "order_id": "order_OTHER", "status": "captured", "amount": 99900,
        }
        with patch("services.payment_service.get_razorpay_client", return_value=client), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", side_effect=store.connect):
            result = verify_payment_service({
                "razorpay_order_id": "order_1",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": "sig",
            }, "42")
        self.assertFalse(result["success"])
        self.assertIn("order", result["error"].lower())
        self.assertEqual(store.user_updates, 0)

    def test_underpayment_and_overpayment(self):
        for amt in (100, 199900):
            store = _store()
            client = _rzp_client("order_1", "pay_1", amt)
            with patch("services.payment_service.get_razorpay_client", return_value=client), \
                 patch("services.payment_service.ensure_payments_plan_column"), \
                 patch("services.payment_service.get_db_connection", side_effect=store.connect):
                result = verify_payment_service({
                    "razorpay_order_id": "order_1",
                    "razorpay_payment_id": "pay_1",
                    "razorpay_signature": "sig",
                }, "42")
            self.assertFalse(result["success"], amt)
            self.assertEqual(result["error"], "Amount mismatch")
            self.assertEqual(store.user_updates, 0)

    def test_authorized_payment_cannot_activate(self):
        store = _store()
        client = _rzp_client("order_1", "pay_1", 99900, status="authorized")
        with patch("services.payment_service.get_razorpay_client", return_value=client), \
             patch("services.payment_service.ensure_payments_plan_column"), \
             patch("services.payment_service.get_db_connection", side_effect=store.connect):
            result = verify_payment_service({
                "razorpay_order_id": "order_1",
                "razorpay_payment_id": "pay_1",
                "razorpay_signature": "sig",
            }, "42")
        self.assertFalse(result["success"])
        self.assertIn("captured", result["error"].lower())
        self.assertEqual(store.user_updates, 0)

    def test_failed_row_cannot_activate_without_recovery(self):
        _, spec = get_plan_spec("business_basic")
        store = _store(status="failed")
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            result = finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42)
        self.assertFalse(result["success"])
        self.assertEqual(store.user_updates, 0)
        self.assertEqual(store.payment["status"], "failed")

    def test_failed_then_captured_recovers(self):
        _, spec = get_plan_spec("business_basic")
        store = _store(status="failed", pid="pay_1")
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            result = finalize_paid_order(
                "order_1", "pay_1", spec, 99900, user_id=42, allow_failed_recovery=True,
            )
        self.assertTrue(result["success"])
        self.assertEqual(store.payment["status"], "activated")
        self.assertEqual(store.payment["payment_id"], "pay_1")

    def test_activated_cannot_regress_to_failed(self):
        store = _store(status="activated", pid="pay_1")
        mark_calls = []

        def fake_connect():
            conn = store.connect()
            orig = conn.execute

            def execute(query, params=None):
                qs = str(query)
                if "UPDATE payments" in qs and params and params.get("st") == "failed":
                    mark_calls.append(params)
                    if (store.payment.get("status") or "").lower() == "activated":
                        res = MagicMock()
                        res.rowcount = 0
                        return res
                return orig(query, params)
            conn.execute = execute
            return conn

        with patch("services.payment_service.get_db_connection", side_effect=fake_connect):
            mark_payment_failed("order_1", "pay_1", status="failed")
        self.assertEqual(store.payment["status"], "activated")

    def test_order_mismatch_in_finalize(self):
        _, spec = get_plan_spec("business_basic")
        store = LockedPaymentDB(
            {
                "id": 9, "user_id": 42, "order_id": "order_A",
                "payment_id": "pay_A", "amount": 99900, "status": "created",
                "plan": "business_basic",
            },
            {"plan": "free", "role": "user", "subscription_expiry": None},
        )
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            result = finalize_paid_order("order_B", "pay_A", spec, 99900, user_id=42)
        self.assertFalse(result["success"])
        self.assertEqual(store.user_updates, 0)


class WebhookHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(payment_bp, url_prefix="/api/payment")
        self.client = self.app.test_client()
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsec_test"

    def _sign(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        sig = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
        return body, sig

    def _event(self, status="captured", amount=99900, notes=None):
        return {
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_1",
                        "order_id": "order_1",
                        "status": status,
                        "amount": amount,
                        "notes": notes or {"user_id": "42", "plan": "business_basic", "duration_days": "9999"},
                    }
                }
            }
        }

    def test_invalid_signature_403(self):
        body, _sig = self._sign(self._event())
        res = self.client.post(
            "/api/payment/razorpay/webhook",
            data=body,
            headers={"X-Razorpay-Signature": "nope", "Content-Type": "application/json"},
        )
        self.assertEqual(res.status_code, 403)

    def test_authorized_ignored(self):
        body, sig = self._sign(self._event(status="authorized"))
        with patch.object(payment_routes_mod, "finalize_paid_order") as fin:
            res = self.client.post(
                "/api/payment/razorpay/webhook",
                data=body,
                headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ignored")
        fin.assert_not_called()

    def test_failed_event_does_not_activate(self):
        body, sig = self._sign(self._event(status="failed"))
        with patch.object(payment_routes_mod, "mark_payment_failed") as mk, \
             patch.object(payment_routes_mod, "finalize_paid_order") as fin:
            res = self.client.post(
                "/api/payment/razorpay/webhook",
                data=body,
                headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
            )
        self.assertEqual(res.status_code, 200)
        mk.assert_called()
        fin.assert_not_called()

    def test_notes_cannot_change_duration(self):
        store = _store()
        payload = self._event(amount=99900)
        body, sig = self._sign(payload)
        row = _row(store.payment)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = row
        with patch.object(payment_routes_mod, "ensure_payments_plan_column"), \
             patch.object(payment_routes_mod, "get_db_connection", return_value=conn), \
             patch.object(payment_routes_mod, "finalize_paid_order", return_value={
                 "success": True, "duplicate": False, "razorpay_payment_id": "pay_1",
             }) as fin, \
             patch.object(payment_routes_mod, "after_payment_finalized"):
            res = self.client.post(
                "/api/payment/razorpay/webhook",
                data=body,
                headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(fin.call_args.kwargs.get("duration_days"), 30)

    def test_duplicate_webhook_idempotent(self):
        _, spec = get_plan_spec("business_basic")
        store = _store(status="activated", pid="pay_1")
        store.user["plan"] = "business_basic"
        store.user["subscription_expiry"] = "2026-09-01"
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            first = finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42)
            second = finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42)
        self.assertTrue(first.get("duplicate"))
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(store.user_updates, 0)
        self.assertEqual(first["expiry"], second["expiry"])


class ExtraBusinessTests(unittest.TestCase):
    def test_does_not_activate_subscription_or_commission(self):
        store = _store(amount=EXTRA_BUSINESS_AMOUNT_PAISE, plan="extra_business")
        with patch("services.payment_service.get_db_connection", side_effect=store.connect):
            result = finalize_extra_business_order(
                "order_1", "pay_eb", EXTRA_BUSINESS_AMOUNT_PAISE, user_id=42,
            )
        self.assertTrue(result["success"])
        self.assertEqual(store.user.get("extra_businesses_purchased"), 1)
        self.assertEqual(store.user["plan"], "free")
        self.assertEqual(store.jobs, [])
        self.assertEqual(store.user_updates, 0)
        self.assertEqual(store.payment["status"], "activated")
        self.assertEqual(store.payment["payment_id"], "pay_eb")

    def test_duplicate_does_not_double_slot(self):
        store = _store(amount=EXTRA_BUSINESS_AMOUNT_PAISE, plan="extra_business", pid="pay_eb")
        store.payment["status"] = "activated"
        store.user["extra_businesses_purchased"] = 1
        with patch("services.payment_service.get_db_connection", side_effect=store.connect):
            result = finalize_extra_business_order(
                "order_1", "pay_eb", EXTRA_BUSINESS_AMOUNT_PAISE, user_id=42,
            )
        self.assertTrue(result.get("duplicate"))
        self.assertEqual(store.user["extra_businesses_purchased"], 1)

    def test_subscription_finalize_rejects_extra_business_row(self):
        _, spec = get_plan_spec("business_basic")
        store = _store(amount=EXTRA_BUSINESS_AMOUNT_PAISE, plan="extra_business")
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            result = finalize_paid_order(
                "order_1", "pay_eb", spec, EXTRA_BUSINESS_AMOUNT_PAISE, user_id=42,
            )
        self.assertFalse(result["success"])
        self.assertEqual(store.user_updates, 0)
        self.assertEqual(store.jobs, [])

    def test_user_route_source_has_no_commission(self):
        user_src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        extra = user_src.split('if plan_type == "extra_business"')[1].split(
            "from services.referral_commission"
        )[0]
        self.assertIn("verify_extra_business_payment", extra)
        self.assertNotIn("after_payment_finalized", extra)


class ReplayAndRenewalTests(unittest.TestCase):
    def test_renewal_is_from_now_not_stacked_on_same_order(self):
        _, spec = get_plan_spec("business_basic")
        store = _store()
        store.user["subscription_expiry"] = (datetime.utcnow() + timedelta(days=20)).strftime("%Y-%m-%d")
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            first = finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42, duration_days=30)
            second = finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42, duration_days=30)
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(store.user_updates, 1)
        self.assertEqual(first["expiry"], second["expiry"])

    def test_concurrent_finalize_once(self):
        _, spec = get_plan_spec("business_basic")
        store = _store()
        results = [None, None]

        def run(idx):
            with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
                 patch("services.payment_service.ensure_referral_commission_schema"):
                results[idx] = finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42)

        t1 = threading.Thread(target=run, args=(0,))
        t2 = threading.Thread(target=run, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(store.user_updates, 1)
        self.assertEqual(store.payment["status"], "activated")
        self.assertEqual(sum(1 for r in results if r.get("duplicate")), 1)

    def test_payment_identity_pay_not_order_placeholder(self):
        _, spec = get_plan_spec("business_basic")
        store = _store()
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            result = finalize_paid_order("order_1", "pay_LIVE", spec, 99900, user_id=42)
        self.assertTrue(result["success"])
        self.assertEqual(store.payment["order_id"], "order_1")
        self.assertEqual(store.payment["payment_id"], "pay_LIVE")
        self.assertEqual(result["razorpay_payment_id"], "pay_LIVE")
        self.assertEqual(store.jobs[0]["payment_id"], "pay_LIVE")

    def test_commission_job_exactly_once_on_duplicate_enqueue(self):
        _, spec = get_plan_spec("business_basic")
        store = _store()
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42)
            finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42)
        self.assertEqual(len(store.jobs), 1)

    def test_admin_activate_source_no_commission(self):
        from inspect import getsource
        from services.payment_service import activate_subscription
        self.assertNotIn("enqueue_referral_commission_job", getsource(activate_subscription))

    def test_orchestration_subscription_disabled(self):
        src = (ROOT / "routes" / "orchestration_routes.py").read_text(encoding="utf-8")
        self.assertIn("This endpoint is disabled", src)
        self.assertIn("410", src)

    def test_payment_agent_cannot_credit_wallet(self):
        src = (ROOT / "agents" / "payment_agent.py").read_text(encoding="utf-8")
        self.assertNotIn("wallet_balance.balance +", src)
        self.assertIn("Use POST /api/payment/verify-payment", src)

    def test_unsigned_app_webhook_still_403(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("/api/payment/webhook", src)
        self.assertIn("403", src.split("def razorpay_webhook_dummy")[1].split("def ")[0])


class ExpiredSubscriptionTests(unittest.TestCase):
    def test_expired_is_inactive(self):
        from services.subscription_access import is_subscription_active
        expiry = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertFalse(is_subscription_active({
            "plan": "business_basic",
            "subscription_expiry": expiry,
        }))


if __name__ == "__main__":
    unittest.main()
