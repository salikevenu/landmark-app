"""Pre-launch audit, section 5: Referral and agent commission.

One test class per checkbox in the "LANDMARK Pre-Launch Audit" doc, section 5.

Pass = each commission is paid once, only after the qualifying event, at the
right amount.

What is real and what is faked
------------------------------
* The money path is REAL: signup -> ``finalize_paid_order`` (the function both
  the verify endpoint and the Razorpay webhook call) -> commission outbox ->
  Saturday release -> wallet, all against PostgreSQL. Only the Razorpay
  signature/API check in front of it is skipped (covered by
  tests/test_payment_lifecycle.py); no Razorpay key or network is used.
* ``needs_db`` tests need a local throwaway PostgreSQL:

      set TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/landmark_test

  Every relevant table is TRUNCATEd on each test; never point it at a
  database you care about. .env is never read, and the module refuses to run
  against a non-local address.

xfail(strict=True)
------------------
Marks behaviour the audit expects that does not exist yet; each names the
audit note explaining it. A strict xfail turns into a hard failure the moment
the behaviour appears, so the marker cannot go stale. Remove it when you fix
the gap (or flip the assertion if you decide the current behaviour is right).
"""
import hashlib
import hmac
import json
import os
import sys
import threading
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Environment isolation (must happen before any project import) -------
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False  # never read .env in this module

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_local_url(url):
    try:
        return (urlparse(url).hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


TEST_DB_URL = (os.getenv("TEST_DATABASE_URL") or "").strip()
if TEST_DB_URL and not _is_local_url(TEST_DB_URL):
    raise RuntimeError("TEST_DATABASE_URL must point at a local PostgreSQL; refusing to run.")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", TEST_DB_URL or "postgresql://test:test@127.0.0.1:5432/test")
os.environ["REDIS_URL"] = ""
os.environ.pop("RENDER", None)

if not _is_local_url(os.environ["DATABASE_URL"]):
    raise RuntimeError("DATABASE_URL must be a local address for tests/test_referral_audit.py; refusing to run.")

# app MUST be imported before routes.auth_routes (rate limits bind at import time).
import app as _app_module  # noqa: E402,F401
from flask import Flask  # noqa: E402
from flask_jwt_extended import JWTManager, create_access_token  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

import database.init_db as dbmod  # noqa: E402
import routes.payment_routes as payment_routes_mod  # noqa: E402
from config.payment_config import duration_days_for_stored_amount, get_plan_spec  # noqa: E402
from routes import auth_routes  # noqa: E402
from routes.referral_routes import referral_bp  # noqa: E402
from routes.wallet_routes import wallet_bp  # noqa: E402
from services import wallet_service  # noqa: E402
from services.payment_service import finalize_paid_order  # noqa: E402
from services.referral_commission import (  # noqa: E402
    after_payment_finalized,
    enqueue_referral_commission_job,
    process_pending_referral_commission_jobs,
    release_locked_referral_payouts,
)

needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="set TEST_DATABASE_URL to a local test PostgreSQL")

MONEY = pytest.approx  # amounts are REAL columns; compare to the paisa


def rupees(value):
    return pytest.approx(value, abs=0.005)


# =========================================================================
# Fixtures and helpers
# =========================================================================
@pytest.fixture(scope="module")
def pg_engine():
    if not TEST_DB_URL:
        pytest.skip("set TEST_DATABASE_URL to a local test PostgreSQL")
    eng = create_engine(TEST_DB_URL)
    with eng.connect() as conn:
        dbmod._init_db_body(conn)
        conn.commit()
    yield eng
    eng.dispose()


@pytest.fixture
def pg(pg_engine, monkeypatch):
    """Every DB call in auth, payment, wallet and commission code goes to the test DB, emptied."""
    monkeypatch.setattr(dbmod, "engine", pg_engine)
    monkeypatch.setattr(auth_routes, "engine", pg_engine)
    with pg_engine.connect() as conn:
        conn.execute(text(
            "TRUNCATE users, pending_referrals, payments, wallet_transactions, wallet_balance, "
            "referral_commission_jobs, withdraw_requests, referral_transactions RESTART IDENTITY CASCADE"
        ))
        conn.commit()
    return pg_engine


