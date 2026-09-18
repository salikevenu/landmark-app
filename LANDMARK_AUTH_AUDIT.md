# LANDMARK — Auth Audit & Signup/Login Migration Plan

**Scope:** authentication only — OTP issue/verify, JWT session lifecycle, the register/login/install pages, and the routes that gate them. No other feature, service or route was examined or changed.

**Files in scope**

```
routes/auth_routes.py          routes/public_routes.py
services/sms_service.py        services/jwt_session.py
services/jwt_blocklist.py      services/authz.py
auth/otp_service.py            middleware/auth_middleware.py
middleware/admin_required.py   middleware/role_required.py
middleware/security_headers.py extensions.py
templates/public/login.html    templates/public/register.html
templates/public/install.html
app.py  (JWT config, /api/refresh, /api/refresh/silent, /logout, /, /join, /download-app)
```

**Status:** audit + plan only. Nothing was modified.

---

## 1. The single most important finding for your redesign

**Register and login are already the same flow.** They differ only in HTML.

Both `register.html` and `login.html` call the identical three endpoints — `POST /api/auth/send-otp`, `POST /api/auth/verify-otp`, `POST /api/auth/resend-otp` — and `verify_otp()` calls `get_or_create_user()`, which creates the account if the phone is unknown and signs it in if it isn't. It already returns `status: "new" | "existing"`.

So there is no register-vs-login distinction on the server to migrate. The backend is already a signup/login flow; only the frontend pretends otherwise. That makes the change you want low-risk: it is a page-and-routing change plus one genuine bug fix, not an auth-logic rewrite.

---

## 2. Current flow map

```
QR / shared link  ──> /install?ref=CODE ──(Continue)──> /register
/  ?ref=CODE      ──> /register?ref=CODE
/join?ref=CODE    ──> /register?ref=CODE
/download-app?ref ──> /register?ref=CODE   (never serves the APK when ref is present)

/register            -> register.html   [phone + name + referral]
/public/login        -> login.html      (public_bp — NO authed-user redirect)
/api/auth/public/login -> login.html    (auth_bp — HAS authed-user redirect)

POST /api/auth/send-otp    -> Message Central v3/send    -> otp_verifications row
POST /api/auth/verify-otp  -> v3/validateOtp -> get_or_create_user -> JWT cookies
POST /api/auth/resend-otp  -> same as send-otp, 60s cooldown
POST /api/auth/logout  |  GET /logout   -> revoke JTIs + clear cookies
GET  /api/auth/me      -> current user

Session keep-alive: page 401 -> /api/refresh/silent (GET) -> new access cookie -> back to page
                    API  401 -> POST /api/refresh (needs X-CSRF-TOKEN)
```

---

## 3. Findings

### CRITICAL

**A-1 — OTP verification may accept any 6-digit code.**
`services/sms_service.py:157`

```python
if response.status_code == 200:
    data = response.json()
    logger.info(f"OTP verified successfully for ID {verification_id}")
    return True, data
```

The transport status is treated as the verification result. Message Central's `v3/validateOtp` returns **HTTP 200 with a failure body** in its normal error cases — `responseCode: 702` / `verificationStatus: "VERIFICATION_FAILED"` for a wrong code, `705` for an expired one. Nothing in this function reads `responseCode`, `data.verificationStatus`, or `data.errorMessage`.

If that is how your account behaves, then **anyone who knows a phone number can sign in as that user by typing any six digits** — and because `get_or_create_user()` runs on the same path, they can also create accounts against numbers they do not control, which feeds the referral/wallet system.

`routes/auth_routes.py:615` trusts this return value completely, so the DB-side `MAX_OTP_ATTEMPTS` guard never fires — a wrong OTP would be reported as correct on the first try.

*Confirm before anything else:* line 145 already logs the raw response body. Submit a deliberately wrong OTP in staging and read the log. If the body carries a non-success `responseCode`, this is live and is your top priority.

