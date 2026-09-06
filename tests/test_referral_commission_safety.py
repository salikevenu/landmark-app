"""Stage 2B: concurrency, idempotency, outbox recovery, payout races."""
import os
import sqlite3
import sys
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util
from sqlalchemy.exc import IntegrityError

from services import referral_commission as rc
from services.referral_commission import (
    FIRST_BONUS_SOURCE,
    RECURRING_SOURCE,
    after_payment_finalized,
    enqueue_referral_commission_job,
    process_pending_referral_commission_jobs,
    process_referral_commission,
    release_locked_referral_payouts,
)
from services.wallet_service import add_pending_referral_reward, process_referral
from services.referral_service import process_referral_reward


def _load(name, relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


referral_agent_mod = _load("referral_agent_iso", "agents/referral_agent.py")
ReferralAgent = referral_agent_mod.ReferralAgent


def _sql(query):
    return " ".join(str(getattr(query, "text", query)).lower().split())


class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping

    def __getitem__(self, index):
        return list(self._mapping.values())[index]


class FakeResult:
    def __init__(self, row=None, rows=None, rowcount=0):
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class CommissionLedger:
    def __init__(self):
        self.lock = threading.RLock()
        self.users = {}
        self.user_locks = {}
        self.txs = []
        self.tx_locks = {}
        self.jobs = []
        self.job_locks = {}
        self.wallet = {}
        self.next_tx_id = 1
        self.next_job_id = 1
        self.users_wallet_column = {}
        self.payments = {}

    def add_user(self, uid, referred_by=None, first_paid=False, wallet_col=0):
        self.users[uid] = {
            "id": uid,
            "referred_by": referred_by,
            "first_sub_commission_paid": 1 if first_paid else 0,
        }
        self.users_wallet_column[uid] = wallet_col
        self.user_locks[uid] = threading.Lock()
        return self.users[uid]

    def add_payment(self, payment_id, user_id, plan, status="activated"):
        """Seed a payments row — process_referral_commission looks up the
        plan for the fixed first-sale bonus via (payment_id, user_id)."""
        self.payments[str(payment_id)] = {
            "payment_id": str(payment_id),
            "user_id": user_id,
            "plan": plan,
            "status": status,
        }

    def connect(self):
        return LedgerConn(self)


class LedgerConn:
    def __init__(self, store):
        self.store = store
        self.held = []

    def begin_nested(self):
        return nullcontext()

    def _hold(self, lock, blocking=True):
        if lock.acquire(blocking=blocking):
            self.held.append(lock)
            return True
        return False

    def _release_held(self):
        while self.held:
            lock = self.held.pop()
            try:
                lock.release()
            except RuntimeError:
                pass

    def commit(self):
        self._release_held()

    def rollback(self):
        self._release_held()

    def close(self):
        self._release_held()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._release_held()
        return False

    def execute(self, query, params=None):
        q = _sql(query)
        params = params or {}
        with self.store.lock:
            return self._execute_locked(q, params)

    def _execute_locked(self, q, params):
        if "from users" in q and "for update" in q:
            uid = params["uid"]
            user = self.store.users.get(uid)
            ulock = self.store.user_locks.get(uid)
            if ulock:
                # release store.lock while waiting so another thread can finish
                self.store.lock.release()
                try:
                    self._hold(ulock, blocking=True)
                finally:
                    self.store.lock.acquire()
            if not user:
                return FakeResult(None)
            return FakeResult(FakeRow(dict(user)))

        if "from payments" in q:
            pid = params.get("pid")
            uid = params.get("uid")
            payment = self.store.payments.get(str(pid))
            if payment and payment["user_id"] == uid:
                return FakeResult(FakeRow(dict(payment)))
            return FakeResult(None)

        if "update users set first_sub_commission_paid" in q:
            uid = params["uid"]
            if uid in self.store.users:
                self.store.users[uid]["first_sub_commission_paid"] = 1
            return FakeResult(rowcount=1)

        if "select 1 from wallet_transactions" in q:
            rzp = params.get("rzp")
            ref_id = params.get("ref_id")
            exists = any(
                t.get("razorpay_payment_id") == rzp and t.get("reference_id") == ref_id
                for t in self.store.txs
            )
            return FakeResult(FakeRow({"exists": 1}) if exists else None)

        if "insert into wallet_transactions" in q:
            source = params["source"]
            ref_id = params["ref_id"]
            rzp = params.get("rzp")
            if source == FIRST_BONUS_SOURCE:
                for tx in self.store.txs:
                    if tx["source"] == source and tx["reference_id"] == ref_id:
                        raise IntegrityError("uq_first_bonus", params, Exception("unique"))
            if rzp:
                for tx in self.store.txs:
                    if tx["source"] == source and tx.get("razorpay_payment_id") == rzp:
                        raise IntegrityError("uq_source_payment", params, Exception("unique"))
            tid = self.store.next_tx_id
            self.store.next_tx_id += 1
            row = {
                "id": tid,
                "user_id": params["referrer_id"],
                "amount": params["amount"],
                "type": "credit",
                "source": source,
                "reference_id": ref_id,
                "status": "locked",
                "unlock_at": datetime.utcnow() - timedelta(hours=1),
                "razorpay_payment_id": rzp,
            }
            self.store.txs.append(row)
            self.store.tx_locks[tid] = threading.Lock()
            return FakeResult(rowcount=1)

        if "insert into referral_commission_jobs" in q:
            pid = str(params["payment_id"])
            if any(j["payment_id"] == pid for j in self.store.jobs):
                return FakeResult(rowcount=0)
            jid = self.store.next_job_id
            self.store.next_job_id += 1
            job = {
                "id": jid,
                "payment_id": pid,
                "razorpay_payment_id": str(params.get("razorpay_payment_id") or pid),
                "referred_user_id": params["referred_user_id"],
                "amount_rupees": params["amount_rupees"],
                "status": params.get("status") or "pending",
                "attempts": 0,
                "last_error": None,
            }
            self.store.jobs.append(job)
            self.store.job_locks[jid] = threading.Lock()
            return FakeResult(rowcount=1)

        if "from referral_commission_jobs" in q:
            pid = params.get("pid")
            selected = []
            for job in self.store.jobs:
                if job["status"] != "pending":
                    continue
                if pid and job["razorpay_payment_id"] != str(pid) and job["payment_id"] != str(pid):
                    continue
                jlock = self.store.job_locks[job["id"]]
                self.store.lock.release()
                try:
                    got = self._hold(jlock, blocking=False)
                finally:
                    self.store.lock.acquire()
                if got:
                    selected.append(FakeRow(dict(job)))
            return FakeResult(rows=selected)

        if "update referral_commission_jobs" in q:
            job = next((j for j in self.store.jobs if j["id"] == params["id"]), None)
            if not job or job["status"] != "pending":
                return FakeResult(rowcount=0)
            if "attempts = attempts + 1" in q:
                job["attempts"] += 1
                job["last_error"] = params.get("err")
            else:
                job["status"] = params["status"]
                job["last_error"] = None
            return FakeResult(rowcount=1)

        if "from wallet_transactions" in q and "for update skip locked" in q:
            selected = []
            now = datetime.utcnow()
            for tx in self.store.txs:
                if tx["status"] != "locked":
                    continue
                if tx["source"] not in (FIRST_BONUS_SOURCE, RECURRING_SOURCE):
                    continue
                if tx.get("unlock_at") and tx["unlock_at"] > now:
                    continue
                tlock = self.store.tx_locks[tx["id"]]
                self.store.lock.release()
                try:
                    got = self._hold(tlock, blocking=False)
                finally:
                    self.store.lock.acquire()
                if got:
                    selected.append(FakeRow({
                        "id": tx["id"],
                        "user_id": tx["user_id"],
                        "amount": tx["amount"],
                    }))
            return FakeResult(rows=selected)

        if "update wallet_transactions" in q and "status = 'released'" in q:
            tid = params["tid"]
            tx = next((t for t in self.store.txs if t["id"] == tid), None)
            if not tx or tx["status"] != "locked":
                return FakeResult(rowcount=0)
            tx["status"] = "released"
            return FakeResult(rowcount=1)

        if "insert into wallet_balance" in q:
            uid = params["uid"]
            amt = params["amt"]
            self.store.wallet[uid] = self.store.wallet.get(uid, 0) + amt
            return FakeResult(rowcount=1)

        if "update users set wallet_balance" in q:
            raise AssertionError("payout must not update users.wallet_balance")

        return FakeResult()


class CommissionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.ledger = CommissionLedger()
        self.referrer = 1
        self.referred = 2
        self.ledger.add_user(self.referrer)
        self.ledger.add_user(self.referred, referred_by=self.referrer)

    def _patch(self):
        return patch.object(rc, "get_db_connection", side_effect=self.ledger.connect)

    def _by_source(self):
        return [t["source"] for t in self.ledger.txs]

    def test_a_first_commission_concurrency(self):
        self.ledger.add_payment("pay_same", self.referred, "service_provider")
        errors = []

        def run(pay_id):
            try:
                with self._patch():
                    process_referral_commission(self.referred, 100, pay_id)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run, args=("pay_same",))
        t2 = threading.Thread(target=run, args=("pay_same",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])
        firsts = [t for t in self.ledger.txs if t["source"] == FIRST_BONUS_SOURCE]
        self.assertEqual(len(firsts), 1)
        self.assertEqual(firsts[0]["amount"], 50)
        # Same payment_id raced twice: the loser must be blocked by the
        # payment-level idempotency guard, not fall through to a second
        # (wrongly-sourced) commission for the identical payment.
        recurrings = [t for t in self.ledger.txs if t["source"] == RECURRING_SOURCE]
        self.assertEqual(len(recurrings), 0)

    def test_a_two_payments_still_one_first_bonus(self):
        self.ledger.add_payment("pay_a", self.referred, "service_provider")
        self.ledger.add_payment("pay_b", self.referred, "service_provider")
        errors = []

        def run(pay_id):
            try:
                with self._patch():
                    process_referral_commission(self.referred, 100, pay_id)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run, args=("pay_a",))
        t2 = threading.Thread(target=run, args=("pay_b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])
        firsts = [t for t in self.ledger.txs if t["source"] == FIRST_BONUS_SOURCE]
        self.assertEqual(len(firsts), 1)
        self.assertEqual(firsts[0]["amount"], 50)
        # Two genuinely different payments for the same referred user: the
        # winner of the first-bonus race gets ONLY the fixed bonus; the
        # other (processed once the flag is already set) gets ONLY the 10%
        # recurring commission for its own (different) payment.
        recurrings = [t for t in self.ledger.txs if t["source"] == RECURRING_SOURCE]
        self.assertEqual(len(recurrings), 1)

    def test_b_same_payment_retry(self):
        self.ledger.add_payment("pay_1", self.referred, "business_basic")
        with self._patch():
            process_referral_commission(self.referred, 499, "pay_1")
            process_referral_commission(self.referred, 499, "pay_1")
        self.assertEqual(self._by_source().count(FIRST_BONUS_SOURCE), 1)
        # Retrying the SAME payment_id must not also mint a recurring
        # commission for it once the flag it flipped makes the second
        # attempt look like a "later" payment — the idempotency guard
        # blocks any further processing of an already-handled payment.
        self.assertEqual(self._by_source().count(RECURRING_SOURCE), 0)
        self.assertEqual({t["razorpay_payment_id"] for t in self.ledger.txs}, {"pay_1"})

    def test_c_verify_webhook_race(self):
        self.ledger.add_payment("pay_vw", self.referred, "service_provider")
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_vw", "pay_vw", self.referred, 100)
        conn.commit()
        errors = []

        def run():
            try:
                with self._patch():
                    process_pending_referral_commission_jobs(razorpay_payment_id="pay_vw")
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])
        self.assertEqual(self._by_source().count(FIRST_BONUS_SOURCE), 1)
        self.assertEqual(self._by_source().count(RECURRING_SOURCE), 0)
        completed = [j for j in self.ledger.jobs if j["status"] in ("completed", "skipped")]
        self.assertEqual(len(completed), 1)

    def test_d_first_fixed_then_recurring_ten_percent(self):
        """First payment gets the fixed plan bonus (NOT a percentage of the
        200 rupees paid — business_basic's fixed bonus is 100, not 20).
        The second payment for the same referred user is a pure 10% renewal."""
        self.ledger.add_payment("pay_a", self.referred, "business_basic")
        self.ledger.add_payment("pay_b", self.referred, "business_basic")
        with self._patch():
            process_referral_commission(self.referred, 200, "pay_a")
            process_referral_commission(self.referred, 200, "pay_b")
        first = [t for t in self.ledger.txs if t["source"] == FIRST_BONUS_SOURCE]
        rec = [t for t in self.ledger.txs if t["source"] == RECURRING_SOURCE]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["amount"], 100)
        self.assertEqual(first[0]["razorpay_payment_id"], "pay_a")
        # pay_a was the first payment — fixed bonus ONLY, no stacked recurring.
        # pay_b is the (second, different) payment — 10% recurring ONLY.
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["amount"], 20)
        self.assertEqual(rec[0]["razorpay_payment_id"], "pay_b")

    def test_e_commission_failure_leaves_pending_job(self):
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_fail", "pay_fail", self.referred, 100)
        conn.commit()
        with self._patch(), patch.object(rc, "process_referral_commission", side_effect=RuntimeError("boom")):
            out = process_pending_referral_commission_jobs(razorpay_payment_id="pay_fail")
        self.assertEqual(out["failed"], [1])
        self.assertEqual(self.ledger.jobs[0]["status"], "pending")
        self.assertEqual(self.ledger.jobs[0]["attempts"], 1)
        self.assertIn("boom", self.ledger.jobs[0]["last_error"])
        self.assertEqual(self.ledger.txs, [])

    def test_f_retry_after_failure_creates_commission_once(self):
        self.ledger.add_payment("pay_retry", self.referred, "service_provider")
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_retry", "pay_retry", self.referred, 100)
        conn.commit()
        with self._patch(), patch.object(rc, "process_referral_commission", side_effect=RuntimeError("boom")):
            process_pending_referral_commission_jobs(razorpay_payment_id="pay_retry")
        with self._patch():
            process_pending_referral_commission_jobs(razorpay_payment_id="pay_retry")
            process_pending_referral_commission_jobs(razorpay_payment_id="pay_retry")
        self.assertEqual(self._by_source().count(FIRST_BONUS_SOURCE), 1)
        self.assertEqual(self._by_source().count(RECURRING_SOURCE), 0)
        self.assertEqual(self.ledger.jobs[0]["status"], "completed")

    def test_g_duplicate_notification_after_failure_still_processes(self):
        self.ledger.add_payment("pay_dup", self.referred, "service_provider")
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_dup", "pay_dup", self.referred, 100)
        conn.commit()
        with self._patch(), patch.object(rc, "process_referral_commission", side_effect=RuntimeError("boom")):
            after_payment_finalized({"success": True, "duplicate": True, "razorpay_payment_id": "pay_dup"})
        self.assertEqual(self.ledger.jobs[0]["status"], "pending")
        with self._patch():
            after_payment_finalized({"success": True, "duplicate": True, "razorpay_payment_id": "pay_dup"})
        self.assertEqual(self.ledger.jobs[0]["status"], "completed")
        # This referred user's first-ever payment -> exactly one commission
        # row (the fixed bonus), not a stacked bonus+recurring pair.
        self.assertEqual(len(self.ledger.txs), 1)

    def test_j_self_referral_zero_commission(self):
        self.ledger.users[self.referred]["referred_by"] = self.referred
        with self._patch():
            result = process_referral_commission(self.referred, 100, "pay_self")
        self.assertEqual(result["reason"], "self_referral")
        self.assertEqual(self.ledger.txs, [])

    def test_no_referrer(self):
        self.ledger.users[self.referred]["referred_by"] = None
        with self._patch():
            result = process_referral_commission(self.referred, 100, "pay_none")
        self.assertEqual(result["reason"], "no_referrer")
        self.assertEqual(self.ledger.txs, [])

    def test_payout_h_i_concurrent_workers_credit_once(self):
        self.ledger.add_payment("pay_payout", self.referred, "service_provider")
        conn = self.ledger.connect()
        process_referral_commission(self.referred, 100, "pay_payout", conn=conn)
        conn.commit()
        for tx in self.ledger.txs:
            tx["unlock_at"] = datetime.utcnow() - timedelta(hours=2)
        legacy_before = dict(self.ledger.users_wallet_column)
        counts = []
        errors = []

        def run():
            try:
                with self._patch():
                    counts.append(release_locked_referral_payouts())
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(counts), len(self.ledger.txs))
        self.assertTrue(all(tx["status"] == "released" for tx in self.ledger.txs))
        expected = sum(tx["amount"] for tx in self.ledger.txs)
        self.assertEqual(self.ledger.wallet[self.referrer], expected)
        self.assertEqual(self.ledger.users_wallet_column, legacy_before)

    def test_missing_payment_id_leaves_job_pending(self):
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_missing", "pay_missing", self.referred, 100)
        conn.commit()
        self.ledger.jobs[0]["payment_id"] = ""
        self.ledger.jobs[0]["razorpay_payment_id"] = ""
        with self._patch():
            out = process_pending_referral_commission_jobs()
        self.assertEqual(out["failed"], [1])
        self.assertEqual(self.ledger.jobs[0]["status"], "pending")
        self.assertIn("missing_payment_id", self.ledger.jobs[0]["last_error"])
        self.assertEqual(self.ledger.txs, [])

    def test_failed_job_automatic_retry_completes_once(self):
        self.ledger.add_payment("pay_auto", self.referred, "service_provider")
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_auto", "pay_auto", self.referred, 100)
        conn.commit()
        with self._patch(), patch.object(rc, "process_referral_commission", side_effect=RuntimeError("boom")):
            process_pending_referral_commission_jobs()
        self.assertEqual(self.ledger.jobs[0]["status"], "pending")
        with self._patch():
            process_pending_referral_commission_jobs()
            process_pending_referral_commission_jobs()
        self.assertEqual(self.ledger.jobs[0]["status"], "completed")
        self.assertEqual(self._by_source().count(FIRST_BONUS_SOURCE), 1)
        self.assertEqual(self._by_source().count(RECURRING_SOURCE), 0)

    def test_payout_does_not_write_users_wallet_column(self):
        src = (ROOT / "services" / "referral_commission.py").read_text(encoding="utf-8")
        payout = src.split("def release_locked_referral_payouts")[1].split("class _SkipClaim")[0]
        self.assertNotIn("users.wallet_balance", payout)
        self.assertNotIn("UPDATE users SET wallet_balance", payout)
        self.assertIn("INSERT INTO wallet_balance", payout)

    def test_canonical_wallet_readers_do_not_use_users_column(self):
        ref = (ROOT / "services" / "referral_service.py").read_text(encoding="utf-8")
        self.assertIn("COALESCE(wb.balance, 0) AS wallet_balance", ref)
        self.assertNotIn("SELECT referral_code, wallet_balance\n        FROM users", ref)
        admin = (ROOT / "services" / "admin_service.py").read_text(encoding="utf-8")
        self.assertIn("LEFT JOIN wallet_balance wb", admin)
        user_svc = (ROOT / "services" / "user_service.py").read_text(encoding="utf-8")
        self.assertNotIn("UPDATE users SET wallet_balance", user_svc)

    def test_extra_business_and_admin_activation_excluded_from_commission(self):
        user_src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        extra = user_src.split('if plan_type == "extra_business"')[1].split("from services.payment_service")[0]
        self.assertNotIn("after_payment_finalized", extra)
        self.assertNotIn("process_referral_commission", extra)
        from inspect import getsource
        from services.payment_service import activate_subscription, activate_subscription_for_user
        self.assertNotIn("enqueue_referral_commission_job", getsource(activate_subscription))
        self.assertNotIn("enqueue_referral_commission_job", getsource(activate_subscription_for_user))
        self.assertNotIn("process_referral_commission", getsource(activate_subscription))

    def test_internal_retry_route_and_cron_exist(self):
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("/internal/referral-commission-retry", app_src)
        self.assertIn("process_pending_referral_commission_jobs", app_src)
        render = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertIn("landmark-saturday-payout", render)
        self.assertIn("30 12 * * 6", render)
        self.assertIn("/internal/saturday-payout", render)
        self.assertIn("landmark-referral-commission-retry", render)
        self.assertIn("/internal/referral-commission-retry", render)

    def test_legacy_money_paths_disabled(self):
        self.assertIsNone(process_referral(self.referred, 1000))
        self.assertIsNone(process_referral_reward(self.referred, "premium", "pay_x"))
        self.assertFalse(add_pending_referral_reward(self.referred, 1))
        agent = ReferralAgent()
        out = agent.process_referral_reward(self.referred, "premium")
        self.assertFalse(out["success"])
        self.assertEqual(self.ledger.txs, [])
        self.assertEqual(self.ledger.wallet, {})