def new_user(engine, phone, code, referred_by=None, ip="203.0.113.1"):
    with engine.connect() as conn:
        uid = conn.execute(text("""
            INSERT INTO users (phone, name, role, referral_code, referred_by, ip_address, created_at)
            VALUES (:p, '', 'free', :c, :r, :ip, CURRENT_TIMESTAMP) RETURNING id
        """), {"p": phone, "c": code, "r": referred_by, "ip": ip}).scalar()
        conn.commit()
    return uid


def new_order(engine, uid, order_id, amount_paise=69900, plan="business_basic", status="created"):
    """What POST /api/payment/create-order stores: payment_id holds the order id until paid."""
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO payments (user_id, order_id, payment_id, amount, status, plan, created_at)
            VALUES (:u, :o, :o, :a, :s, :pl, CURRENT_TIMESTAMP)
        """), {"u": uid, "o": order_id, "a": amount_paise, "s": status, "pl": plan})
        conn.commit()


def pay(uid, order_id, payment_id, amount_paise=69900, plan="business_basic"):
    """A captured, verified payment: exactly what verify-payment and the webhook do next."""
    _, spec = get_plan_spec(plan)
    _cycle, days = duration_days_for_stored_amount(spec["amount_paise"], amount_paise)
    result = finalize_paid_order(order_id, payment_id, spec, amount_paise, user_id=uid, duration_days=days)
    assert result["success"], result
    after_payment_finalized(result, razorpay_payment_id=payment_id)
    return result


def ledger(engine, uid):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT source, amount, status, type, razorpay_payment_id
            FROM wallet_transactions WHERE user_id = :u ORDER BY id
        """), {"u": uid}).fetchall()
    return [dict(r._mapping) for r in rows]


def balance(engine, uid):
    with engine.connect() as conn:
        value = conn.execute(text("SELECT balance FROM wallet_balance WHERE user_id = :u"), {"u": uid}).scalar()
    return 0.0 if value is None else float(value)


def unlock_now(engine):
    with engine.connect() as conn:
        conn.execute(text("UPDATE wallet_transactions SET unlock_at = NOW() - INTERVAL '1 hour' WHERE status = 'locked'"))
        conn.commit()


def http_app():
    app = Flask(__name__)
    app.config.update(SECRET_KEY="test-secret", JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
                      JWT_TOKEN_LOCATION=["headers"])
    JWTManager(app)
    app.register_blueprint(wallet_bp)
    app.register_blueprint(referral_bp)
    app.register_blueprint(payment_routes_mod.payment_bp, url_prefix="/api/payment")
    app.register_blueprint(auth_routes.auth_bp, url_prefix="/api/auth")
    return app


def auth_header(app, uid):
    with app.app_context():
        return {"Authorization": f"Bearer {create_access_token(identity=str(uid))}"}


def signup(app, phone, ip, ref=None):
    """The real referral + account-creation helpers, exactly as verify-otp calls them."""
    data = {"ref": ref} if ref else {}
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": ip}):
        ok, err = auth_routes.persist_referral_for_phone(phone, data)
        assert ok, err
        referrer_id, err = auth_routes.resolve_referrer_id_for_signup(phone, data)
        assert not err, err
        return auth_routes.get_or_create_user(phone, ip_address=ip, referrer_id=referrer_id)


# =========================================================================
# 1. Valid referral code links the new user to the right agent
# =========================================================================
@needs_db
class TestValidCodeLinksToTheRightAgent:
    def test_new_user_is_linked_to_the_agent_whose_code_was_used(self, pg):
        agent_a = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        agent_c = new_user(pg, "9000000003", "AGENTCCC", ip="198.51.100.3")
        app = Flask(__name__)
        app.config["SECRET_KEY"] = "test-secret"
        to_a, _ = signup(app, "9111111111", "203.0.113.11", ref="AGENTAAA")
        to_c, _ = signup(app, "9222222222", "203.0.113.12", ref="AGENTCCC")
        assert to_a["referred_by"] == agent_a
        assert to_c["referred_by"] == agent_c

    def test_the_link_is_stored_and_never_reassigned_on_a_later_login(self, pg):
        agent_a = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        agent_c = new_user(pg, "9000000003", "AGENTCCC", ip="198.51.100.3")
        app = Flask(__name__)
        app.config["SECRET_KEY"] = "test-secret"
        first, _ = signup(app, "9111111111", "203.0.113.11", ref="AGENTAAA")
        again, status = signup(app, "9111111111", "203.0.113.11", ref="AGENTCCC")
        assert status == "existing" and again["referred_by"] == agent_a != agent_c