*Fix:* require both `status_code == 200` **and** an explicit success marker in the body (`responseCode in (200,)` and `data["data"]["verificationStatus"] == "VERIFICATION_COMPLETED"`), and fail closed on any body you cannot parse.

---

### HIGH

**A-2 — The name collected at registration is thrown away.**
`register.html:57` requires a name and posts it on both `send-otp` and `verify-otp`. Neither endpoint reads it. `get_or_create_user()` hardcodes it:

```python
INSERT INTO users (phone, name, role, ...) VALUES (:phone, '', 'free', ...)
```

Every account is created with an empty name. The registration form's extra field buys nothing and costs you conversions. This is the bug your signup redesign should fix properly — see §4, step 3.

**A-3 — All rate limiting collapses into one shared bucket in production.**
`extensions.py:43` uses `get_remote_address` (i.e. `request.remote_addr`), and `app.py` never installs `ProxyFix`. Behind Render's load balancer `remote_addr` is the proxy, not the user. Consequences:

- `@_limit("5 per minute")` on `send-otp` is 5/min **for your entire user base**, not per user. At any real signup volume legitimate users start getting 429s.
- `verify-otp` (10/min, 30/hour) has no per-phone key at all, so its only meaningful protection is the DB `MAX_OTP_ATTEMPTS = 5` counter.
- `users.ip_address` records the proxy IP for every signup, so the fraud signal is worthless.

The per-phone key on `send-otp`/`resend-otp` (`otp_phone_key`, 3/hour) is correct and is currently the only limit doing real work.

*Fix:* `app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)` before `init_extensions`, and add a per-phone key to `verify-otp`.

**A-4 — Every OTP send blocks the whole application for up to 20 seconds.**
`gunicorn.conf.py` sets `workers = 1, threads = 1, worker_class = "sync"`. `sms_service.send_otp` uses `timeout=(5, 15)`; `verify_otp` retries up to 3 times on 5xx, so worst case is roughly 60s plus backoff. A single sync worker means one slow Message Central call stalls **every other request on the site** — dashboards, listings, payments. This is the biggest availability risk on the auth path.

*Fix:* `threads = 4` (or `worker_class = "gthread"`) is the minimal change; it does not affect correctness since OTP state lives in Postgres.

**A-5 — Two divergent `/public/login` routes.**
`public_bp` registers `/public/login` (`routes/public_routes.py:6`) with **no** authenticated-user redirect. `auth_bp` registers `/api/auth/public/login` (`auth_routes.py:718`) **with** the redirect. `register.html` and `/api/refresh/silent` both point at the `/api/auth/...` one, so the bare `/public/login` is an unguarded duplicate that shows a logged-in user a fresh OTP form. Delete it as part of the migration.

**A-6 — Blocked and deactivated users are issued tokens before being rejected.**
`verify_otp()` never checks `is_blocked` / `is_active`. A banned user passes OTP, gets valid access and refresh cookies set, is redirected to `/dashboard`, and only then is rejected by `lookup_jwt_user()` (`jwt_session.py:51`) — which returns 401 and bounces them through `/api/refresh/silent` back to login, in a loop, with no explanation.

*Fix:* check both columns in `get_or_create_user`'s existing-user branch and return a clear 403 before minting tokens.

---

### MEDIUM

**A-7 — `remember_me` issues a 30-day *access* token.**
`auth_routes.py:656`. Access tokens are the ones that cannot be cheaply revoked — the blocklist (`jwt_blocklist.py`) falls back to per-process memory whenever Redis is unreachable, and `extensions.py:31` shows Redis is treated as optional. A 30-day access token on a compromised device survives logout in that state.

*Fix:* keep the access token at 2 hours always; let `remember_me` extend only the refresh token (already 365 days). The silent-refresh path (`/api/refresh/silent`) makes this invisible to the user — it is the mechanism that already keeps people signed in across restarts.

**A-8 — The OTP record is deleted before the user row is created.**
`auth_routes.py:621` calls `delete_verification()`, then `get_or_create_user()` at line 628. If the insert fails (DB blip, referral retry exhaustion) the OTP is already consumed, the user sees a generic 500, and the 60-second resend cooldown blocks an immediate retry. Move the delete after a successful user resolution, or guard the window.

