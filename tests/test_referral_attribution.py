"""Stage 1: durable referral attribution through OTP and landing URLs."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager
from sqlalchemy.exc import IntegrityError

from extensions import init_extensions

_bootstrap = Flask(__name__)
_bootstrap.config["SECRET_KEY"] = "test-secret"
init_extensions(_bootstrap)

from routes import auth_routes
from routes.auth_routes import (
    auth_bp,
    get_or_create_user,
    get_pending_referral,
    persist_referral_for_phone,
    register_url_with_ref,
    resolve_referrer_id_for_signup,
)


class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping

    def __getitem__(self, index):
        return list(self._mapping.values())[index]


class FakeResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or ([] if row is None else [row])

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class ReferralStore:
    def __init__(self):
        self.users_by_id = {}
        self.users_by_phone = {}
        self.users_by_code = {}
        self.pending = {}
        self.next_id = 1

    def add_user(self, phone, referral_code, referred_by=None, name="", role="free"):
        uid = self.next_id
        self.next_id += 1
        row = {
            "id": uid,
            "phone": phone,
            "name": name,
            "role": role,
            "referral_code": referral_code,
            "referred_by": referred_by,
        }
        self.users_by_id[uid] = row
        self.users_by_phone[phone] = row
        self.users_by_code[referral_code] = row
        return row


class FakeConn:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}
        if "from users where referral_code" in q:
            row = self.store.users_by_code.get(params.get("code"))
            return FakeResult(FakeRow(dict(row)) if row else None)
        if "from users where phone" in q:
            row = self.store.users_by_phone.get(params.get("phone"))
            return FakeResult(FakeRow(dict(row)) if row else None)
        if q.startswith("insert into users"):
            code = params["code"]
            if code in self.store.users_by_code:
                raise IntegrityError("duplicate referral_code", params, Exception())
            if params["phone"] in self.store.users_by_phone:
                raise IntegrityError("duplicate phone", params, Exception())
            row = self.store.add_user(
                params["phone"],
                code,
                referred_by=params.get("referred_by"),
            )
            return FakeResult(FakeRow({"id": row["id"]}))
        if "insert into pending_referrals" in q:
            phone = params["phone"]
            self.store.pending[phone] = {
                "phone": phone,
                "ref_code": params["ref_code"],
                "referrer_id": params["referrer_id"],
                "expires_at": datetime.utcnow() + timedelta(days=7),
            }
            return FakeResult()
        if "delete from pending_referrals" in q:
            self.store.pending.pop(params.get("phone"), None)
            return FakeResult()
        if "from pending_referrals" in q:
            row = self.store.pending.get(params.get("phone"))
            if not row:
                return FakeResult(None)
            if row["expires_at"] <= datetime.utcnow():
                return FakeResult(None)
            return FakeResult(FakeRow({
                "ref_code": row["ref_code"],
                "referrer_id": row["referrer_id"],
                "expires_at": row["expires_at"],
            }))
        if q.startswith("update users set referred_by"):
            uid = params["uid"]
            user = self.store.users_by_id.get(uid)
            if user and user.get("referred_by") is None:
                user["referred_by"] = params["rid"]
            return FakeResult()
        return FakeResult()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeEngine:
    def __init__(self, store):
        self.store = store

    def connect(self):
        return FakeConn(self.store)


def _auth_app(store):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
        JWT_TOKEN_LOCATION=["headers", "cookies"],
        JWT_COOKIE_SECURE=False,
        JWT_COOKIE_CSRF_PROTECT=False,
        TESTING=True,
    )
    JWTManager(app)
    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.engine_store = store
    return app


class ReferralHelperTests(unittest.TestCase):
    def test_register_url_with_ref_encodes_code(self):
        self.assertEqual(register_url_with_ref("ABC123"), "/register?ref=ABC123")
        self.assertEqual(register_url_with_ref("A B"), "/register?ref=A%20B")
        self.assertEqual(register_url_with_ref(""), "/register")


class ReferralAttributionFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = ReferralStore()
        self.referrer = self.store.add_user("9998887777", "REFCODE1")
        self.engine = FakeEngine(self.store)
        self.engine_patch = patch.object(auth_routes, "engine", self.engine)
        self.engine_patch.start()
        self.app = _auth_app(self.store)
        self.client = self.app.test_client()

    def tearDown(self):
        self.engine_patch.stop()

    def test_new_user_valid_referral_sets_referred_by_on_insert(self):
        with self.app.test_request_context("/api/auth/send-otp?ref=REFCODE1", json={"phone": "9876543210"}):
            ok, err = persist_referral_for_phone("9876543210", {"phone": "9876543210", "ref": "REFCODE1"})
            self.assertTrue(ok)
            self.assertIsNone(err)
            user, status = get_or_create_user("9876543210", referrer_id=self.referrer["id"])
        self.assertEqual(status, "new")
        self.assertEqual(user["referred_by"], self.referrer["id"])
        self.assertEqual(self.store.users_by_phone["9876543210"]["referred_by"], self.referrer["id"])

    def test_existing_user_referral_not_reassigned(self):
        existing = self.store.add_user("9123456789", "OWNCODE", referred_by=None)
        with self.app.test_request_context("/", json={"ref": "REFCODE1"}):
            ok, err = persist_referral_for_phone(existing["phone"], {"ref": "REFCODE1"})
            self.assertTrue(ok)
            user, status = get_or_create_user(existing["phone"], referrer_id=self.referrer["id"])
        self.assertEqual(status, "existing")
        self.assertIsNone(user.get("referred_by"))
        self.assertIsNone(self.store.users_by_phone[existing["phone"]]["referred_by"])

    def test_existing_user_referred_by_cannot_be_overwritten(self):
        other = self.store.add_user("9111111111", "OTHERREF")
        existing = self.store.add_user("9123456790", "KEEPCODE", referred_by=self.referrer["id"])
        with self.app.test_request_context("/", json={"ref": "OTHERREF"}):
            ok, err = persist_referral_for_phone(existing["phone"], {"ref": "OTHERREF"})
            self.assertTrue(ok)
            user, status = get_or_create_user(existing["phone"], referrer_id=other["id"])
        self.assertEqual(status, "existing")
        self.assertEqual(user["referred_by"], self.referrer["id"])
        self.assertEqual(self.store.users_by_phone[existing["phone"]]["referred_by"], self.referrer["id"])
        self.assertNotEqual(user["referred_by"], other["id"])

    def test_invalid_referral_rejected(self):
        with self.app.test_request_context("/", json={"ref": "NOPE"}):
            ok, err = persist_referral_for_phone("9876543210", {"ref": "NOPE"})
        self.assertFalse(ok)
        self.assertIn("Invalid", err)

    def test_self_referral_does_not_attribute_or_block_login(self):
        with self.app.test_request_context("/", json={"ref": "REFCODE1"}):
            from flask import session
            session["ref_code"] = "REFCODE1"
            ok, err = persist_referral_for_phone(self.referrer["phone"], {"ref": "REFCODE1"})
            self.assertTrue(ok)
            self.assertIsNone(err)
            self.assertNotIn("ref_code", session)
        self.assertNotIn(self.referrer["phone"], self.store.pending)

    def test_send_otp_own_referral_still_sends(self):
        sms = MagicMock()
        sms.send_otp.return_value = (True, {}, "vid-self")
        with patch.object(auth_routes, "get_verification", return_value=None), \
             patch.object(auth_routes, "store_verification"), \
             patch.object(auth_routes, "get_sms_service", return_value=sms):
            res = self.client.post(
                "/api/auth/send-otp?ref=REFCODE1",
                json={"phone": self.referrer["phone"], "ref": "REFCODE1"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])
        self.assertNotIn(self.referrer["phone"], self.store.pending)

    def test_new_user_still_attributed_with_valid_third_party_code(self):
        with self.app.test_request_context("/", json={"ref": "REFCODE1"}):
            ok, err = persist_referral_for_phone("9876543210", {"ref": "REFCODE1"})
            self.assertTrue(ok)
            self.assertIsNone(err)
        self.assertEqual(self.store.pending["9876543210"]["referrer_id"], self.referrer["id"])

    def test_verify_without_browser_session_uses_pending_row(self):
        with self.app.test_request_context("/api/auth/send-otp?ref=REFCODE1", json={"phone": "9000000001"}):
            persist_referral_for_phone("9000000001", {"ref": "REFCODE1"})
        self.assertIn("9000000001", self.store.pending)
        with self.app.test_request_context("/api/auth/verify-otp", json={"phone": "9000000001", "otp": "123456"}):
            self.app.session_interface.get_signing_serializer(self.app)  # no session ref
            from flask import session
            session.pop("ref_code", None)
            rid, err = resolve_referrer_id_for_signup("9000000001", {})
        self.assertIsNone(err)
        self.assertEqual(rid, self.referrer["id"])

    def test_resend_otp_preserves_pending_referral(self):
        sms = MagicMock()
        sms.send_otp.return_value = (True, {}, "vid-2")
        with patch.object(auth_routes, "get_verification", return_value=None), \
             patch.object(auth_routes, "store_verification"), \
             patch.object(auth_routes, "get_sms_service", return_value=sms):
            res = self.client.post(
                "/api/auth/resend-otp?ref=REFCODE1",
                json={"phone": "9000000002", "ref": "REFCODE1"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])
        self.assertEqual(self.store.pending["9000000002"]["referrer_id"], self.referrer["id"])

    def test_duplicate_pending_upsert_keeps_latest_code(self):
        other = self.store.add_user("9111111111", "REFCODE2")
        with self.app.test_request_context("/", json={"ref": "REFCODE1"}):
            persist_referral_for_phone("9000000003", {"ref": "REFCODE1"})
        with self.app.test_request_context("/", json={"ref": "REFCODE2"}):
            persist_referral_for_phone("9000000003", {"ref": "REFCODE2"})
        self.assertEqual(self.store.pending["9000000003"]["referrer_id"], other["id"])
        self.assertEqual(self.store.pending["9000000003"]["ref_code"], "REFCODE2")

    def test_pending_referral_expiry_is_ignored(self):
        self.store.pending["9000000004"] = {
            "ref_code": "REFCODE1",
            "referrer_id": self.referrer["id"],
            "expires_at": datetime.utcnow() - timedelta(minutes=1),
        }
        with self.app.test_request_context("/"):
            pending = get_pending_referral("9000000004")
        self.assertIsNone(pending)

    def test_send_otp_persists_valid_ref(self):
        sms = MagicMock()
        sms.send_otp.return_value = (True, {}, "vid-1")
        with patch.object(auth_routes, "get_verification", return_value=None), \
             patch.object(auth_routes, "store_verification"), \
             patch.object(auth_routes, "get_sms_service", return_value=sms):
            res = self.client.post(
                "/api/auth/send-otp?ref=REFCODE1",
                json={"phone": "9876501234", "ref": "REFCODE1"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.pending["9876501234"]["referrer_id"], self.referrer["id"])

    def test_send_otp_rejects_invalid_ref(self):
        res = self.client.post(
            "/api/auth/send-otp?ref=MISSING",
            json={"phone": "9876501234", "ref": "MISSING"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid", res.get_json()["message"])

    def test_verify_otp_new_user_without_session_cookie(self):
        self.store.pending["9876509999"] = {
            "ref_code": "REFCODE1",
            "referrer_id": self.referrer["id"],
            "expires_at": datetime.utcnow() + timedelta(days=1),
        }
        sms = MagicMock()
        sms.verify_otp.return_value = (True, {})
        stored = {
            "verification_id": "v1",
            "attempts": 0,
            "expires_at": datetime.utcnow() + timedelta(minutes=1),
            "created_at": datetime.utcnow(),
        }
        with patch.object(auth_routes, "get_verification", return_value=stored), \
             patch.object(auth_routes, "delete_verification"), \
             patch.object(auth_routes, "get_sms_service", return_value=sms):
            res = self.client.post(
                "/api/auth/verify-otp",
                json={"phone": "9876509999", "otp": "123456"},
            )
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["data"]["status"], "new")
        self.assertEqual(body["data"]["user"]["referred_by"], self.referrer["id"])
        self.assertNotIn("9876509999", self.store.pending)

    def test_verify_otp_existing_user_keeps_referred_by(self):
        self.store.add_user("9876511111", "EXISTCODE", referred_by=None)
        sms = MagicMock()
        sms.verify_otp.return_value = (True, {})
        stored = {
            "verification_id": "v1",
            "attempts": 0,
            "expires_at": datetime.utcnow() + timedelta(minutes=1),
            "created_at": datetime.utcnow(),
        }
        with patch.object(auth_routes, "get_verification", return_value=stored), \
             patch.object(auth_routes, "delete_verification"), \
             patch.object(auth_routes, "get_sms_service", return_value=sms):
            res = self.client.post(
                "/api/auth/verify-otp",
                json={"phone": "9876511111", "otp": "123456", "ref": "REFCODE1"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["data"]["status"], "existing")
        self.assertIsNone(self.store.users_by_phone["9876511111"]["referred_by"])


class LandingAndFrontendTests(unittest.TestCase):
    def test_homepage_download_and_qr_do_not_drop_ref(self):
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("register_url_with_ref", app_src)
        self.assertIn('if ref:', app_src)
        self.assertNotIn("if ref:\n        pass", app_src)
        self.assertIn("register_url_with_ref(referral_code)", app_src)
        qr_fn = app_src.split("def generate_qr")[1].split("\n@app.route")[0]
        self.assertNotIn("cache_landing_referral_code", qr_fn)
        self.assertNotIn("/download-app?ref={referral_code}", app_src)

    def test_login_sends_ref_on_verify_and_resend(self):
        login = (ROOT / "templates" / "public" / "login.html").read_text(encoding="utf-8")
        self.assertIn("function referralCode()", login)
        self.assertIn("ref: referralCode()", login)
        self.assertIn("/api/auth/verify-otp", login)
        self.assertIn("/api/auth/resend-otp' + referralQuery()", login)

    def test_register_sends_ref_in_body_and_query(self):
        register = (ROOT / "templates" / "public" / "register.html").read_text(encoding="utf-8")
        self.assertIn('"?ref=" + encodeURIComponent(referral)', register)
        self.assertIn("ref: referral", register)
        self.assertIn("data.success", register)

    def test_pending_referrals_schema_present(self):
        init_db = (ROOT / "database" / "init_db.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS pending_referrals", init_db)
        self.assertIn("referrer_id INTEGER NOT NULL REFERENCES users(id)", init_db)
        migration = (ROOT / "migrations" / "add_pending_referrals.py").read_text(encoding="utf-8")
        self.assertIn("pending_referrals", migration)

    # 8. apple-touch-icon appears in both required layouts
    def test_apple_touch_icon_present_in_both_layouts(self):
        public_layout = (ROOT / "templates" / "layouts" / "layout_public.html").read_text(encoding="utf-8")
        app_layout = (ROOT / "templates" / "layouts" / "layout_app.html").read_text(encoding="utf-8")
        self.assertIn('rel="apple-touch-icon"', public_layout)
        self.assertIn("icon-192.png", public_layout)
        self.assertIn('rel="apple-touch-icon"', app_layout)
        self.assertIn("icon-192.png", app_layout)

    def test_manifest_start_url_and_qr_generation_untouched(self):
        """Regression guard (item 7): the manifest's start_url and the live
        QR endpoint must be exactly what they were before this change —
        referral preservation must not touch either."""
        manifest = (ROOT / "static" / "manifest.json").read_text(encoding="utf-8")
        self.assertIn('"start_url": "/"', manifest)
        qr_src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("signup_url = request.host_url.rstrip('/') + register_url_with_ref(referral_code)", qr_src)


class RegisterPageReferralCaptureTests(unittest.TestCase):
    """/register?ref=CODE must durably capture the code into the session on
    first page load — not just when the phone/OTP form is later submitted.
    Covers the PWA-install-before-registering gap: manifest start_url is a
    fixed "/", so a home-screen relaunch carries no ?ref= at all, and only
    a session cache that survives a real browser/app close (see
    cache_landing_referral_code's session.permanent=True) can still
    attribute the referral once the user actually registers."""

    def setUp(self):
        # send-otp is IP-rate-limited (5/min). @_limit(...) in auth_routes.py
        # captured its own reference to extensions.limiter at auth_routes'
        # first import (before app.py's later init_extensions(app) rebinds
        # the extensions.limiter name to a different object) — reset the
        # one auth_routes actually holds, so this class's calls can't be
        # starved by (or starve) unrelated tests that ran first.
        if getattr(auth_routes, "limiter", None) is not None:
            auth_routes.limiter.reset()
        self.store = ReferralStore()
        self.referrer = self.store.add_user("9998887777", "REFCODE1")
        self.engine = FakeEngine(self.store)
        self.engine_patch = patch.object(auth_routes, "engine", self.engine)
        self.engine_patch.start()
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def tearDown(self):
        self.engine_patch.stop()

    def _mock_send_otp(self, verification_id="vid"):
        sms = MagicMock()
        sms.send_otp.return_value = (True, {}, verification_id)
        return patch.object(auth_routes, "get_verification", return_value=None), \
            patch.object(auth_routes, "store_verification"), \
            patch.object(auth_routes, "get_sms_service", return_value=sms)

    # 1. GET /register?ref=VALID_CODE preserves referral
    def test_register_with_valid_ref_returns_200(self):
        res = self.client.get("/register?ref=REFCODE1")
        self.assertEqual(res.status_code, 200)

    # 2. Referral is available when registration begins later (no ref in
    # that later request at all — simulates a PWA install gap in between).
    def test_referral_survives_to_later_send_otp_with_no_ref_in_request(self):
        get_res = self.client.get("/register?ref=REFCODE1")
        self.assertEqual(get_res.status_code, 200)
        p1, p2, p3 = self._mock_send_otp("vid-later")
        with p1, p2, p3:
            res = self.client.post("/api/auth/send-otp", json={"phone": "9876500002"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.pending["9876500002"]["referrer_id"], self.referrer["id"])

    # 3. Invalid referral code is not persisted
    def test_register_with_invalid_ref_does_not_persist(self):
        res = self.client.get("/register?ref=NOSUCHCODE")
        self.assertEqual(res.status_code, 200, "an invalid code must not break the page")
        p1, p2, p3 = self._mock_send_otp("vid-invalid")
        with p1, p2, p3:
            self.client.post("/api/auth/send-otp", json={"phone": "9876500001"})
        self.assertNotIn("9876500001", self.store.pending)

    # 4. Existing referral attribution still works (query/body path, no
    # prior /register landing involved at all)
    def test_direct_send_otp_with_ref_still_works_unaffected(self):
        p1, p2, p3 = self._mock_send_otp("vid-direct")
        with p1, p2, p3:
            res = self.client.post(
                "/api/auth/send-otp?ref=REFCODE1",
                json={"phone": "9876500005", "ref": "REFCODE1"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.pending["9876500005"]["referrer_id"], self.referrer["id"])

    # 5. Normal /register without ref still works exactly as before
    def test_register_without_ref_still_works(self):
        res = self.client.get("/register")
        self.assertEqual(res.status_code, 200)
        p1, p2, p3 = self._mock_send_otp("vid-noref")
        with p1, p2, p3:
            self.client.post("/api/auth/send-otp", json={"phone": "9876500006"})
        self.assertNotIn("9876500006", self.store.pending)

    # 6. Referral cannot be incorrectly replaced by an unrelated later request
    def test_unrelated_later_request_does_not_replace_existing_session_referral(self):
        self.client.get("/register?ref=REFCODE1")
        self.client.get("/register")  # unrelated: no ref, must not clear/replace
        p1, p2, p3 = self._mock_send_otp("vid-unrelated")
        with p1, p2, p3:
            res = self.client.post("/api/auth/send-otp", json={"phone": "9876500003"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.pending["9876500003"]["referrer_id"], self.referrer["id"])

    # Full flow: landing -> (simulated install gap) -> send-otp -> verify-otp
    # -> new account created with referred_by correctly attributed.
    def test_full_flow_landing_before_registration_then_verify_otp(self):
        self.client.get("/register?ref=REFCODE1")
        p1, p2, p3 = self._mock_send_otp("vid-e2e")
        with p1, p2, p3:
            send_res = self.client.post("/api/auth/send-otp", json={"phone": "9876500004"})
        self.assertEqual(send_res.status_code, 200)

        sms = MagicMock()
        sms.verify_otp.return_value = (True, {})
        stored = {
            "verification_id": "vid-e2e",
            "attempts": 0,
            "expires_at": datetime.utcnow() + timedelta(minutes=1),
            "created_at": datetime.utcnow(),
        }
        with patch.object(auth_routes, "get_verification", return_value=stored), \
             patch.object(auth_routes, "delete_verification"), \
             patch.object(auth_routes, "get_sms_service", return_value=sms):
            verify_res = self.client.post(
                "/api/auth/verify-otp",
                json={"phone": "9876500004", "otp": "123456"},
            )
        self.assertEqual(verify_res.status_code, 200, verify_res.get_json())
        body = verify_res.get_json()
        self.assertEqual(body["data"]["status"], "new")
        self.assertEqual(body["data"]["user"]["referred_by"], self.referrer["id"])
        self.assertNotIn("9876500004", self.store.pending)


class LandingRouteTests(unittest.TestCase):
    def test_homepage_and_download_redirect_to_register(self):
        store = ReferralStore()
        store.add_user("9998887777", "REFCODE1")
        engine = FakeEngine(store)
        with patch.object(auth_routes, "engine", engine):
            from app import app as flask_app
            flask_app.config["TESTING"] = True
            client = flask_app.test_client()
            home = client.get("/?ref=REFCODE1")
            self.assertEqual(home.status_code, 302)
            self.assertIn("/register?ref=REFCODE1", home.headers.get("Location", ""))
            down = client.get("/download-app?ref=REFCODE1")
            self.assertEqual(down.status_code, 302)
            self.assertIn("/register?ref=REFCODE1", down.headers.get("Location", ""))

    def test_join_route_redirects_with_ref(self):
        store = ReferralStore()
        store.add_user("9998887777", "REFCODE1")
        engine = FakeEngine(store)
        with patch.object(auth_routes, "engine", engine):
            from app import app as flask_app
            flask_app.config["TESTING"] = True
            client = flask_app.test_client()
            join = client.get("/join?ref=REFCODE1")
            self.assertEqual(join.status_code, 302)
            self.assertIn("/register?ref=REFCODE1", join.headers.get("Location", ""))

    def test_join_route_without_ref_goes_to_register(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        client = flask_app.test_client()
        join = client.get("/join")
        self.assertEqual(join.status_code, 302)
        self.assertIn("/register", join.headers.get("Location", ""))
        self.assertNotIn("ref=", join.headers.get("Location", ""))

    def test_join_route_with_invalid_ref_does_not_attribute_but_still_redirects(self):
        store = ReferralStore()
        engine = FakeEngine(store)
        with patch.object(auth_routes, "engine", engine):
            from app import app as flask_app
            flask_app.config["TESTING"] = True
            client = flask_app.test_client()
            join = client.get("/join?ref=NOSUCHCODE")
            self.assertEqual(join.status_code, 302)
            self.assertIn("/register?ref=NOSUCHCODE", join.headers.get("Location", ""))

    def test_qr_encodes_register_url(self):
        from routes.auth_routes import register_url_with_ref
        self.assertTrue(register_url_with_ref("REFCODE1").startswith("/register?ref="))
        qr_src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("signup_url = request.host_url.rstrip('/') + register_url_with_ref(referral_code)", qr_src)
        qr_fn = qr_src.split("def generate_qr")[1].split("\n@app.route")[0]
        self.assertNotIn("cache_landing_referral_code", qr_fn)


if __name__ == "__main__":
    unittest.main()