# =========================================================================
# 2. Invalid or expired code rejected cleanly (signup still works)
# =========================================================================
class TestInvalidOrExpiredCode:
    @needs_db
    def test_an_expired_pending_referral_is_ignored_and_signup_works_without_an_agent(self, pg):
        agent = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        with pg.connect() as conn:
            conn.execute(text("""
                INSERT INTO pending_referrals (phone, ref_code, referrer_id, created_at, expires_at)
                VALUES ('9111111111', 'AGENTAAA', :a, NOW() - INTERVAL '9 days', NOW() - INTERVAL '2 days')
            """), {"a": agent})
            conn.commit()
        app = Flask(__name__)
        app.config["SECRET_KEY"] = "test-secret"
        user, status = signup(app, "9111111111", "203.0.113.11")
        assert status == "new" and user["referred_by"] is None

    def test_an_unknown_code_is_rejected_with_a_clear_message(self):
        app = Flask(__name__)
        app.config["SECRET_KEY"] = "test-secret"
        with patch.object(auth_routes, "fetch_referrer_by_code", return_value=None):
            with app.test_request_context("/"):
                assert auth_routes.persist_referral_for_phone("9111111111", {"ref": "NOPE1234"}) == (
                    False, "Invalid referral code.")
                assert auth_routes.resolve_referrer_id_for_signup("9111111111", {"ref": "NOPE1234"}) == (
                    None, "Invalid referral code.")

    @pytest.mark.xfail(strict=True, reason="audit note 5.2: an unknown code makes send-otp answer 400 and no OTP is "
                                           "sent, so signup stops until the code is removed (decision pending)")
    def test_an_unknown_code_does_not_stop_the_otp_being_sent(self):
        app = http_app()
        sms = MagicMock()
        sms.send_otp.return_value = (True, {"responseCode": 200}, "vid-1")
        if auth_routes.limiter is not None:
            auth_routes.limiter.reset()
        with patch.object(auth_routes, "fetch_referrer_by_code", return_value=None), \
             patch.object(auth_routes, "get_verification", return_value=None), \
             patch.object(auth_routes, "store_verification"), \
             patch.object(auth_routes, "get_sms_service", return_value=sms):
            res = app.test_client().post(
                "/api/auth/send-otp", json={"phone": "9111111111", "ref": "NOPE1234"},
                environ_base={"REMOTE_ADDR": "203.0.113.21"},
            )
        assert res.status_code == 200