class FixedFirstBonusModelTests(unittest.TestCase):
    """New commission model: fixed first-sale bonus by plan (₹50 service /
    ₹100 basic / ₹150 premium — never a percentage of amount paid), then
    10% of the actual payment on every eligible renewal thereafter
    (same plan, upgrade, downgrade, or billing-cycle change)."""

    def setUp(self):
        self.ledger = CommissionLedger()
        self.referrer = 1
        self.referred = 2
        self.ledger.add_user(self.referrer)
        self.ledger.add_user(self.referred, referred_by=self.referrer)

    def _patch(self):
        return patch.object(rc, "get_db_connection", side_effect=self.ledger.connect)

    def _first(self):
        return [t for t in self.ledger.txs if t["source"] == FIRST_BONUS_SOURCE]

    def _recurring(self):
        return [t for t in self.ledger.txs if t["source"] == RECURRING_SOURCE]

    def test_service_provider_first_bonus_is_exactly_50(self):
        self.ledger.add_payment("pay_sp", self.referred, "service_provider")
        with self._patch():
            process_referral_commission(self.referred, 499, "pay_sp")
        first = self._first()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["amount"], 50)

    def test_business_basic_first_bonus_is_exactly_100(self):
        self.ledger.add_payment("pay_bb", self.referred, "business_basic")
        with self._patch():
            process_referral_commission(self.referred, 999, "pay_bb")
        first = self._first()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["amount"], 100)

    def test_business_premium_first_bonus_is_exactly_150(self):
        self.ledger.add_payment("pay_bp", self.referred, "business_premium")
        with self._patch():
            process_referral_commission(self.referred, 1499, "pay_bp")
        first = self._first()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["amount"], 150)

    def test_first_bonus_fixed_regardless_of_billing_cycle_amount(self):
        """Same plan (business_basic), three different actual-paid amounts
        simulating monthly / 3-month / yearly discounted totals — the fixed
        ₹100 bonus must not move with the amount."""
        cycles = [("monthly", 999), ("3months", 2697.30), ("yearly", 8391.60)]
        for label, amount in cycles:
            with self.subTest(cycle=label):
                ledger = CommissionLedger()
                ledger.add_user(self.referrer)
                ledger.add_user(self.referred, referred_by=self.referrer)
                pay_id = f"pay_cycle_{label}"
                ledger.add_payment(pay_id, self.referred, "business_basic")
                with patch.object(rc, "get_db_connection", side_effect=ledger.connect):
                    process_referral_commission(self.referred, amount, pay_id)
                first = [t for t in ledger.txs if t["source"] == FIRST_BONUS_SOURCE]
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0]["amount"], 100)

    def test_first_bonus_is_not_a_percentage_of_amount(self):
        """A large one-time payment must not inflate the fixed bonus —
        10% of 5000 would be 500; the fixed service_provider bonus is 50."""
        self.ledger.add_payment("pay_huge", self.referred, "service_provider")
        with self._patch():
            process_referral_commission(self.referred, 5000, "pay_huge")
        first = self._first()
        self.assertEqual(first[0]["amount"], 50)

    def test_renewal_commission_is_exactly_ten_percent(self):
        self.ledger.users[self.referred]["first_sub_commission_paid"] = 1
        self.ledger.add_payment("pay_renew", self.referred, "business_basic")
        with self._patch():
            process_referral_commission(self.referred, 1000, "pay_renew")
        rec = self._recurring()
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["amount"], 100.0)

    def test_renewal_commission_for_current_live_prices(self):
        cases = [(499, 49.9, "service_provider"), (999, 99.9, "business_basic"), (1499, 149.9, "business_premium")]
        for amount, expected, plan in cases:
            with self.subTest(amount=amount):
                ledger = CommissionLedger()
                ledger.add_user(self.referrer)
                ledger.add_user(self.referred, referred_by=self.referrer, first_paid=True)
                pay_id = f"pay_price_{amount}"
                ledger.add_payment(pay_id, self.referred, plan)
                with patch.object(rc, "get_db_connection", side_effect=ledger.connect):
                    process_referral_commission(self.referred, amount, pay_id)
                rec = [t for t in ledger.txs if t["source"] == RECURRING_SOURCE]
                self.assertEqual(len(rec), 1)
                self.assertEqual(rec[0]["amount"], expected)

    def test_renewal_uses_actual_discounted_multimonth_amount(self):
        """business_basic 3-month billed total: 999 * 3 * 0.90 = 2697.30 —
        commission must be 10% of THAT actual amount, not a synthetic
        per-month decomposition."""
        self.ledger.users[self.referred]["first_sub_commission_paid"] = 1
        self.ledger.add_payment("pay_3mo_renew", self.referred, "business_basic")
        with self._patch():
            process_referral_commission(self.referred, 2697.30, "pay_3mo_renew")
        rec = self._recurring()
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["amount"], round(2697.30 * 0.10, 2))

    def test_downgrade_renewal_gets_ten_percent_of_actual_payment(self):
        self.ledger.users[self.referred]["first_sub_commission_paid"] = 1
        self.ledger.add_payment("pay_downgrade", self.referred, "service_provider")
        with self._patch():
            process_referral_commission(self.referred, 499, "pay_downgrade")
        rec = self._recurring()
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["amount"], 49.9)

    def test_upgrade_renewal_gets_ten_percent_of_actual_payment(self):
        self.ledger.users[self.referred]["first_sub_commission_paid"] = 1
        self.ledger.add_payment("pay_upgrade", self.referred, "business_premium")
        with self._patch():
            process_referral_commission(self.referred, 1499, "pay_upgrade")
        rec = self._recurring()
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["amount"], 149.9)

    def test_no_second_first_commission_after_upgrade(self):
        self.ledger.add_payment("pay_first", self.referred, "service_provider")
        self.ledger.add_payment("pay_upgrade_2", self.referred, "business_premium")
        with self._patch():
            process_referral_commission(self.referred, 499, "pay_first")
            process_referral_commission(self.referred, 1499, "pay_upgrade_2")
        first = self._first()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["razorpay_payment_id"], "pay_first")
        rec = self._recurring()
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["razorpay_payment_id"], "pay_upgrade_2")

    def test_no_second_first_commission_after_downgrade(self):
        self.ledger.add_payment("pay_first2", self.referred, "business_premium")
        self.ledger.add_payment("pay_downgrade_2", self.referred, "service_provider")
        with self._patch():
            process_referral_commission(self.referred, 1499, "pay_first2")
            process_referral_commission(self.referred, 499, "pay_downgrade_2")
        first = self._first()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["razorpay_payment_id"], "pay_first2")

    def test_no_second_first_commission_after_billing_cycle_change(self):
        self.ledger.add_payment("pay_first3", self.referred, "business_basic")
        self.ledger.add_payment("pay_cycle_change", self.referred, "business_basic")
        with self._patch():
            process_referral_commission(self.referred, 999, "pay_first3")
            process_referral_commission(self.referred, 2697.30, "pay_cycle_change")
        first = self._first()
        self.assertEqual(len(first), 1)

    def test_unknown_plan_does_not_pay_a_guessed_first_bonus(self):
        """An unrecognized plan key must never fall back to a percentage or
        a guessed fixed amount. The job must stay pending (retryable, with
        diagnosable last_error), and the one-time first-bonus flag must NOT
        be burned by the failed attempt."""
        self.ledger.add_payment("pay_unknown", self.referred, "mystery_plan")
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_unknown", "pay_unknown", self.referred, 777)
        conn.commit()
        with self._patch():
            out = process_pending_referral_commission_jobs(razorpay_payment_id="pay_unknown")
        self.assertEqual(out["failed"], [1])
        self.assertEqual(self.ledger.jobs[0]["status"], "pending")
        self.assertIn("Unrecognized plan", self.ledger.jobs[0]["last_error"])
        self.assertEqual(self.ledger.txs, [])
        self.assertEqual(self.ledger.users[self.referred]["first_sub_commission_paid"], 0)

    def test_unrecognized_plan_does_not_crash_the_dispatcher(self):
        """A second, independent eligible job must still process normally
        even though an earlier job hit an unknown plan."""
        self.ledger.add_payment("pay_bad", self.referred, "not_a_real_plan")
        self.ledger.add_payment("pay_good", self.referred, "service_provider")
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_bad", "pay_bad", self.referred, 100)
        conn.commit()
        with self._patch():
            process_pending_referral_commission_jobs(razorpay_payment_id="pay_bad")
        conn2 = self.ledger.connect()
        enqueue_referral_commission_job(conn2, "pay_good", "pay_good", self.referred, 100)
        conn2.commit()
        with self._patch():
            out = process_pending_referral_commission_jobs(razorpay_payment_id="pay_good")
        self.assertEqual(out["processed"], [2])
        good_job = next(j for j in self.ledger.jobs if j["payment_id"] == "pay_good")
        self.assertEqual(good_job["status"], "completed")
        self.assertEqual(self._first()[0]["razorpay_payment_id"], "pay_good")

    def test_payment_not_activated_refuses_first_bonus(self):
        self.ledger.add_payment("pay_not_active", self.referred, "service_provider", status="created")
        with self._patch():
            with self.assertRaises(rc.CommissionPlanLookupError):
                process_referral_commission(self.referred, 499, "pay_not_active")
        self.assertEqual(self.ledger.txs, [])

    def test_missing_payment_row_refuses_first_bonus(self):
        """No add_payment() call at all — simulates the job racing ahead of
        a committed payments row, or a data-integrity gap. Must refuse, not
        guess."""
        with self._patch():
            with self.assertRaises(rc.CommissionPlanLookupError):
                process_referral_commission(self.referred, 499, "pay_never_recorded")
        self.assertEqual(self.ledger.txs, [])

    def test_first_bonus_lookup_scoped_to_the_referred_users_own_payment(self):
        """The (payment_id, user_id) lookup must never attribute a payment
        row belonging to a different user."""
        other_user = 99
        self.ledger.add_payment("pay_other_user", other_user, "business_premium")
        with self._patch():
            with self.assertRaises(rc.CommissionPlanLookupError):
                process_referral_commission(self.referred, 499, "pay_other_user")
        self.assertEqual(self.ledger.txs, [])

    # ---- Fix 1: recurring commission requires an activated payment ----

    def test_recurring_commission_refuses_non_activated_payment(self):
        """The renewal (recurring) branch must enforce the exact same
        activation requirement as the first-sale bonus branch — a payment
        that is not in the repository's activated state must never
        produce a commission, must not touch the wallet, and must not
        change first_sub_commission_paid (already 1 here, from an earlier
        eligible payment)."""
        self.ledger.users[self.referred]["first_sub_commission_paid"] = 1
        self.ledger.add_payment("pay_renew_not_active", self.referred, "business_basic", status="created")
        with self._patch():
            with self.assertRaises(rc.CommissionPlanLookupError):
                process_referral_commission(self.referred, 999, "pay_renew_not_active")
        self.assertEqual(self.ledger.txs, [])
        self.assertEqual(self.ledger.users[self.referred]["first_sub_commission_paid"], 1)

    def test_recurring_commission_refuses_missing_payment_row(self):
        """No payments row at all for this (payment_id, user_id) — must
        refuse rather than blindly trust the caller-supplied amount."""
        self.ledger.users[self.referred]["first_sub_commission_paid"] = 1
        with self._patch():
            with self.assertRaises(rc.CommissionPlanLookupError):
                process_referral_commission(self.referred, 999, "pay_renew_never_recorded")
        self.assertEqual(self.ledger.txs, [])

    # ---- Fix 2: idempotency guard is scoped to the referred subscriber ----

    def test_idempotency_guard_is_scoped_to_the_referred_user_not_just_payment_id(self):
        """A DIFFERENT referred user's own commission row that happens to
        share this razorpay_payment_id string must never cause the
        current user's own legitimate commission to be wrongly skipped as
        'already_processed'. Uses RECURRING_SOURCE for the seeded other-
        user row (while this user's own eligible commission is the FIRST
        bonus) specifically so this scenario does not collide with the
        pre-existing (source, razorpay_payment_id) unique index — this
        test proves the guard's own reference_id scoping, not just the
        database constraint."""
        other_user = 77
        self.ledger.add_user(other_user, referred_by=self.referrer, first_paid=True)
        conn = self.ledger.connect()
        conn.execute(
            """
            INSERT INTO wallet_transactions
                (user_id, amount, type, source, reference_id, status, unlock_at,
                 created_at, razorpay_payment_id)
            VALUES (:referrer_id, :amount, 'credit', :source, :ref_id, 'locked',
                    :unlock_at, CURRENT_TIMESTAMP, :rzp)
            """,
            {
                "referrer_id": self.referrer,
                "amount": 20.0,
                "source": RECURRING_SOURCE,
                "ref_id": f"user_{other_user}",
                "unlock_at": "2020-01-01 00:00:00",
                "rzp": "pay_shared_id",
            },
        )
        conn.commit()

        self.ledger.add_payment("pay_shared_id", self.referred, "service_provider")
        with self._patch():
            result = process_referral_commission(self.referred, 499, "pay_shared_id")
        self.assertEqual(result["reason"], "ok")
        mine = [
            t for t in self.ledger.txs
            if t["source"] == FIRST_BONUS_SOURCE and t["reference_id"] == f"user_{self.referred}"
        ]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["amount"], 50)

    def test_same_payment_retry_still_blocked_by_scoped_guard(self):
        """Retains the same-payment-retry protection (now user-scoped):
        retrying the identical payment for the SAME referred user must
        still be blocked — this is the case the guard exists for."""
        self.ledger.add_payment("pay_retry_scoped", self.referred, "service_provider")
        with self._patch():
            first_result = process_referral_commission(self.referred, 499, "pay_retry_scoped")
            second_result = process_referral_commission(self.referred, 499, "pay_retry_scoped")
        self.assertEqual(first_result["reason"], "ok")
        self.assertEqual(second_result["reason"], "already_processed")
        self.assertEqual(len(self.ledger.txs), 1)


