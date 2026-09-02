"""Automatic referral-code generation, backfill, and the referral-info API.

Complements tests/test_referral_attribution.py (which already covers referral
*attribution*: ref-code capture, referred_by assignment, self-referral,
immutability). This file covers the referral *code* itself: generation
format/uniqueness, collision retry, backfilling existing users, and the
/api/invite + /api/referral/info response shape.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-32bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from sqlalchemy.exc import IntegrityError

from extensions import init_extensions

_bootstrap = Flask(__name__)
_bootstrap.config["SECRET_KEY"] = "test-secret"
init_extensions(_bootstrap)

from routes.auth_routes import generate_referral_code, referral_link_for
from config.payment_config import BASE_URL

ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


class GeneratorFormatTests(unittest.TestCase):
    """Requirement 1: unique, short, secure/random, not sequential/ID-like."""

    def test_code_is_short_and_uses_expected_charset(self):
        for _ in range(300):
            code = generate_referral_code()
            self.assertEqual(len(code), 8)
            self.assertTrue(set(code) <= ALPHABET, code)

    def test_code_is_not_a_database_id_or_sequential(self):
        codes = [generate_referral_code() for _ in range(50)]
        # A code derived from a sequential ID would be numeric-only and/or
        # monotonically increasing. Ensure real letters appear and codes
        # don't trivially sort into an increasing numeric sequence.
        self.assertTrue(any(c.isalpha() for code in codes for c in code))
        self.assertFalse(all(code.isdigit() for code in codes))

    def test_repeated_calls_are_practically_unique(self):
        codes = [generate_referral_code() for _ in range(500)]
        # 36^8 keyspace: 500 draws colliding would indicate a broken RNG, not chance.
        self.assertEqual(len(set(codes)), len(codes))


class ReferralLinkTests(unittest.TestCase):
    """Requirement 8: expose a referral_link built from existing config, not a hardcoded domain."""

    def test_referral_link_uses_existing_base_url_config(self):
        link = referral_link_for("LMK7X4P2")
        self.assertTrue(link.startswith(BASE_URL.rstrip("/")))
        self.assertIn("/register?ref=LMK7X4P2", link)

    def test_referral_link_for_empty_code_is_empty(self):
        self.assertEqual(referral_link_for(""), "")
        self.assertEqual(referral_link_for(None), "")

    def test_no_hardcoded_production_domain_in_helper_source(self):
        src = (ROOT / "routes" / "auth_routes.py").read_text(encoding="utf-8")
        fn_src = src.split("def referral_link_for")[1].split("\ndef ")[0]
        self.assertNotIn("landmarkvts.in", fn_src)
        self.assertIn("BASE_URL", fn_src)


# ---------------------------------------------------------------------------
# Minimal fake DB harness for backfill + /api/invite tests
# ---------------------------------------------------------------------------

class FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class FakeResult:
    def __init__(self, row=None, rows=None, rowcount=0):
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class UsersStore:
    def __init__(self):
        self.users = {}

    def add(self, uid, referral_code=None, referred_by=None):
        self.users[uid] = {"id": uid, "referral_code": referral_code, "referred_by": referred_by}


class FakeConn:
    """Understands just the handful of statements these two call sites issue."""

    def __init__(self, store):
        self.store = store

    def execute(self, sql, params=None):
        q = " ".join(str(getattr(sql, "text", sql)).lower().split())
        params = params or {}

        if q.startswith("select id from users where referral_code is null"):
            rows = [FakeRow({"id": u["id"]}) for u in self.store.users.values() if u["referral_code"] is None]
            return FakeResult(rows=rows)

        if q.startswith("select referral_code from users where id"):
            u = self.store.users.get(int(params.get("uid")))
            return FakeResult(FakeRow({"referral_code": u["referral_code"]}) if u else None)

        if q.startswith("select count(*) as cnt from users where referred_by"):
            uid = int(params.get("uid"))
            cnt = sum(1 for u in self.store.users.values() if u.get("referred_by") == uid)
            return FakeResult(FakeRow({"cnt": cnt}))

        if q.startswith("update users set referral_code"):
            code = params["code"]
            uid = int(params["uid"])
            if any(u["referral_code"] == code for u in self.store.users.values()):
                raise IntegrityError("duplicate referral_code", params, Exception())
            u = self.store.users.get(uid)
            if u is None or u["referral_code"] is not None:
                return FakeResult(rowcount=0)
            u["referral_code"] = code
            return FakeResult(rowcount=1)

        return FakeResult()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class BackfillTests(unittest.TestCase):
    """Requirement 2: safe backfill for existing users."""

    def setUp(self):
        self.store = UsersStore()

    def test_backfill_assigns_codes_only_to_null_rows(self):
        self.store.add(1, referral_code=None)
        self.store.add(2, referral_code="EXISTING1")
        self.store.add(3, referral_code=None)
        conn = FakeConn(self.store)
        with patch("migrations.backfill_referral_codes.get_db_connection", return_value=conn):
            from migrations.backfill_referral_codes import backfill_referral_codes
            result = backfill_referral_codes()
        self.assertEqual(result["assigned"], 2)
        self.assertIsNotNone(self.store.users[1]["referral_code"])
        self.assertIsNotNone(self.store.users[3]["referral_code"])

    def test_backfill_never_overwrites_existing_code(self):
        self.store.add(1, referral_code="KEEPTHIS")
        conn = FakeConn(self.store)
        with patch("migrations.backfill_referral_codes.get_db_connection", return_value=conn):
            from migrations.backfill_referral_codes import backfill_referral_codes
            result = backfill_referral_codes()
        self.assertEqual(result["assigned"], 0)
        self.assertEqual(self.store.users[1]["referral_code"], "KEEPTHIS")

    def test_backfill_is_idempotent_on_rerun(self):
        self.store.add(1, referral_code=None)
        conn = FakeConn(self.store)
        with patch("migrations.backfill_referral_codes.get_db_connection", return_value=conn):
            from migrations.backfill_referral_codes import backfill_referral_codes
            backfill_referral_codes()
            first_code = self.store.users[1]["referral_code"]
            result2 = backfill_referral_codes()
        self.assertEqual(result2["assigned"], 0)
        self.assertEqual(self.store.users[1]["referral_code"], first_code)

    def test_backfill_retries_past_a_collision(self):
        self.store.add(1, referral_code=None)
        self.store.add(2, referral_code="TAKEN0001")
        conn = FakeConn(self.store)
        seq = iter(["TAKEN0001", "TAKEN0001", "FRESHCODE"])
        with patch("migrations.backfill_referral_codes.get_db_connection", return_value=conn), \
             patch("migrations.backfill_referral_codes.generate_referral_code", side_effect=lambda: next(seq)):
            from migrations.backfill_referral_codes import backfill_referral_codes
            result = backfill_referral_codes()
        self.assertEqual(result["assigned"], 1)
        self.assertEqual(self.store.users[1]["referral_code"], "FRESHCODE")

    def test_add_column_defensive_statements_precede_the_index(self):
        """The ADD COLUMN IF NOT EXISTS guards must run before the unique
        index that references referral_code, or a pre-existing DB missing
        the column would fail at boot."""
        init_db_src = (ROOT / "database" / "init_db.py").read_text(encoding="utf-8")
        add_col_pos = init_db_src.index('ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT')
        index_pos = init_db_src.index('CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_code')
        self.assertLess(add_col_pos, index_pos)
        self.assertIn(
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by INTEGER REFERENCES users(id)',
            init_db_src,
        )


class ApiInviteTests(unittest.TestCase):
    """Requirement 1 (retry-safe) + 8 (referral_link) for the existing /api/invite endpoint."""

    def setUp(self):
        self.store = UsersStore()
        self.store.add(1, referral_code=None)
        self.store.add(2, referral_code="MYCODE01")
        self.conn = FakeConn(self.store)

        self.app = Flask(__name__)
        self.app.config.update(
            SECRET_KEY="test-secret",
            JWT_SECRET_KEY="test-jwt-secret-key-32bytes-long",
            JWT_TOKEN_LOCATION=["headers"],
            JWT_COOKIE_CSRF_PROTECT=False,
            TESTING=True,
        )
        JWTManager(self.app)
        from routes.user_routes import user_bp
        self.app.register_blueprint(user_bp, url_prefix="/api/user")
        self.client = self.app.test_client()

    def _headers(self, uid):
        with self.app.app_context():
            token = create_access_token(identity=str(uid))
        return {"Authorization": f"Bearer {token}"}

    def test_invite_assigns_canonical_format_code_when_missing(self):
        with patch("routes.user_routes.get_db_connection", return_value=self.conn):
            res = self.client.get("/api/user/api/invite", headers=self._headers(1))
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        code = body["referral_code"]
        self.assertEqual(len(code), 8)
        self.assertTrue(set(code) <= ALPHABET)
        self.assertNotIn("-", code)
        self.assertNotIn("_", code)  # old secrets.token_urlsafe() output could contain these
        self.assertEqual(self.store.users[1]["referral_code"], code)

    def test_invite_returns_referral_link(self):
        with patch("routes.user_routes.get_db_connection", return_value=self.conn):
            res = self.client.get("/api/user/api/invite", headers=self._headers(2))
        body = res.get_json()
        self.assertEqual(body["referral_code"], "MYCODE01")
        self.assertIn("MYCODE01", body["referral_link"])
        self.assertTrue(body["referral_link"].startswith(BASE_URL.rstrip("/")))

    def test_invite_preserves_existing_code_unchanged(self):
        with patch("routes.user_routes.get_db_connection", return_value=self.conn):
            self.client.get("/api/user/api/invite", headers=self._headers(2))
        self.assertEqual(self.store.users[2]["referral_code"], "MYCODE01")

    def test_invite_retries_past_a_collision_instead_of_500(self):
        self.store.add(3, referral_code=None)
        seq = iter(["MYCODE01", "MYCODE01", "BRANDNEW"])
        with patch("routes.user_routes.get_db_connection", return_value=self.conn), \
             patch("routes.user_routes.generate_referral_code", side_effect=lambda: next(seq)):
            res = self.client.get("/api/user/api/invite", headers=self._headers(3))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["referral_code"], "BRANDNEW")

    def test_invite_uses_canonical_generator_not_legacy_token_urlsafe(self):
        src = (ROOT / "routes" / "user_routes.py").read_text(encoding="utf-8")
        invite_src = src.split("def api_invite")[1].split("\n@user_bp.route")[0]
        self.assertNotIn("token_urlsafe", invite_src)
        self.assertIn("generate_referral_code", invite_src)

    def test_invite_returns_referral_count(self):
        self.store.add(4, referral_code="REFERRER")
        self.store.add(5, referral_code="AAAAAAAA", referred_by=4)
        self.store.add(6, referral_code="BBBBBBBB", referred_by=4)
        conn = FakeConn(self.store)
        with patch("routes.user_routes.get_db_connection", return_value=conn):
            res = self.client.get("/api/user/api/invite", headers=self._headers(4))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["referral_count"], 2)

    def test_invite_referral_count_zero_for_new_user(self):
        with patch("routes.user_routes.get_db_connection", return_value=self.conn):
            res = self.client.get("/api/user/api/invite", headers=self._headers(2))
        self.assertEqual(res.get_json()["referral_count"], 0)


class ReferralCardFrontendTests(unittest.TestCase):
    """The invite page's branded 'Share QR' card: reuses the real logo and
    the backend-provided referral_link, never a reconstructed URL or any
    private user field. Source-text checks, matching this file's existing
    style for JS behavior that has no Python-side test harness."""

    def setUp(self):
        self.src = (ROOT / "templates" / "users" / "invite.html").read_text(encoding="utf-8")

    def test_share_qr_button_and_card_builder_present(self):
        self.assertIn('id="shareQrBtn"', self.src)
        self.assertIn("function buildReferralCard", self.src)
        self.assertIn("function shareQr", self.src)
        self.assertIn("getContext(\"2d\")", self.src)

    def test_existing_share_link_and_whatsapp_buttons_still_present(self):
        self.assertIn('id="shareBtn"', self.src)
        self.assertIn('id="whatsappBtn"', self.src)
        self.assertIn('id="copyBtn"', self.src)
        self.assertIn("function nativeShare", self.src)
        self.assertIn("wa.me", self.src)

    def test_card_uses_backend_referral_link_not_a_rebuilt_url(self):
        load_fn = self.src.split("async function loadReferral")[1].split("\n  async function")[0]
        self.assertIn("currentLinkValue = data.referral_link", load_fn)
        card_fn = self.src.split("function buildReferralCard")[1].split("\n  function downloadCard")[0]
        self.assertIn("currentLinkValue", card_fn)
        # The card drawing code must not synthesize its own /register?ref= URL —
        # only the pre-fetch fallback (outside buildReferralCard) may do that.
        self.assertNotIn("register?ref=", card_fn)

    def test_card_uses_the_real_landmark_logo_asset(self):
        self.assertIn("images/landmark-logo.png", self.src)
        self.assertIn("LOGO_URL", self.src)
        self.assertNotIn("data:image", self.src)  # no invented/inline placeholder logo

    def test_card_qr_source_is_the_existing_dynamic_endpoint(self):
        card_fn = self.src.split("function buildReferralCard")[1].split("\n  function downloadCard")[0]
        self.assertIn('"/qr/" + encodeURIComponent(currentCode)', card_fn)

    def test_card_never_draws_private_user_data(self):
        card_fn = self.src.split("function buildReferralCard")[1].split("\n  function downloadCard")[0]
        for forbidden in ("phone", "email", "wallet_balance", "user_id", "token", "password"):
            self.assertNotIn(forbidden, card_fn.lower())

    def test_share_qr_feature_detects_web_share_api(self):
        share_fn = self.src.split("async function shareQr")[1].split("\n  copyBtn.addEventListener")[0]
        self.assertIn("navigator.canShare", share_fn)
        self.assertIn("navigator.share", share_fn)
        # Must not assume support unconditionally.
        self.assertNotIn("navigator.share(", share_fn.split("if (navigator.canShare")[0])

    def test_share_qr_has_a_non_native_fallback(self):
        share_fn = self.src.split("async function shareQr")[1].split("\n  copyBtn.addEventListener")[0]
        self.assertIn("downloadCard(blob)", share_fn)

    def test_shared_message_includes_link_and_code(self):
        share_fn = self.src.split("async function shareQr")[1].split("\n  copyBtn.addEventListener")[0]
        self.assertIn("Join me on LANDMARK!", share_fn)
        self.assertIn("currentLinkValue", share_fn)
        self.assertIn("Referral Code: \" + currentCode", share_fn)

    def test_card_preview_element_present(self):
        self.assertIn('id="cardPreview"', self.src)
        self.assertIn('id="cardPreviewWrap"', self.src)


class QrCodeEndpointTests(unittest.TestCase):
    """Requirement 12/13: QR encodes the referral URL and never touches the DB
    or attributes a referral — viewing/scanning a QR must not credit anything."""

    def test_qr_endpoint_returns_png_without_any_db_access(self):
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        def _boom(*a, **k):
            raise AssertionError("QR generation must not touch the database")

        with patch("database.init_db.get_db_connection", side_effect=_boom), \
             patch("database.init_db.engine.connect", side_effect=_boom):
            res = client.get("/qr/SOMECODE123")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "image/png")

    def test_qr_content_encodes_the_referral_url(self):
        import io
        try:
            from pyzbar.pyzbar import decode as _decode  # optional; skip if unavailable
        except Exception:
            self.skipTest("pyzbar not installed; covered instead by test_qr_encodes_register_url")
        from PIL import Image
        from app import app as flask_app
        flask_app.config["TESTING"] = True
        client = flask_app.test_client()
        res = client.get("/qr/SOMECODE123", headers={"Host": "landmarkvts.in"})
        img = Image.open(io.BytesIO(res.data))
        decoded = _decode(img)
        self.assertTrue(decoded)
        payload = decoded[0].data.decode("utf-8")
        self.assertIn("/register?ref=SOMECODE123", payload)


class ReferralInfoServiceTests(unittest.TestCase):
    """services/referral_service.get_referral_info now also returns referral_link."""

    def test_get_referral_info_includes_referral_link(self):
        from services import referral_service

        class Row:
            _mapping = {"referral_code": "ABCD1234", "wallet_balance": 12.5}

        class Conn:
            def execute(self, *a, **k):
                class R:
                    def fetchone(self_inner):
                        return Row()
                return R()

        with patch.object(referral_service, "get_db_connection", return_value=Conn()):
            info = referral_service.get_referral_info(42)
        self.assertEqual(info["referral_code"], "ABCD1234")
        self.assertIn("ABCD1234", info["referral_link"])
        self.assertTrue(info["referral_link"].startswith(BASE_URL.rstrip("/")))


class VerifyOtpResponseTests(unittest.TestCase):
    """Requirement 3: registration response already carries referral_code;
    it should now also carry referral_link, additively."""

    def test_verify_otp_route_includes_referral_link_in_response(self):
        src = (ROOT / "routes" / "auth_routes.py").read_text(encoding="utf-8")
        verify_fn = src.split("def verify_otp")[1].split("\n@auth_bp.route")[0]
        self.assertIn('"referral_link": referral_link_for(user_data.get("referral_code"))', verify_fn)
        # The existing response shape (status/user) must still be present — additive only.
        self.assertIn('"status": status', verify_fn)
        self.assertIn('"user": user_data', verify_fn)


if __name__ == "__main__":
    unittest.main()