**A-9 — The guess budget resets on every resend.**
`store_verification`'s `ON CONFLICT` sets `attempts = 0`. With `MAX_OTP_ATTEMPTS = 5`, a 60s cooldown and 3 sends/hour per phone, an attacker gets ~15 guesses per hour per number rather than 5. Against a 6-digit space that is fine on its own; it matters only if A-1 is not the real problem.

**A-10 — The referral session cookie lives for ten years.**
`app.py:127` sets `PERMANENT_SESSION_LIFETIME` to 3650 days and `cache_landing_referral_code` marks the session permanent. The comment explains why (surviving a PWA install between QR scan and signup), and it is honest reasoning — but it applies to the whole Flask session for every visitor, and there is no `SECRET_KEY` rotation story. A 30–90 day scoped cookie would serve the same purpose.

**A-11 — `_limit` silently becomes a no-op.**
`auth_routes.py:39` captures `extensions.limiter` at import time and returns the undecorated function if it is `None`. Today `app.py` calls `init_extensions` (line 153) before importing routes (line 209), so limits are live — but the ordering is load-bearing and undocumented. Any test harness or alternate entrypoint that imports `routes` first gets **completely unrate-limited OTP endpoints with no warning**. Log a warning in that branch.

**A-12 — The frontend never sends a CSRF token.**
`JWT_COOKIE_CSRF_PROTECT = True`, so `POST /api/refresh` requires `X-CSRF-TOKEN` read from the `csrf_access_token` cookie. Neither `login.html` nor `register.html` does this. It works today only because browsers use the GET `/api/refresh/silent` path instead. Worth confirming whichever client calls the POST variant (the POS client sends `X-Client-Type: pos` and takes tokens from the body, so it likely uses headers rather than cookies — confirm rather than assume).

**A-13 — Signup asks for GPS permission on the login screen.**
`login.html:571` awaits `getLoginLocation()` before verifying the OTP, with an 8-second timeout. A browser permission prompt at the moment of login is a well-known conversion killer, and the coordinates are **discarded for existing users** anyway (`get_or_create_user` only stores them on INSERT). Move this to after first login, or drop it.

---

### LOW / hygiene

- **A-14** `/download-app?ref=CODE` redirects to `/register` and never serves the APK (`app.py:526`). Only reachable with a ref, but the route name lies.
- **A-15** `install.html`'s `goToDashboard()` navigates to `/register`. Rename it.
- **A-16** Duplicate logout routes: `GET /api/auth/logout` redirects to `/logout`, which repeats the same revoke-and-clear. Harmless, but two code paths for one action.
- **A-17** `middleware/admin_required.py` and `middleware/auth_middleware.py` are byte-for-byte duplicates of the same `admin_required` decorator. Delete one.
- **A-18** `auth/otp_service.py` is a disabled legacy stub. Delete it — it is the only thing in `auth/` and its existence invites a wrong import.
- **A-19** `middleware/security_headers.py` sets only three headers. No `Content-Security-Policy`, `Strict-Transport-Security`, or `Referrer-Policy`. `X-XSS-Protection` is deprecated and ignored by modern browsers.
- **A-20** Phone-number enumeration is inherent to this design (`verify-otp` returns `status: new|existing`, and the message differs). Unavoidable for phone-first auth; noted so it is a decision rather than an oversight.

**Correctly done, worth preserving:** OTP state in Postgres with server-side `NOW()` throughout (no client clocks); resend cooldown as a constant separate from OTP validity, with the reasoning documented; attempts incremented only on failure; `DEBUG_SMS` hard-disabled when `RENDER=true`; `httpOnly` + `SameSite=Lax` cookies; JWT blocklist on logout; banned-user check in the JWT user loader; `set_access_cookies` rather than tokens in `localStorage`; the non-retryable POST for the non-idempotent send. This is a more careful auth layer than most at this stage.

---