class OrderIdUniquenessTests(unittest.TestCase):
    def test_l_duplicate_non_null_order_id_rejected(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id TEXT, payment_id TEXT)")
        db.execute(
            "CREATE UNIQUE INDEX uq_payments_order_id_not_null "
            "ON payments(order_id) WHERE order_id IS NOT NULL"
        )
        db.execute("INSERT INTO payments (order_id, payment_id) VALUES ('order_1', 'pay_a')")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO payments (order_id, payment_id) VALUES ('order_1', 'pay_b')")
        db.execute("INSERT INTO payments (order_id, payment_id) VALUES (NULL, 'pay_c')")
        db.execute("INSERT INTO payments (order_id, payment_id) VALUES (NULL, 'pay_d')")

    def test_migration_stops_when_duplicate_order_ids_exist(self):
        from migrations import add_referral_commission_money_safety as mig

        dup = FakeRow({"order_id": "order_dup", "n": 2, "ids": [1, 2]})
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [dup]
        with patch.object(mig, "get_db_connection", return_value=conn):
            with self.assertRaises(RuntimeError) as ctx:
                mig.migrate_referral_commission_money_safety()
        self.assertIn("STOP", str(ctx.exception))
        self.assertIn("order_dup", str(ctx.exception))

    def test_migration_sql_is_additive(self):
        src = (ROOT / "migrations" / "add_referral_commission_money_safety.py").read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS razorpay_payment_id", src)
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_first_bonus_reference", src)
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_source_razorpay_payment", src)
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_order_id_not_null", src)
        self.assertIn("CREATE TABLE IF NOT EXISTS referral_commission_jobs", src)
        body = src.split("def migrate_referral_commission_money_safety", 1)[1]
        self.assertNotIn("DROP TABLE", body)
        self.assertNotIn("TRUNCATE", body)
        self.assertNotIn("DELETE FROM", body)

    def test_canonical_wallet_documented(self):
        src = (ROOT / "services" / "referral_commission.py").read_text(encoding="utf-8")
        self.assertIn("wallet_balance.balance", src)
        self.assertIn("users.wallet_balance", src)
        self.assertIn("CANONICAL_WALLET", src)
        init_db = (ROOT / "database" / "init_db.py").read_text(encoding="utf-8")
        self.assertIn("referral_commission_jobs", init_db)
        self.assertIn("razorpay_payment_id", init_db)
        migration = (ROOT / "migrations" / "add_referral_commission_money_safety.py").read_text(encoding="utf-8")
        self.assertIn("uq_payments_order_id_not_null", migration)

    def test_legacy_user_verify_routes_through_commission(self):
        user_src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        pay_src = (ROOT / "routes" / "payment_routes.py").read_text(encoding="utf-8")
        self.assertIn("after_payment_finalized", user_src)
        self.assertIn("after_payment_finalized", pay_src)
        self.assertNotIn("if result.get(\"duplicate\"):\n        return", pay_src.replace("'", '"'))
        self.assertIn("duplicate", pay_src)