# =========================================================================
# 3. 10% commission credited only after a successful subscription payment
# =========================================================================
@needs_db
class TestCommissionAfterPayment:
    def _agent_and_referred(self, pg):
        agent = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        referred = new_user(pg, "9000000002", "USERBBBB", referred_by=agent, ip="198.51.100.2")
        return agent, referred

    def test_an_unpaid_order_earns_the_agent_nothing_and_the_job_waits(self, pg):
        agent, referred = self._agent_and_referred(pg)
        new_order(pg, referred, "order_1")  # created, never paid
        with pg.connect() as conn:
            enqueue_referral_commission_job(conn, "order_1", "order_1", referred, 699.0)
            conn.commit()
        result = process_pending_referral_commission_jobs(razorpay_payment_id="order_1")
        assert result["failed"] and not result["processed"]
        assert ledger(pg, agent) == []
        with pg.connect() as conn:
            job = conn.execute(text("SELECT status, attempts, last_error FROM referral_commission_jobs")).fetchone()
        assert job.status == "pending" and job.attempts == 1 and "not in activated state" in job.last_error

    def test_first_monthly_payment_pays_a_fixed_bonus_by_plan_not_ten_percent(self, pg):
        """Box text says 10%; the code (deliberately, see referral_commission.py) pays Rs 100 for
        Business Basic's first monthly payment. 10% would be Rs 69.90. Audit note 5.3."""
        agent, referred = self._agent_and_referred(pg)
        new_order(pg, referred, "order_1")
        pay(referred, "order_1", "pay_1")
        rows = ledger(pg, agent)
        assert [(r["source"], r["status"]) for r in rows] == [("referral_first_bonus", "locked")]
        assert rows[0]["amount"] == rupees(100.0)

    def test_every_later_payment_pays_ten_percent_of_the_amount_paid(self, pg):
        agent, referred = self._agent_and_referred(pg)
        new_order(pg, referred, "order_1")
        pay(referred, "order_1", "pay_1")
        new_order(pg, referred, "order_2")
        pay(referred, "order_2", "pay_2")
        rows = ledger(pg, agent)
        assert [r["source"] for r in rows] == ["referral_first_bonus", "referral_recurring"]
        assert rows[1]["amount"] == rupees(69.90)  # 10% of Rs 699

    def test_a_first_three_month_payment_pays_ten_percent_not_the_fixed_bonus(self, pg):
        agent, referred = self._agent_and_referred(pg)
        new_order(pg, referred, "order_1", amount_paise=188730)  # Business Basic, 3 months
        pay(referred, "order_1", "pay_1", amount_paise=188730)
        rows = ledger(pg, agent)
        assert len(rows) == 1 and rows[0]["source"] == "referral_first_bonus"
        assert rows[0]["amount"] == rupees(188.73)  # 10% of Rs 1,887.30

    def test_commission_stays_locked_until_the_saturday_release_then_becomes_spendable(self, pg):
        agent, referred = self._agent_and_referred(pg)
        new_order(pg, referred, "order_1")
        pay(referred, "order_1", "pay_1")
        assert balance(pg, agent) == 0.0  # locked, not spendable
        release_locked_referral_payouts()
        assert balance(pg, agent) == 0.0  # unlock_at is still in the future
        unlock_now(pg)
        assert release_locked_referral_payouts() == 1
        assert balance(pg, agent) == rupees(100.0)
        assert [r["status"] for r in ledger(pg, agent)] == ["released"]

    def test_the_legacy_flat_reward_paths_are_disabled(self):
        from agents.referral_agent import ReferralAgent
        from services.referral_service import process_referral_reward

        assert process_referral_reward(1, "basic", "pay_x") is None
        assert wallet_service.process_referral(1, 999) is None
        assert wallet_service.add_pending_referral_reward(1, 1) is False
        assert ReferralAgent().process_referral_reward(1, "basic")["success"] is False


# =========================================================================
# 4. Signup commission only after the qualifying action, not at bare signup
# =========================================================================
@needs_db
class TestNoCommissionAtBareSignup:
    def test_signing_up_and_logging_in_credits_the_agent_nothing(self, pg):
        agent = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        app = Flask(__name__)
        app.config["SECRET_KEY"] = "test-secret"
        user, _ = signup(app, "9111111111", "203.0.113.11", ref="AGENTAAA")
        signup(app, "9111111111", "203.0.113.11")  # a later login
        assert user["referred_by"] == agent
        assert ledger(pg, agent) == [] and balance(pg, agent) == 0.0
        with pg.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM referral_commission_jobs")).scalar() == 0

    def test_no_code_path_credits_a_flat_rupee_five_signup_commission(self):
        """The doc's Rs 5 item has no counterpart in the code: nothing credits a fixed Rs 5 (audit note 5.4)."""
        import re

        offenders = []
        for folder in ("routes", "services", "agents", "utils"):
            for path in (ROOT / folder).glob("*.py"):
                src = path.read_text(encoding="utf-8-sig")
                if re.search(r"(?i)signup_bonus|signup_commission|SIGNUP_REWARD", src):
                    offenders.append(path.name)
        assert offenders == []


# =========================================================================
# 5. Referred user who never qualifies earns the agent nothing
# =========================================================================
@needs_db
class TestNonQualifyingReferral:
    def test_created_or_failed_payments_earn_nothing(self, pg):
        agent = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        referred = new_user(pg, "9000000002", "USERBBBB", referred_by=agent, ip="198.51.100.2")
        new_order(pg, referred, "order_created")
        new_order(pg, referred, "order_failed", status="failed")
        for order in ("order_created", "order_failed"):
            with pg.connect() as conn:
                enqueue_referral_commission_job(conn, order, order, referred, 699.0)
                conn.commit()
            process_pending_referral_commission_jobs(razorpay_payment_id=order)
        assert ledger(pg, agent) == []
        with pg.connect() as conn:
            paid_flag = conn.execute(text("SELECT first_sub_commission_paid FROM users WHERE id = :u"),
                                     {"u": referred}).scalar()
        assert paid_flag == 0  # the one-time first-sale bonus is not burnt by a non-qualifying attempt


