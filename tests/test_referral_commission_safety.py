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

    def add_user(self, uid, referred_by=None, first_paid=False, wallet_col=0):
        self.users[uid] = {
            "id": uid,
            "referred_by": referred_by,
            "first_sub_commission_paid": 1 if first_paid else 0,
        }
        self.users_wallet_column[uid] = wallet_col
        self.user_locks[uid] = threading.Lock()
        return self.users[uid]

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

        if "update users set first_sub_commission_paid" in q:
            uid = params["uid"]
            if uid in self.store.users:
                self.store.users[uid]["first_sub_commission_paid"] = 1
            return FakeResult(rowcount=1)

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
        self.assertEqual(firsts[0]["amount"], 10)
        recurrings = [t for t in self.ledger.txs if t["source"] == RECURRING_SOURCE]
        self.assertEqual(len(recurrings), 1)

    def test_a_two_payments_still_one_first_bonus(self):
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
        recurrings = [t for t in self.ledger.txs if t["source"] == RECURRING_SOURCE]
        self.assertEqual(len(recurrings), 2)

    def test_b_same_payment_retry(self):
        with self._patch():
            process_referral_commission(self.referred, 499, "pay_1")
            process_referral_commission(self.referred, 499, "pay_1")
        self.assertEqual(self._by_source().count(FIRST_BONUS_SOURCE), 1)
        self.assertEqual(self._by_source().count(RECURRING_SOURCE), 1)
        self.assertEqual({t["razorpay_payment_id"] for t in self.ledger.txs}, {"pay_1"})

    def test_c_verify_webhook_race(self):
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
        self.assertEqual(self._by_source().count(RECURRING_SOURCE), 1)
        completed = [j for j in self.ledger.jobs if j["status"] in ("completed", "skipped")]
        self.assertEqual(len(completed), 1)

    def test_d_two_different_payments_10_then_5(self):
        with self._patch():
            process_referral_commission(self.referred, 200, "pay_a")
            process_referral_commission(self.referred, 200, "pay_b")
        first = [t for t in self.ledger.txs if t["source"] == FIRST_BONUS_SOURCE]
        rec = [t for t in self.ledger.txs if t["source"] == RECURRING_SOURCE]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["amount"], 20)
        self.assertEqual(first[0]["razorpay_payment_id"], "pay_a")
        self.assertEqual(len(rec), 2)
        self.assertEqual(sorted(t["amount"] for t in rec), [10, 10])
        self.assertEqual(sorted(t["razorpay_payment_id"] for t in rec), ["pay_a", "pay_b"])

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
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_retry", "pay_retry", self.referred, 100)
        conn.commit()
        with self._patch(), patch.object(rc, "process_referral_commission", side_effect=RuntimeError("boom")):
            process_pending_referral_commission_jobs(razorpay_payment_id="pay_retry")
        with self._patch():
            process_pending_referral_commission_jobs(razorpay_payment_id="pay_retry")
            process_pending_referral_commission_jobs(razorpay_payment_id="pay_retry")
        self.assertEqual(self._by_source().count(FIRST_BONUS_SOURCE), 1)
        self.assertEqual(self._by_source().count(RECURRING_SOURCE), 1)
        self.assertEqual(self.ledger.jobs[0]["status"], "completed")

    def test_g_duplicate_notification_after_failure_still_processes(self):
        conn = self.ledger.connect()
        enqueue_referral_commission_job(conn, "pay_dup", "pay_dup", self.referred, 100)
        conn.commit()
        with self._patch(), patch.object(rc, "process_referral_commission", side_effect=RuntimeError("boom")):
            after_payment_finalized({"success": True, "duplicate": True, "razorpay_payment_id": "pay_dup"})
        self.assertEqual(self.ledger.jobs[0]["status"], "pending")
        with self._patch():
            after_payment_finalized({"success": True, "duplicate": True, "razorpay_payment_id": "pay_dup"})
        self.assertEqual(self.ledger.jobs[0]["status"], "completed")
        self.assertEqual(len(self.ledger.txs), 2)

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
        self.assertEqual(self._by_source().count(RECURRING_SOURCE), 1)

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