class FinalizeEnqueuesJobTests(unittest.TestCase):
    def test_finalize_enqueues_job_on_duplicate(self):
        from tests.test_payment_subscription import LockedPaymentDB
        from services.payment_service import finalize_paid_order
        from config.payment_config import get_plan_spec

        store = LockedPaymentDB(
            {
                "id": 9, "user_id": 42, "order_id": "order_1",
                "payment_id": "pay_1", "amount": 99900, "status": "activated",
                "plan": "business_basic",
            },
            {"plan": "business_basic", "role": "business_basic", "subscription_expiry": "2026-09-01"},
        )
        _, spec = get_plan_spec("business_basic")
        with patch("services.payment_service.get_db_connection", side_effect=store.connect), \
             patch("services.payment_service.ensure_referral_commission_schema"):
            result = finalize_paid_order("order_1", "pay_1", spec, 99900, user_id=42)
        self.assertTrue(result["success"])
        self.assertTrue(result.get("duplicate"))
        self.assertEqual(result.get("razorpay_payment_id"), "pay_1")
        self.assertEqual(len(store.jobs), 1)
        self.assertEqual(store.jobs[0]["payment_id"], "pay_1")
        self.assertEqual(store.jobs[0]["amount_rupees"], 999.0)


if __name__ == "__main__":
    unittest.main()
