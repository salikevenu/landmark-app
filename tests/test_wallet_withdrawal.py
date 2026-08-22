"""Stage 3 wallet, payout, and withdrawal safety tests."""
import os
import sys
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from services.wallet_service import (
    WITHDRAW_DEBIT_SOURCE,
    WITHDRAW_REFUND_SOURCE,
    approve_withdrawal,
    credit_wallet,
    debit_wallet,
    get_wallet_balance,
    mark_withdrawal_paid,
    parse_money,
    reject_withdrawal,
    request_withdrawal,
)
from services.wallet_service import process_referral, add_pending_referral_reward


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
        return list(self._rows)


class WalletStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.balances = {}
        self.withdrawals = []
        self.txs = []
        self.business_refs = {}
        self.settings = {
            "withdrawal_min_amount": "100",
            "withdrawal_max_amount": "50000",
        }
        self.next_wid = 1
        self.next_tid = 1

    def connect(self):
        return WalletConn(self)


class WalletConn:
    def __init__(self, store):
        self.store = store
        self._locked = False

    def execute(self, query, params=None):
        q = _sql(query)
        params = params or {}
        with self.store.lock:
            return self._execute(q, params)

    def _execute(self, q, params):
        if "from admin_settings" in q:
            rows = [
                FakeRow({"key": k, "value": v})
                for k, v in self.store.settings.items()
            ]
            return FakeResult(rows=rows)

        if "from withdraw_requests" in q and "approved" in q:
            uid = int(params["uid"])
            for w in self.store.withdrawals:
                if w["user_id"] == uid and w.get("status") in ("approved", "paid"):
                    return FakeResult(FakeRow({"id": w["id"]}))
            return FakeResult(None)

        if "from users" in q and "referred_by" in q:
            uid = int(params["uid"])
            n = int(self.store.business_refs.get(uid, 0))
            return FakeResult(FakeRow({"cnt": n}))

        if "from withdraw_requests" in q and "reference_id" in q and "select" in q:
            uid = int(params["uid"])
            ref = params.get("ref")
            for w in reversed(self.store.withdrawals):
                if w["user_id"] == uid and w.get("reference_id") == ref:
                    return FakeResult(FakeRow(dict(w)))
            return FakeResult(None)

        if "insert into wallet_balance" in q:
            uid = int(params["uid"])
            if uid not in self.store.balances:
                self.store.balances[uid] = Decimal("0.00")
            return FakeResult(rowcount=1)

        if "select balance from wallet_balance" in q:
            uid = int(params["uid"])
            if "for update" in q:
                self._locked = True
            bal = self.store.balances.get(uid, Decimal("0.00"))
            return FakeResult(FakeRow({"balance": bal}))

        if "update wallet_balance" in q and "balance -" in q:
            uid = int(params["uid"])
            amt = Decimal(str(params["amount"]))
            bal = self.store.balances.get(uid, Decimal("0.00"))
            if bal >= amt:
                self.store.balances[uid] = bal - amt
                return FakeResult(rowcount=1)
            return FakeResult(rowcount=0)

        if "update wallet_balance" in q and "balance +" in q:
            uid = int(params["uid"])
            amt = Decimal(str(params["amount"]))
            self.store.balances[uid] = self.store.balances.get(uid, Decimal("0.00")) + amt
            return FakeResult(rowcount=1)

        if "insert into withdraw_requests" in q:
            uid = int(params["uid"])
            ref = params.get("ref")
            if ref:
                for w in self.store.withdrawals:
                    if w["user_id"] == uid and w.get("reference_id") == ref:
                        from sqlalchemy.exc import IntegrityError
                        raise IntegrityError("dup", {}, Exception("dup"))
            wid = self.store.next_wid
            self.store.next_wid += 1
            row = {
                "id": wid,
                "user_id": uid,
                "amount": Decimal(str(params["amount"])),
                "status": params["status"],
                "upi_id": params.get("upi"),
                "reference_id": ref,
            }
            self.store.withdrawals.append(row)
            return FakeResult(FakeRow({"id": wid}), rowcount=1)

        if "insert into wallet_transactions" in q:
            ref = params.get("ref_id")
            src = params.get("source")
            if ref:
                for tx in self.store.txs:
                    if tx.get("reference_id") == ref and tx.get("source") == src:
                        from sqlalchemy.exc import IntegrityError
                        raise IntegrityError("dup", {}, Exception("dup"))
            tid = self.store.next_tid
            self.store.next_tid += 1
            tx_type = "debit" if "'debit'" in q else "credit"
            self.store.txs.append({
                "id": tid,
                "user_id": int(params["uid"]),
                "amount": Decimal(str(params["amount"])),
                "type": tx_type,
                "source": src,
                "reference_id": ref,
                "status": "completed",
            })
            return FakeResult(rowcount=1)

        if "update withdraw_requests" in q:
            wid = int(params["wid"])
            from_status = params["from_status"]
            to_status = params["to_status"]
            for w in self.store.withdrawals:
                if w["id"] == wid and w["status"] == from_status:
                    w["status"] = to_status
                    return FakeResult(FakeRow(dict(w)), rowcount=1)
            return FakeResult(None, rowcount=0)

        if "from wallet_transactions" in q and "reference_id" in q:
            for tx in self.store.txs:
                if tx.get("reference_id") == params.get("ref") and tx.get("source") == params.get("src"):
                    return FakeResult(FakeRow({"id": tx["id"]}))
            return FakeResult(None)

        return FakeResult(None)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def begin_nested(self):
        class _N:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _N()


