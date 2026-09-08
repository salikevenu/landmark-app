"""POS billing payment verification + webhook (Phase G-B):
POST /api/pos/billing/verify and POST /api/pos/billing/webhook.

Proves: signature-verified payment recording, idempotency under every
arrival order (duplicate verify, duplicate webhook, verify-then-webhook,
webhook-then-verify), and that neither path ever touches
pos_subscriptions/users -- activation is explicitly out of scope for this
phase (G-C).

No real database connection -- routes.pos_routes.get_db_connection is
patched with an in-memory fake, matching every other pos_* test file's
pattern. No real Razorpay call -- routes.pos_routes.get_razorpay_client is
patched with a MagicMock, matching tests/test_payment_lifecycle.py's
convention. The fake deliberately has no handler for any
users/pos_subscriptions query -- if either code path ever reads or writes
them, the fake raises AssertionError instead of silently succeeding.
"""
import hashlib
import hmac
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

WEBHOOK_SECRET = "whsec_test_pos_secret"


class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class FakeResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class BillingOrderStore:
    def __init__(self):
        self.rows = {}

    def add(self, id, owner_user_id, pos_plan, amount_paise, currency,
            razorpay_order_id, status="created", razorpay_payment_id=None, paid_at=None):
        self.rows[id] = {
            "id": id,
            "owner_user_id": owner_user_id,
            "pos_plan": pos_plan,
            "amount_paise": amount_paise,
            "currency": currency,
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "status": status,
            "paid_at": paid_at,
        }

    def find_by_id_owner(self, id, owner_user_id):
        row = self.rows.get(id)
        if row and row["owner_user_id"] == owner_user_id:
            return dict(row)
        return None

    def find_by_razorpay_order_id(self, razorpay_order_id):
        for row in self.rows.values():
            if row["razorpay_order_id"] == razorpay_order_id:
                return dict(row)
        return None

    def mark_paid(self, id, payment_id):
        # Simulates the partial UNIQUE index on razorpay_payment_id.
        for other_id, other in self.rows.items():
            if other_id != id and other.get("razorpay_payment_id") == payment_id:
                raise IntegrityError("update", {}, Exception("duplicate payment id"))
        row = self.rows[id]
        if row["status"] == "paid":
            return None
        row["status"] = "paid"
        row["razorpay_payment_id"] = payment_id
        row["paid_at"] = "2026-09-08T00:00:00"
        return dict(row)

    def mark_failed(self, id):
        row = self.rows.get(id)
        if row and row["status"] != "paid":
            row["status"] = "failed"


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if "from pos_billing_orders where razorpay_order_id" in q:
            row = self.store.find_by_razorpay_order_id(params["rzp_order_id"])
            return FakeResult(row=FakeRow(row) if row else None)

        if "from pos_billing_orders where id = :id and owner_user_id = :uid" in q:
            row = self.store.find_by_id_owner(params["id"], params["uid"])
            return FakeResult(row=FakeRow(row) if row else None)

        if q.startswith("update pos_billing_orders set status = 'paid'"):
            row = self.store.mark_paid(params["id"], params["pid"])
            return FakeResult(row=FakeRow(row) if row else None)

        if q.startswith("update pos_billing_orders set status = 'failed'"):
            self.store.mark_failed(params["id"])
            return FakeResult(row=None)

        raise AssertionError(f"Unexpected query in test fake: {q}")

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

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


def _webhook_signature(body_bytes, secret=WEBHOOK_SECRET):
    return hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()


def _webhook_body(event, payment):
    return json.dumps({
        "event": event,
        "payload": {"payment": {"entity": payment}},
    }).encode()