# =========================================================================
# 6. Each commission credited once, even if the trigger fires twice
# =========================================================================
@needs_db
class TestCommissionCreditedOnce:
    def _setup(self, pg):
        agent = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        referred = new_user(pg, "9000000002", "USERBBBB", referred_by=agent, ip="198.51.100.2")
        new_order(pg, referred, "order_1")
        return agent, referred

    def test_the_same_payment_notified_twice_credits_once(self, pg):
        agent, referred = self._setup(pg)
        first = pay(referred, "order_1", "pay_1")
        second = pay(referred, "order_1", "pay_1")  # verify endpoint AND webhook both fire
        assert not first.get("duplicate") and second["duplicate"] is True
        assert len(ledger(pg, agent)) == 1
        unlock_now(pg)
        release_locked_referral_payouts()
        release_locked_referral_payouts()  # a second Saturday run must not pay again
        assert balance(pg, agent) == rupees(100.0)

    def test_two_simultaneous_notifications_credit_once(self, pg):
        agent, referred = self._setup(pg)
        barrier = threading.Barrier(2)
        errors = []

        def notify():
            try:
                barrier.wait()
                pay(referred, "order_1", "pay_1")
            except Exception as exc:  # pragma: no cover - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=notify) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == []
        assert len(ledger(pg, agent)) == 1
        with pg.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM referral_commission_jobs")).scalar() == 1

    def test_two_simultaneous_saturday_releases_credit_once(self, pg):
        agent, referred = self._setup(pg)
        pay(referred, "order_1", "pay_1")
        unlock_now(pg)
        barrier = threading.Barrier(2)
        released = []

        def run():
            barrier.wait()
            released.append(release_locked_referral_payouts())

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert sum(released) == 1
        assert balance(pg, agent) == rupees(100.0)

    @pytest.mark.xfail(strict=True, reason="audit note 5.6: the unique commission indexes are created only by "
                                           "migrations/add_referral_commission_money_safety.py, never by init_db, "
                                           "so a fresh database has no database-level guard")
    def test_a_fresh_database_has_the_unique_commission_indexes(self):
        src = (ROOT / "database" / "init_db.py").read_text(encoding="utf-8-sig")
        for index in ("uq_wallet_tx_source_razorpay_payment", "uq_wallet_tx_first_bonus_reference",
                      "uq_payments_order_id_not_null"):
            assert index in src, index


# =========================================================================
# 7. Commission reversed when the subscription is refunded
# =========================================================================
@needs_db
class TestRefundReversesCommission:
    @pytest.mark.xfail(strict=True, reason="audit note 5.7: refund webhooks are ignored; the referrer's commission "
                                           "stays locked and is paid out on the next Saturday")
    def test_a_refund_cancels_the_locked_commission(self, pg):
        agent = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        referred = new_user(pg, "9000000002", "USERBBBB", referred_by=agent, ip="198.51.100.2")
        new_order(pg, referred, "order_ref1")
        pay(referred, "order_ref1", "pay_ref1")
        assert [r["status"] for r in ledger(pg, agent)] == ["locked"]

        body = json.dumps({
            "event": "refund.processed",
            "payload": {
                "payment": {"entity": {"id": "pay_ref1", "order_id": "order_ref1", "status": "refunded",
                                       "amount": 69900, "notes": {"user_id": str(referred)}}},
                "refund": {"entity": {"id": "rfnd_1", "payment_id": "pay_ref1", "amount": 69900}},
            },
        }).encode()
        signature = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
        with patch.object(payment_routes_mod, "get_razorpay_webhook_secret", return_value="whsec_test"):
            res = http_app().test_client().post(
                "/api/payment/razorpay/webhook", data=body,
                headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"},
            )
        assert res.status_code == 200
        unlock_now(pg)
        release_locked_referral_payouts()
        assert balance(pg, agent) == 0.0  # a refunded payment must not pay a commission out