class ParseMoneyTests(unittest.TestCase):
    def test_rejects_invalid(self):
        for bad in (0, -1, "NaN", "Infinity", "1e6", "", None, True, 99.999):
            with self.assertRaises(ValueError):
                parse_money(bad)

    def test_accepts_rupees(self):
        self.assertEqual(parse_money("100"), Decimal("100.00"))
        self.assertEqual(parse_money("100.50"), Decimal("100.50"))
        self.assertEqual(parse_money("100.01"), Decimal("100.01"))


class WithdrawalSafetyTests(unittest.TestCase):
    def setUp(self):
        self.store = WalletStore()
        self.store.balances[1] = Decimal("150.00")
        # Subsequent-withdrawal tests: first withdrawal already completed.
        self.store.withdrawals.append({
            "id": 0, "user_id": 1, "amount": Decimal("500.00"),
            "status": "paid", "upi_id": "done@upi", "reference_id": None,
        })
        self.patcher = patch("services.wallet_service.get_db_connection", side_effect=self.store.connect)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_below_100_rejected(self):
        for amt in (0, 1, "99.99"):
            out = request_withdrawal(1, amt, "user@upi")
            self.assertFalse(out["success"], amt)
        self.assertEqual(self.store.balances[1], Decimal("150.00"))
        pending = [w for w in self.store.withdrawals if w["status"] == "pending"]
        self.assertEqual(pending, [])

    def test_exactly_100(self):
        out = request_withdrawal(1, 100, "user@upi")
        self.assertTrue(out["success"])
        self.assertEqual(self.store.balances[1], Decimal("50.00"))
        self.assertEqual(self.store.withdrawals[-1]["status"], "pending")

    def test_decimal_amount(self):
        self.store.balances[1] = Decimal("200.00")
        out = request_withdrawal(1, "100.50", "user@upi")
        self.assertTrue(out["success"])
        self.assertEqual(self.store.balances[1], Decimal("99.50"))

    def test_insufficient_and_huge(self):
        self.assertFalse(request_withdrawal(1, 151, "user@upi")["success"])
        self.assertFalse(request_withdrawal(1, 999999, "user@upi")["success"])
        self.assertEqual(self.store.balances[1], Decimal("150.00"))

    def test_negative_amount(self):
        self.assertFalse(request_withdrawal(1, -100, "user@upi")["success"])

    def test_concurrent_withdrawals_one_succeeds(self):
        results = [None, None]

        def run(idx):
            results[idx] = request_withdrawal(1, 100, "user@upi")

        t1 = threading.Thread(target=run, args=(0,))
        t2 = threading.Thread(target=run, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        ok = [r for r in results if r and r.get("success")]
        bad = [r for r in results if r and not r.get("success")]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(bad), 1)
        self.assertEqual(self.store.balances[1], Decimal("50.00"))
        self.assertGreaterEqual(self.store.balances[1], 0)
        self.assertEqual(len(self.store.withdrawals), 2)

    def test_duplicate_idempotency_key(self):
        a = request_withdrawal(1, 100, "user@upi", idempotency_key="idem-1")
        b = request_withdrawal(1, 100, "user@upi", idempotency_key="idem-1")
        self.assertTrue(a["success"])
        self.assertTrue(b["success"])
        self.assertTrue(b.get("duplicate"))
        self.assertEqual(len(self.store.withdrawals), 2)
        self.assertEqual(self.store.balances[1], Decimal("50.00"))

    def test_duplicate_admin_approval(self):
        request_withdrawal(1, 100, "user@upi")
        wid = self.store.withdrawals[-1]["id"]
        first = approve_withdrawal(wid)
        second = approve_withdrawal(wid)
        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertEqual(self.store.withdrawals[-1]["status"], "approved")
        self.assertEqual(self.store.balances[1], Decimal("50.00"))

    def test_rejection_refunds_once(self):
        request_withdrawal(1, 100, "user@upi")
        wid = self.store.withdrawals[-1]["id"]
        first = reject_withdrawal(wid)
        second = reject_withdrawal(wid)
        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertEqual(self.store.balances[1], Decimal("150.00"))
        refunds = [t for t in self.store.txs if t["source"] == WITHDRAW_REFUND_SOURCE]
        self.assertEqual(len(refunds), 1)

    def test_paid_cannot_reject(self):
        request_withdrawal(1, 100, "user@upi")
        wid = self.store.withdrawals[-1]["id"]
        approve_withdrawal(wid)
        mark_withdrawal_paid(wid)
        self.assertFalse(reject_withdrawal(wid)["success"])
        self.assertEqual(self.store.withdrawals[-1]["status"], "paid")
        self.assertEqual(self.store.balances[1], Decimal("50.00"))

    def test_retry_after_rejection(self):
        request_withdrawal(1, 100, "user@upi")
        reject_withdrawal(self.store.withdrawals[-1]["id"])
        again = request_withdrawal(1, 100, "user@upi")
        self.assertTrue(again["success"])
        self.assertEqual(self.store.balances[1], Decimal("50.00"))

    def test_locked_commission_not_spendable(self):
        # Spendable is wallet_balance only; locked ledger is ignored.
        self.store.txs.append({
            "id": 99, "user_id": 1, "amount": Decimal("500"),
            "type": "credit", "source": "referral_recurring",
            "status": "locked", "reference_id": None,
        })
        self.assertEqual(get_wallet_balance(1), Decimal("150.00"))
        self.assertFalse(request_withdrawal(1, 200, "user@upi")["success"])

    def test_user_isolation(self):
        self.store.balances[2] = Decimal("500.00")
        out = request_withdrawal(1, 100, "user@upi")
        self.assertTrue(out["success"])
        self.assertEqual(self.store.balances[2], Decimal("500.00"))
        self.assertEqual(self.store.withdrawals[-1]["user_id"], 1)

    def test_debit_cannot_go_negative(self):
        self.assertFalse(debit_wallet(1, 151, source="promo"))
        self.assertEqual(self.store.balances[1], Decimal("150.00"))

    def test_legacy_paths_disabled(self):
        self.assertIsNone(process_referral(1, 1000))
        self.assertFalse(add_pending_referral_reward(1, 1))
        self.assertEqual(self.store.txs, [])
        self.assertEqual(self.store.balances[1], Decimal("150.00"))

    def test_canonical_balance_only(self):
        src = (ROOT / "services" / "wallet_service.py").read_text(encoding="utf-8")
        self.assertIn("wallet_balance.balance", src)
        self.assertNotIn("UPDATE users SET wallet_balance", src)
        self.assertIn("First withdrawal requires minimum ₹500 balance", src)
        withdraw_src = (ROOT / "routes" / "wallet_routes.py").read_text(encoding="utf-8")
        self.assertIn("request_withdrawal", withdraw_src)
        self.assertNotIn("Minimum withdraw ₹500", withdraw_src)
        admin = (ROOT / "services" / "admin_service.py").read_text(encoding="utf-8")
        self.assertIn("approve_withdrawal", admin)
        self.assertNotIn("debit_wallet(row[0]", admin)


class FirstWithdrawalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.store = WalletStore()
        self.patcher = patch("services.wallet_service.get_db_connection", side_effect=self.store.connect)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_first_withdrawal_requires_500_balance(self):
        self.store.balances[1] = Decimal("499.00")
        self.store.business_refs[1] = 1
        out = request_withdrawal(1, 100, "user@upi")
        self.assertFalse(out["success"])
        self.assertIn("₹500", out["error"])
        self.assertEqual(self.store.balances[1], Decimal("499.00"))
        self.assertEqual(self.store.withdrawals, [])

    def test_first_withdrawal_requires_paid_business_referral(self):
        self.store.balances[1] = Decimal("500.00")
        self.store.business_refs[1] = 0
        out = request_withdrawal(1, 100, "user@upi")
        self.assertFalse(out["success"])
        self.assertIn("paid business", out["error"].lower())
        self.assertEqual(self.store.balances[1], Decimal("500.00"))

    def test_first_withdrawal_succeeds_with_500_and_referral(self):
        self.store.balances[1] = Decimal("500.00")
        self.store.business_refs[1] = 1
        out = request_withdrawal(1, 100, "user@upi")
        self.assertTrue(out["success"])
        self.assertEqual(self.store.balances[1], Decimal("400.00"))

    def test_after_first_100_with_150_ok(self):
        self.store.balances[1] = Decimal("150.00")
        self.store.withdrawals.append({
            "id": 0, "user_id": 1, "amount": Decimal("500"), "status": "paid",
            "upi_id": "x", "reference_id": None,
        })
        out = request_withdrawal(1, 100, "user@upi")
        self.assertTrue(out["success"])
        self.assertEqual(self.store.balances[1], Decimal("50.00"))