## 4. Migration plan — register/login → signup/login

**Principle:** the API does not change. `send-otp` / `verify-otp` / `resend-otp` keep their current contract, so nothing outside auth can break. This is a page, route and copy change plus the A-2 fix.

### Target flow

```
Any entry point (/, /join, /install, QR, /signup, /login)
        │
        ▼
  ONE screen: "Enter your mobile number"          <- phone only
        │  POST /api/auth/send-otp
        ▼
  "Enter the 6-digit code"                        <- OTP + remember me
        │  POST /api/auth/verify-otp  -> status
        ├── status "existing" ──────────────────> /dashboard
        └── status "new" ───────────────────────> /welcome  (name, then dashboard)
```

One screen replaces two. The user never has to know whether they have an account — which is the whole point, since the server already doesn't ask.

### Steps

**Step 1 — Add the unified template.**
Create `templates/public/auth.html` from `login.html` (it is the better of the two: spinner states, resend cooldown, change-number, auto-submit on the sixth digit, `autocomplete="one-time-code"`). Two changes: drop the `getLoginLocation()` call (A-13), and branch on `result.data.status` at line 593 — `"new"` goes to `/welcome`, `"existing"` goes to `/dashboard` or `/admin/dashboard`. Delete the phone-disabled-on-OTP-step behaviour only if you want inline editing; otherwise leave it.

**Step 2 — Rewire the routes.** In `routes/public_routes.py`:

| Route | Now | After |
|---|---|---|
| `/signup` | — | renders `auth.html`, `mode="signup"`, captures `?ref` |
| `/login` | — | renders `auth.html`, `mode="login"` |
| `/register` | renders `register.html` | `301` → `/signup` (preserve `?ref`) |
| `/public/login` | renders `login.html`, unguarded | delete (A-5) |
| `/api/auth/public/login` | renders `login.html` | `301` → `/login` |

Both `/signup` and `/login` keep the `_current_request_is_authenticated_user()` → `/dashboard` guard that `/register` and `/install` already have. `mode` only changes the heading ("Create your account" vs "Welcome back") and which link appears at the bottom — the form and the API calls are identical.

**Step 3 — Capture the name *after* verification, not before (fixes A-2).**
Add `GET /welcome`, rendered only for a user whose `name` is empty, asking for the name and nothing else. It saves via your existing profile-update endpoint, then redirects to `/dashboard`. This is strictly better than the current form: you have a verified user row to write to, the name is a post-signup step rather than a pre-OTP barrier, and drop-off after OTP is near zero. If `name` is still empty on a later visit, `/welcome` can be shown again.

**Step 4 — Update the four inbound redirects** in `app.py` (`/`, `/join`, `/download-app`) and `install.html` to point at `/signup` instead of `/register`. `register_url_with_ref()` in `auth_routes.py:109` is the single place the URL is built — change the literal there and three of the four follow automatically.

**Step 5 — Update outbound links.** `install.html:83` (`/register`), `/api/refresh/silent`'s `login_url` (`app.py:498`), and `decorators.py:26`'s `url_for('auth.login')` — note that endpoint does not currently exist, so that `flash`-and-redirect branch would raise `BuildError` if a user row ever went missing. Point it at `/login`.

**Step 6 — Delete `register.html`** once nothing references it.

### Ordering

Do **A-1** first and alone — if OTP verification is not actually verifying, nothing else on this list matters. Then **A-3** and **A-4**, which are one-line changes with outsized effect. Then the migration above, which naturally absorbs **A-2**, **A-5** and **A-13**. **A-6** and **A-7** are small and independent; fold them in wherever convenient.

### Risk

Every step above is confined to `routes/public_routes.py`, `routes/auth_routes.py` (URL constants and the two page routes), `app.py`'s redirect literals, and the `templates/public/` auth pages. No change to `verify_otp`'s logic, the JWT configuration, the refresh path, the blocklist, or any non-auth blueprint. The one behavioural change users will notice is that the name is asked for after the OTP instead of before it — which is also the only way that field has ever actually been saved.