# =========================================================================
# 8. Agent can see their earnings; numbers match the ledger
# =========================================================================
@needs_db
class TestAgentSeesEarnings:
    def _paid(self, pg):
        agent = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        referred = new_user(pg, "9000000002", "USERBBBB", referred_by=agent, ip="198.51.100.2")
        new_order(pg, referred, "order_1")
        pay(referred, "order_1", "pay_1")
        new_order(pg, referred, "order_2")
        pay(referred, "order_2", "pay_2")
        return agent, referred

    def test_wallet_overview_matches_the_ledger_before_and_after_release(self, pg):
        agent, _ = self._paid(pg)
        client, headers = http_app().test_client(), None
        headers = auth_header(client.application, agent)
        locked_total = sum(r["amount"] for r in ledger(pg, agent) if r["status"] == "locked")
        overview = client.get("/api/wallet/overview", headers=headers).get_json()
        assert overview["pending_unlock"] == rupees(locked_total) == rupees(169.90)
        assert overview["available_balance"] == rupees(0.0)

        unlock_now(pg)
        release_locked_referral_payouts()
        overview = client.get("/api/wallet/overview", headers=headers).get_json()
        assert overview["pending_unlock"] == rupees(0.0)
        assert overview["available_balance"] == rupees(169.90) == rupees(balance(pg, agent))

    def test_wallet_transaction_list_shows_every_ledger_row(self, pg):
        agent, _ = self._paid(pg)
        client = http_app().test_client()
        rows = client.get("/api/wallet/transactions", headers=auth_header(client.application, agent)).get_json()
        assert [r["source"] for r in rows] == ["referral_recurring", "referral_first_bonus"]  # newest first
        assert [round(r["amount"], 2) for r in rows] == [69.9, 100.0]

    def test_an_agent_only_ever_sees_their_own_wallet(self, pg):
        agent, referred = self._paid(pg)
        client = http_app().test_client()
        overview = client.get("/api/wallet/overview", headers=auth_header(client.application, referred)).get_json()
        assert overview["pending_unlock"] == rupees(0.0) and overview["available_balance"] == rupees(0.0)

    @pytest.mark.xfail(strict=True, reason="audit note 5.8: the dashboard's Referral Earnings tile reads "
                                           "referral_earnings, which /api/payment/wallet never returns, so it "
                                           "always shows Rs 0")
    def test_the_dashboard_earnings_tile_gets_a_number_from_the_backend(self, pg):
        agent, _ = self._paid(pg)
        client = http_app().test_client()
        data = client.get("/api/payment/wallet", headers=auth_header(client.application, agent)).get_json()
        assert data["referral_earnings"] == rupees(169.90)

    @pytest.mark.xfail(strict=True, reason="audit note 5.8: the leaderboard (and admin referral counts) read "
                                           "referral_transactions, which nothing writes to, so they stay empty")
    def test_the_referral_leaderboard_counts_real_referrals(self, pg):
        self._paid(pg)
        rows = http_app().test_client().get("/api/referral-leaderboard").get_json()
        assert [r["total_referrals"] for r in rows] == [1]

    def test_nothing_writes_to_the_referral_transactions_table(self):
        """Documents the cause of the two xfail tests above."""
        import re

        writers = []
        for folder in ("routes", "services", "agents", "utils", "migrations"):
            for path in (ROOT / folder).glob("*.py"):
                if re.search(r"(?i)(insert\s+into|update|delete\s+from)\s+referral_transactions", path.read_text(encoding="utf-8-sig")):
                    writers.append(path.name)
        assert writers == []


# =========================================================================
# Money precision (affects boxes 3, 6 and 8: amounts credited, summed, withdrawn)
# =========================================================================
@needs_db
class TestWalletMoneyPrecision:
    @pytest.mark.xfail(strict=True, reason="audit note 5.9: wallet_balance.balance and wallet_transactions.amount "
                                           "are REAL (4-byte float); 100 credits of Rs 1259.16 sum to 125915.83, "
                                           "not 125916.00")
    def test_a_hundred_credits_add_up_to_the_exact_amount(self, pg):
        user = new_user(pg, "9000000001", "AGENTAAA", ip="198.51.100.1")
        for _ in range(100):
            assert wallet_service.credit_wallet(user, "1259.16", source="test")
        assert wallet_service.get_wallet_balance(user) == Decimal("125916.00")