class WithdrawalRouteAuthTests(unittest.TestCase):
    def setUp(self):
        from routes.wallet_routes import wallet_bp
        self.app = Flask(__name__)
        self.app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
        self.app.config["JWT_TOKEN_LOCATION"] = ["headers"]
        JWTManager(self.app)
        self.app.register_blueprint(wallet_bp)
        self.client = self.app.test_client()

    def test_unauthenticated_withdraw_401(self):
        res = self.client.post("/api/withdraw", json={"amount": 100, "upi_id": "a@upi"})
        self.assertEqual(res.status_code, 401)

    def test_client_user_id_ignored(self):
        store = WalletStore()
        store.balances[7] = Decimal("200.00")
        store.balances[99] = Decimal("200.00")
        store.withdrawals.append({
            "id": 0, "user_id": 7, "amount": Decimal("500"), "status": "paid",
            "upi_id": "x", "reference_id": None,
        })
        with self.app.app_context():
            token = create_access_token(identity="7")
        with patch("services.wallet_service.get_db_connection", side_effect=store.connect):
            res = self.client.post(
                "/api/withdraw",
                json={"amount": 100, "upi_id": "a@upi", "user_id": 99},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(store.balances[7], Decimal("100.00"))
        self.assertEqual(store.balances[99], Decimal("200.00"))

    def test_min_100_via_http(self):
        store = WalletStore()
        store.balances[7] = Decimal("200.00")
        with self.app.app_context():
            token = create_access_token(identity="7")
        with patch("services.wallet_service.get_db_connection", side_effect=store.connect):
            res = self.client.post(
                "/api/withdraw",
                json={"amount": 99.99, "upi_id": "a@upi"},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(store.withdrawals, [])


class PayoutReleasedOnceTests(unittest.TestCase):
    def test_released_cannot_release_again_source(self):
        src = (ROOT / "services" / "referral_commission.py").read_text(encoding="utf-8")
        self.assertIn("FOR UPDATE SKIP LOCKED", src)
        self.assertIn("WHERE id = :tid AND status = 'locked'", src)
        self.assertIn("wallet_balance.balance", src)

    def test_migration_not_destructive(self):
        src = (ROOT / "migrations" / "add_wallet_withdrawal_safety.py").read_text(encoding="utf-8")
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS uq_withdraw_requests_user_reference", src)
        self.assertNotIn("DROP TABLE", src)
        self.assertNotIn("DELETE FROM", src)


if __name__ == "__main__":
    unittest.main()