class PosBillingVerificationTests(unittest.TestCase):
    ORDER_ID = "order_abc123"
    PAYMENT_ID = "pay_xyz789"
    AMOUNT = 39900

    def setUp(self):
        self.store = BillingOrderStore()
        self.store.add(
            id=1, owner_user_id=1, pos_plan="starter", amount_paise=self.AMOUNT,
            currency="INR", razorpay_order_id=self.ORDER_ID,
        )
        self.conn = FakeConn(self.store)
        conn_patcher = patch("routes.pos_routes.get_db_connection", lambda: self.conn)
        conn_patcher.start()
        self.addCleanup(conn_patcher.stop)

        self.rzp = MagicMock()
        self.rzp.utility.verify_payment_signature.return_value = True
        self.rzp.order.fetch.return_value = {
            "id": self.ORDER_ID, "status": "paid", "amount": self.AMOUNT, "currency": "INR",
        }
        self.rzp.payment.fetch.return_value = {
            "id": self.PAYMENT_ID, "status": "captured", "order_id": self.ORDER_ID,
            "amount": self.AMOUNT, "currency": "INR",
        }
        rzp_patcher = patch("routes.pos_routes.get_razorpay_client", return_value=self.rzp)
        rzp_patcher.start()
        self.addCleanup(rzp_patcher.stop)

        self.app = _make_app()
        self.client = self.app.test_client()

    def _auth_headers(self, uid=1):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def _verify_body(self, **overrides):
        body = {
            "billing_order_id": 1,
            "razorpay_order_id": self.ORDER_ID,
            "razorpay_payment_id": self.PAYMENT_ID,
            "razorpay_signature": "sig_ok",
        }
        body.update(overrides)
        return body

    def _verify(self, uid=1, **overrides):
        return self.client.post(
            "/api/pos/billing/verify",
            json=self._verify_body(**overrides),
            headers=self._auth_headers(uid),
        )

    # ---- Authentication ----

    def test_unauthenticated_verification_rejected(self):
        res = self.client.post("/api/pos/billing/verify", json=self._verify_body())
        self.assertEqual(res.status_code, 401)

    # ---- Verification ----

    def test_valid_payment_verifies_successfully(self):
        res = self._verify()
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["status"], "paid")
        self.assertEqual(body["razorpay_payment_id"], self.PAYMENT_ID)
        self.assertEqual(self.store.rows[1]["status"], "paid")

    def test_invalid_signature_rejected(self):
        self.rzp.utility.verify_payment_signature.side_effect = Exception("bad signature")
        res = self._verify()
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.store.rows[1]["status"], "created")

    def test_client_supplied_success_flag_does_not_bypass_signature_check(self):
        self.rzp.utility.verify_payment_signature.side_effect = Exception("bad signature")
        res = self._verify(success=True)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.store.rows[1]["status"], "created")

    def test_malformed_request_missing_signature_rejected(self):
        res = self.client.post(
            "/api/pos/billing/verify",
            json={"billing_order_id": 1, "razorpay_order_id": self.ORDER_ID,
                  "razorpay_payment_id": self.PAYMENT_ID},
            headers=self._auth_headers(),
        )
        self.assertEqual(res.status_code, 400)

    def test_malformed_request_non_integer_billing_order_id_rejected(self):
        res = self._verify(billing_order_id="1")
        self.assertEqual(res.status_code, 400)

    def test_unknown_local_billing_order_rejected(self):
        res = self._verify(billing_order_id=999)
        self.assertEqual(res.status_code, 404)

    def test_another_owners_billing_order_cannot_be_verified(self):
        res = self._verify(uid=2)
        self.assertEqual(res.status_code, 404)
        # No leak: same 404 shape as a genuinely unknown id.
        unknown = self._verify(uid=2, billing_order_id=999)
        self.assertEqual(res.get_json(), unknown.get_json())

    def test_mismatched_razorpay_order_id_rejected(self):
        res = self._verify(razorpay_order_id="order_someone_else")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.rows[1]["status"], "created")

    def test_mismatched_payment_order_relationship_rejected(self):
        self.rzp.payment.fetch.return_value = {
            "id": self.PAYMENT_ID, "status": "captured", "order_id": "order_different",
            "amount": self.AMOUNT, "currency": "INR",
        }
        res = self._verify()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.rows[1]["status"], "created")

    def test_mismatched_amount_rejected(self):
        self.rzp.payment.fetch.return_value = {
            "id": self.PAYMENT_ID, "status": "captured", "order_id": self.ORDER_ID,
            "amount": 1, "currency": "INR",
        }
        res = self._verify()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.rows[1]["status"], "created")

    def test_mismatched_currency_rejected(self):
        self.rzp.order.fetch.return_value = {
            "id": self.ORDER_ID, "status": "paid", "amount": self.AMOUNT, "currency": "USD",
        }
        res = self._verify()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.rows[1]["status"], "created")

    def test_order_not_paid_rejected(self):
        self.rzp.order.fetch.return_value = {
            "id": self.ORDER_ID, "status": "created", "amount": self.AMOUNT, "currency": "INR",
        }
        res = self._verify()
        self.assertEqual(res.status_code, 400)

    def test_payment_not_captured_rejected(self):
        self.rzp.payment.fetch.return_value = {
            "id": self.PAYMENT_ID, "status": "authorized", "order_id": self.ORDER_ID,
            "amount": self.AMOUNT, "currency": "INR",
        }
        res = self._verify()
        self.assertEqual(res.status_code, 400)

    def test_missing_razorpay_client_handled_safely(self):
        with patch("routes.pos_routes.get_razorpay_client", return_value=None):
            res = self._verify()
        self.assertEqual(res.status_code, 503)

    def test_razorpay_order_fetch_failure_handled_safely(self):
        self.rzp.order.fetch.side_effect = Exception("network error")
        res = self._verify()
        self.assertEqual(res.status_code, 502)
        self.assertEqual(self.store.rows[1]["status"], "created")

    # ---- Idempotency ----

    def test_same_verification_repeated_is_safe(self):
        first = self._verify()
        second = self._verify()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["status"], "paid")
        self.assertEqual(self.store.rows[1]["razorpay_payment_id"], self.PAYMENT_ID)

    def test_same_payment_cannot_be_attached_to_another_billing_order(self):
        self.store.add(
            id=2, owner_user_id=1, pos_plan="growth", amount_paise=79900,
            currency="INR", razorpay_order_id="order_other",
        )
        self._verify(billing_order_id=1)  # attaches PAYMENT_ID to order 1

        self.rzp.order.fetch.return_value = {
            "id": "order_other", "status": "paid", "amount": 79900, "currency": "INR",
        }
        self.rzp.payment.fetch.return_value = {
            "id": self.PAYMENT_ID, "status": "captured", "order_id": "order_other",
            "amount": 79900, "currency": "INR",
        }
        res = self._verify(billing_order_id=2, razorpay_order_id="order_other")

        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.rows[2]["status"], "created")

    def test_already_paid_order_same_payment_is_idempotent(self):
        self.store.rows[1].update(status="paid", razorpay_payment_id=self.PAYMENT_ID)
        res = self._verify()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "paid")

    def test_already_paid_order_different_payment_is_conflict(self):
        self.store.rows[1].update(status="paid", razorpay_payment_id="pay_original")
        res = self._verify()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.rows[1]["razorpay_payment_id"], "pay_original")

    def test_verification_followed_by_webhook_is_safe(self):
        verify_res = self._verify()
        self.assertEqual(verify_res.status_code, 200)

        webhook_app = _make_app()
        webhook_client = webhook_app.test_client()
        payment_entity = {
            "id": self.PAYMENT_ID, "order_id": self.ORDER_ID,
            "status": "captured", "amount": self.AMOUNT, "currency": "INR",
        }
        body = _webhook_body("payment.captured", payment_entity)
        with patch("routes.pos_routes.get_db_connection", lambda: self.conn), \
             patch("routes.pos_routes.get_razorpay_webhook_secret", return_value=WEBHOOK_SECRET):
            res = webhook_client.post(
                "/api/pos/billing/webhook", data=body,
                headers={"X-Razorpay-Signature": _webhook_signature(body),
                         "Content-Type": "application/json"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "already_processed")
        self.assertEqual(self.store.rows[1]["status"], "paid")

    def test_webhook_followed_by_verification_is_safe(self):
        payment_entity = {
            "id": self.PAYMENT_ID, "order_id": self.ORDER_ID,
            "status": "captured", "amount": self.AMOUNT, "currency": "INR",
        }
        body = _webhook_body("payment.captured", payment_entity)
        webhook_app = _make_app()
        webhook_client = webhook_app.test_client()
        with patch("routes.pos_routes.get_db_connection", lambda: self.conn), \
             patch("routes.pos_routes.get_razorpay_webhook_secret", return_value=WEBHOOK_SECRET):
            webhook_res = webhook_client.post(
                "/api/pos/billing/webhook", data=body,
                headers={"X-Razorpay-Signature": _webhook_signature(body),
                         "Content-Type": "application/json"},
            )
        self.assertEqual(webhook_res.status_code, 200)
        self.assertEqual(self.store.rows[1]["status"], "paid")

        verify_res = self._verify()
        self.assertEqual(verify_res.status_code, 200)
        self.assertEqual(verify_res.get_json()["status"], "paid")

    # ---- Subscription boundary ----

    def test_successful_verification_does_not_touch_pos_subscriptions_or_users(self):
        res = self._verify()
        self.assertEqual(res.status_code, 200)
        # FakeConn has no handler for users/pos_subscriptions -- reaching
        # here at all proves no such query was ever attempted.


class PosBillingWebhookTests(unittest.TestCase):
    ORDER_ID = "order_wh001"
    PAYMENT_ID = "pay_wh001"
    AMOUNT = 79900

    def setUp(self):
        self.store = BillingOrderStore()
        self.store.add(
            id=5, owner_user_id=3, pos_plan="growth", amount_paise=self.AMOUNT,
            currency="INR", razorpay_order_id=self.ORDER_ID,
        )
        self.conn = FakeConn(self.store)
        conn_patcher = patch("routes.pos_routes.get_db_connection", lambda: self.conn)
        conn_patcher.start()
        self.addCleanup(conn_patcher.stop)

        secret_patcher = patch(
            "routes.pos_routes.get_razorpay_webhook_secret", return_value=WEBHOOK_SECRET
        )
        secret_patcher.start()
        self.addCleanup(secret_patcher.stop)

        self.app = _make_app()
        self.client = self.app.test_client()

    def _post_webhook(self, body_bytes, signature=None):
        return self.client.post(
            "/api/pos/billing/webhook",
            data=body_bytes,
            headers={
                "X-Razorpay-Signature": signature if signature is not None else _webhook_signature(body_bytes),
                "Content-Type": "application/json",
            },
        )

    def _captured_entity(self, **overrides):
        entity = {
            "id": self.PAYMENT_ID, "order_id": self.ORDER_ID,
            "status": "captured", "amount": self.AMOUNT, "currency": "INR",
        }
        entity.update(overrides)
        return entity

    # ---- Authentication (signature, not JWT) ----

    def test_webhook_does_not_require_jwt(self):
        body = _webhook_body("payment.captured", self._captured_entity())
        res = self._post_webhook(body)
        # No Authorization header was sent at all -- succeeds purely on a
        # valid HMAC signature.
        self.assertEqual(res.status_code, 200)

    def test_valid_webhook_signature_accepted(self):
        body = _webhook_body("payment.captured", self._captured_entity())
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 200)

    def test_invalid_webhook_signature_rejected(self):
        body = _webhook_body("payment.captured", self._captured_entity())
        res = self._post_webhook(body, signature="not-the-real-signature")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.store.rows[5]["status"], "created")

    def test_missing_webhook_signature_rejected(self):
        body = _webhook_body("payment.captured", self._captured_entity())
        res = self.client.post(
            "/api/pos/billing/webhook", data=body, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(res.status_code, 403)

    def test_webhook_secret_not_configured_handled_safely(self):
        body = _webhook_body("payment.captured", self._captured_entity())
        with patch("routes.pos_routes.get_razorpay_webhook_secret", return_value=None):
            res = self._post_webhook(body)
        self.assertEqual(res.status_code, 503)

    # ---- Events ----

    def test_successful_payment_event_updates_local_billing_order(self):
        body = _webhook_body("payment.captured", self._captured_entity())
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.rows[5]["status"], "paid")
        self.assertEqual(self.store.rows[5]["razorpay_payment_id"], self.PAYMENT_ID)

    def test_failed_payment_event_handled_correctly(self):
        body = _webhook_body("payment.failed", self._captured_entity(status="failed"))
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.rows[5]["status"], "failed")

    def test_failed_event_never_regresses_an_already_paid_order(self):
        self.store.rows[5].update(status="paid", razorpay_payment_id=self.PAYMENT_ID)
        body = _webhook_body("payment.failed", self._captured_entity(status="failed"))
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.rows[5]["status"], "paid")

    def test_unsupported_event_handled_safely(self):
        body = _webhook_body("payment.authorized", self._captured_entity(status="authorized"))
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ignored")
        self.assertEqual(self.store.rows[5]["status"], "created")

    def test_malformed_webhook_payload_handled_safely(self):
        body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 400)

    def test_webhook_for_unknown_order_handled_safely(self):
        body = _webhook_body("payment.captured", self._captured_entity(order_id="order_unknown"))
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "order_not_found")

    def test_webhook_amount_mismatch_rejected(self):
        body = _webhook_body("payment.captured", self._captured_entity(amount=1))
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.rows[5]["status"], "created")

    # ---- Idempotency ----

    def test_duplicate_webhook_event_does_not_duplicate_effects(self):
        body = _webhook_body("payment.captured", self._captured_entity())
        first = self._post_webhook(body)
        second = self._post_webhook(body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["status"], "already_processed")
        self.assertEqual(self.store.rows[5]["razorpay_payment_id"], self.PAYMENT_ID)

    def test_webhook_payment_already_attached_to_another_order_is_conflict(self):
        self.store.add(
            id=6, owner_user_id=3, pos_plan="starter", amount_paise=39900,
            currency="INR", razorpay_order_id="order_other_wh",
        )
        first_body = _webhook_body("payment.captured", self._captured_entity())
        self._post_webhook(first_body)  # attaches PAYMENT_ID to order 5

        conflicting_entity = self._captured_entity(order_id="order_other_wh")
        # amount/currency must match order 6's own amount to reach the
        # attach step and hit the payment-id collision.
        conflicting_entity["amount"] = 39900
        second_body = _webhook_body("payment.captured", conflicting_entity)
        res = self._post_webhook(second_body)

        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.store.rows[6]["status"], "created")

    # ---- Subscription boundary ----

    def test_successful_webhook_does_not_touch_pos_subscriptions_or_users(self):
        body = _webhook_body("payment.captured", self._captured_entity())
        res = self._post_webhook(body)
        self.assertEqual(res.status_code, 200)
        # FakeConn has no handler for users/pos_subscriptions -- reaching
        # here at all proves no such query was ever attempted.


if __name__ == "__main__":
    unittest.main()
