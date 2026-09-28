# LANDMARK Security Audit

**Audit date:** 2026-08-14  
**Mode:** read-only source review. No exploits, no payload crafting, no secret dumping.

This is a defect list for the maintainers. It is not an authorization to attack the live site.

---

## Severity summary

| Severity | Count (approx.) | Examples |
|---|---|---|
| Critical | 5 | Unauthenticated Razorpay order; dummy webhook; verify JWT optional + fake user; payment success not activating correct entitlements; JWT refresh cookie never sent |
| High | 7 | Rate limit disabled; CSRF vs Bearer split; admin identity mismatch; impersonate; error bodies leak exceptions; commission/wallet race; register/open payment proof |
| Medium | 8 | OTP DEBUG_SMS; XSS via unescaped listing HTML in some templates; APK/QR; SSL on engine; session vs JWT dual; missing input validation |
| Low | several | Fast2SMS dead file with placeholder key; verbose SMS logs |

---

## 1. JWT and sessions

**Configured**

- Access 2 h, fixed (remember_me no longer lengthens it; was 30 days)
- Refresh 7 days (or 30 if remember_me; was 365 until 2026-09-28). `JWT_REFRESH_TOKEN_EXPIRES` = 7 days is only the fallback; login always passes an explicit `expires_delta`
- Locations: cookies + headers
- Cookie secure + CSRF protect
- Flask `session` also used for language; `PERMANENT_SESSION_LIFETIME` 10 years

**Issues**

1. **Two client contracts.** OTP login sets httpOnly cookies and does not put tokens in JSON. Most templates send `Authorization: Bearer ${localStorage.access_token}`. After a normal login, Bearer is empty. Some fetches omit Bearer but use `credentials: 'include'` (layout profile) — those can work. Create listing / invite / map / admin JS often require Bearer.

2. **Refresh cookie path** `JWT_REFRESH_COOKIE_PATH="/token/refresh"` vs route `POST /api/refresh`. Browser will not attach the refresh cookie. `static/js/auth.js` also POSTs JSON `refresh_token` which the route does not read (`@jwt_required(refresh=True)`).

3. **`JWT_COOKIE_SECURE=True` globally.** Cookies will not stick on plain HTTP localhost.

4. **Identity is string user id**, but `get_admin_info()` looks up `WHERE phone = :phone` using that identity. Admin actions may no-op or attach the wrong actor.

5. **Remember-me 30-day access token** in cookie + localStorage copies elsewhere increases theft window.

---

## 2. OTP

**Good**

- Phone normalized to 10 digits, Indian 6–9 prefix
- OTP stored as Message Central `verificationId` in Postgres, not the OTP itself (when not debug)
- Attempt cap 3 (was 5), then a 15-minute per-number lock (2026-09-28)
- Parameterized SQL

**Issues**

- `DEBUG_SMS=true` accepts any OTP (`verify_otp` returns True). Fatal if set on Render.
- Cooldown is “row still unexpired (60s)” but API message says 30s.
- New user insert uses `role='free'` with no fraud_check on verify.
- `auth/otp_service.py` Fast2SMS + in-memory OTP is unused but contains a fake API key pattern — ignore, do not revive.
- SMS service logs request URL/headers (token redacted) and **full response body**.

---

## 3. Rate limiting

`extensions.py` installs **DummyLimiter** (`return lambda x: x`) with log line “DISABLED for testing”.

Flask-Limiter in requirements is not enforcing.

In-memory `middleware/rate_limit.py` is only referenced by **unregistered** heatmap routes.

OTP, payment, withdraw, admin SMS have no production rate limit.

---

## 4. Authorization / access control

| Role intended | How enforced | Gap |
|---|---|---|
| Normal user | JWT | `role='free'` on signup |
| Business owner | listing create checks `users.plan` + expiry | Payment writes different fields; frontend URL wrong |
| Active subscriber | `is_subscription_active` **inconsistent** (`app.py` treats `plan==free` as active; listing treats free as inactive) | Same helper name, opposite meaning |
| Expired | listing 403; `requires_active_plan` tries demote | Demote uses `fromisoformat` vs expiry stored `%Y-%m-%d`; redirect to `user.pricing` may 404 |
| Admin | JWT claim `role==admin` | Claim issued at login; DB role change without new token; admin info lookup broken |

Frontend hiding of pricing buttons is **not** access control. Backend listing create **does** check subscription — but the check will not match Razorpay-activated accounts.

`/api/payment/verify-payment` is `@jwt_required(optional=True)` and if missing identity uses `request.json.user_id` default **`test_user_001`**.

`/api/payment/create-order-debug` has **no authentication**.

`/api/payment/submit-payment-proof` has **no authentication**.

Public: listing rate, click-call, click-whatsapp — no auth (click fraud).

---

## 5. SQL injection

Route SQL uses SQLAlchemy `text()` with bound params in the files reviewed (`:uid`, `:phone`, etc.). That is the right pattern.

Residual risk: string-built WHERE in admin listing filters (`admin_service` f-strings for optional status). Treat as medium until each f-string is confirmed bound, not concatenated from raw user input.

---

## 6. XSS

- Map popups use `escapeHtml` — good.
- Several admin and listing templates interpolate `business_name` into HTML with `escapeHtml` in browse/my_listings — good there.
- `create_listing` success uses innerHTML with static strings.
- Any template that does `${biz.name}` without escape (browse uses `escapeHtml(biz.name)` in title — good).
- Jinja autoescape is on by default for HTML — good for server-rendered text.

Risk remains in inline JS building HTML from API JSON without escape (promotions, analytics). Review those before calling UI complete.

---

## 7. CSRF

Cookie JWT CSRF is **on**. Pricing sends `X-CSRF-TOKEN` from `csrf_access_token` cookie — correct for cookie auth.

Bearer-only POSTs are not CSRF-vulnerable in the classic cookie sense, but login is cookie-based, so mixed pages that use `credentials: include` **without** CSRF header can fail (403) or, if CSRF skipped for some paths, be CSRF-able.

Logout link `/api/auth/logout` as GET in sidebar is a CSRF logout risk if that route GET-clears cookies.

---

## 8. Secrets and env

- Required: `SECRET_KEY`, `JWT_SECRET_KEY`, `DATABASE_URL`
- Razorpay keys optional at import; missing → client None → payments 500
- `RAZORPAY_WEBHOOK_SECRET` required only on signed webhook
- `SATURDAY_PAYOUT_SECRET`: if **unset**, `Bearer None` may be guessable/miscompared — treat as critical to set
- `.env` is local; must stay gitignored
- `#firebase_client.py` leftover

Do not commit `.env`. CI uses dummy secrets (good).

---

## 9. Payments

> **Superseded:** this table is the 2026-08-14 state. See section 16 (re-audit 2026-09-29) for the current status of sections 9 and 10.

| Control | Status |
|---|---|
| Order amount from server PLAN_PRICES | yes on `/create-order` |
| Signature verify | yes in `verify_payment_service` |
| Amount vs Razorpay order | yes |
| Order status must be paid | yes |
| Duplicate payment_id | attempted in `process_payment` |
| Authenticated payer | **optional JWT** on verify |
| Webhook signature | on `/razorpay/webhook` only |
| Dummy webhook | `/api/payment/webhook` returns ok **with no signature** |
| Refunds | not implemented |
| Idempotent subscription activate | no unique constraint per user/period |

Wallet credit-then-debit can leave money in wallet if debit fails after credit.

---

## 10. Referral / wallet abuse

- `referred_by` is set on **new** OTP signup from `pending_referrals`; existing users are never reassigned
- Live commission is 10% first + 5% recurring via `referral_commission.py`; `process_referral` 20% and flat ₹ rewards are disabled
- Withdraw UI not connected; registered `/api/withdraw` debits immediately then inserts pending (user loses balance even if admin never pays)
- No server-side UPI format validation
- IP fraud_check unused on payment/signup

---

## 11. Admin

- Impersonate endpoint issues user token — high risk if admin JWT is stolen
- `/api/send-sms` and `/api/send-otp` under admin — extra SMS cost/abuse
- Admin pages store `access_token` in localStorage (XSS → full admin)

---

## 12. Error leakage

`app.py` error handler:

```python
return jsonify({"error": str(e)}), 500
```

`/api/readiness` returns exception string. Payment create-order returns `type` and `str(e)`.

---

## 13. Uploads

Avatar and listing images saved under `static/uploads` with `secure_filename` plus timestamp. `MAX_CONTENT_LENGTH` 20MB. No evident virus scan or strict MIME allowlist. Files are publicly fetchable if URL is known.

---

## 14. What is relatively sound

- Parameterized SQL in core OTP/listing/nearby queries
- Message Central verification ids rather than storing OTP (non-debug)
- Razorpay signature + amount check **when** the real verify path runs with a real JWT
- Security headers: nosniff, DENY, XSS-Protection
- Listing create **does** attempt subscription enforcement on the backend (even if plan values are wrong)

---

## 15. Remediation log: auth hardening pass (2026-09-28, branch `audit/auth`)

**JWT / sessions**

- `JWT_SECRET_KEY` hardcoded fallback (`"your-secure-jwt-secret-key"`) removed from `app.py`; the app refuses to start (`RuntimeError`) when it is unset. Tests get a test-only secret from `tests/conftest.py`.
- Access token fixed at 2 h (`ACCESS_TOKEN_TTL`), also for tokens minted by `/api/refresh` and `/api/refresh/silent`. Remember-me lengthens only the refresh token: 30 days (was 365). Default refresh 7 days.
- `JWT_REFRESH_TOKEN_EXPIRES` restored to 7 days, so a refresh token minted without `expires_delta` never gets remember-me's lifetime. A test fails if any production `create_refresh_token` call omits `expires_delta` (today there is one: `routes/auth_routes.py::generate_jwt_tokens`).
- TTL constants (`ACCESS_TOKEN_TTL`, `DEFAULT_REFRESH_TTL`, `REMEMBER_ME_REFRESH_TTL`) now live in `services/jwt_session.py`, shared by `app.py` and `routes/auth_routes.py`.
- Refresh cookie path `/api/refresh` → `/` so logout requests carry the refresh token and can blocklist it. `JWT_REFRESH_CSRF_COOKIE_PATH` is `/` (it always was).
- All logout paths (`/logout`, `/api/auth/logout` GET/POST, `/api/user/logout`) use `clear_auth_cookies()`, which also expires the legacy `/api/refresh` refresh cookie with the same domain/secure/samesite/httponly attributes as when it was set. No legacy CSRF-cookie delete is needed.

**OTP**

- Max wrong attempts 5 → 3. An admin setting may lower it, never raise it (runtime ceiling). Hitting the cap locks the number for 15 min: verify, send and resend all return 429 `OTP_LOCKED`.
- Message Central 506 `REQUEST_ALREADY_EXISTS` (same live code) no longer resets the wrong-attempt counter.
- OTP expiry setting capped at 300 s.
- Per-number SMS limit is now shared by send-otp and resend-otp: 5/hour in total, counting only requests that sent (HTTP 200). Previously each endpoint allowed 5/hour on its own (10 combined).
- Existing databases: run `python -m migrations.set_otp_max_attempts_3` once, because the `init_db` seed is `ON CONFLICT DO NOTHING`.

**Rate limiter when Redis is unavailable** (predates this branch; separate commit)

- Before: Redis down at boot → silently pinned to per-process memory until restart. Redis down after boot → every rate-limited endpoint (OTP, `/api/refresh`) returned 500.
- Now: the limiter always uses `REDIS_URL` when set, with Flask-Limiter's `in_memory_fallback_enabled`. Whether Redis is down at boot or later, each route's own limits keep applying in process memory, and the limiter switches back to Redis when it answers again (backoff up to ~32 s). Both transitions are logged at WARNING. `swallow_errors` is deliberately not used, since it would skip limiting entirely.
- Redis socket timeouts are 2 s, so a hung Redis can't stall request threads.
- While in fallback, counters are per process and start from zero, so limits are looser across instances or restarts. With `workers = 1` that only matters if Render runs more than one instance.
- Residual: if Redis fails in the instant between a request's limit check and its deferred deduction (`deduct_when`, used by the OTP send limit), that one request can still return 500.

**Open / not done in this pass**

- ~~Verify-attempt check is read-then-increment, not atomic.~~ **Fixed 2026-09-29:** `verify_otp` claims each attempt with one atomic `UPDATE … WHERE attempts < max RETURNING` (`reserve_attempt`) before the provider sees the guess, so concurrent guesses can't exceed the cap. Every claimed attempt counts, including a provider error or timeout (as before). Covered by a concurrent-guess DB test.
- Lockout can be triggered by anyone who knows a phone number (3 wrong guesses → 15 min denial of login). **Follow-up:** lock by phone number + client IP instead of number alone, so a stranger's wrong guesses don't lock the real user out. Needs an `ip` column on `otp_verifications` (schema change + migration), so it's planned as a separate PR.
- `services/admin_service.py` still accepts `otp_max_attempts` > 3 and `otp_verification_expiry_seconds` > 300; runtime silently caps them.
- Logout accepts GET and CSRF-less POST (forced-logout CSRF, low impact).
- `app.secret_key` still has a hardcoded fallback, unreachable because `SECRET_KEY` is in `REQUIRED_ENV_VARS`.

---

## 16. Payments, wallet and referral re-audit (2026-09-29, branch `audit/payments`)

Read-through of `routes/payment_routes.py`, `services/payment_service.py`, `routes/wallet_routes.py`, `routes/withdraw_routes.py`, `services/wallet_service.py`, `services/referral_commission.py` and the internal payout job in `app.py`.

**Fixed since the 2026-08-14 audit (verified in code)**

| Old finding | Now |
|---|---|
| `/create-order-debug` unauthenticated | Returns 404, creates nothing (`payment_routes.py`) |
| `/verify-payment` optional JWT, `test_user_001` fallback | `@jwt_required()`; the order must belong to the caller (payments row + Razorpay notes) |
| `/submit-payment-proof` unauthenticated | `@jwt_required()`, returns 404 |
| Unsigned `/api/payment/webhook` | Now a stub in `app.py` that always returns 403 and records nothing; payments are only activated by the HMAC-verified `/api/payment/razorpay/webhook` (constant-time compare, 503 if secret unset) |
| Activation not idempotent | Payment row locked `FOR UPDATE`; activates only from a pre-activation status, then compare-and-set to `activated`; replays return `duplicate` without extending expiry |
| Verify trusts client data | Signature, Razorpay order `paid`, payment `captured` and matching order, amounts equal to the stored row, plan and duration from the stored row |
| Wallet credit-then-debit leaves money behind | Withdrawal reserves with a row lock + compare-and-set debit (never negative), in one transaction with the request and ledger row |
| Withdraw debits immediately, lost if never paid | Debit is a reservation; reject refunds exactly once (unique refund ledger index); approve does not debit again |
| Duplicate withdrawal submits | Idempotency key backed by a unique `(user_id, reference_id)` index |
| Commission double-pay | One job per payment (`UNIQUE (payment_id)`), one commission row per payment, only for `activated` payments, self-referral blocked, payout release `SKIP LOCKED` + compare-and-set |
| `SATURDAY_PAYOUT_SECRET` unset → `Bearer None` | Unset secret denies; must differ from app/JWT secrets; constant-time compare |

**Fixed in this pass**

- `verify_extra_business_payment` (reachable via legacy `POST /api/user/verify-payment` with `plan=extra_business`) inserted a payments row for any paid ₹249 Razorpay order it had no record of, as long as the order had no `user_id` note. Not exploitable today, since no other product costs ₹249, but a future ₹249 product or dashboard payment link would have become a free listing slot. It now requires a row created by `/create-order`.
- Withdrawal UPI IDs are validated as `handle@provider` before any balance is reserved (was: any non-empty text ≤ 120 chars).
- `GET /api/payment/wallet-transactions` used `SELECT *`, exposing the referred user's `razorpay_payment_id` on a referrer's commission rows. Now uses explicit columns, the same as `/api/wallet/transactions`.

**Open: product decisions**

- **Early renewal loses remaining days.** Activation sets expiry to *now + billed duration*, never extending the current expiry (`payment_service._expiry_date`). A user with 20 days left who renews monthly ends with 30, not 50. Buying a lower plan while on a higher one also downgrades immediately.
- **Refunds and chargebacks are not handled.** Refund webhooks are ignored. A refunded payment keeps the subscription active, and its referral commission unlocks the next Saturday 18:00 IST (sometimes less than a day later) and can then be withdrawn.
- **Pending withdrawals have no timeout or alert.** Reserved money stays out of the user's balance until an admin approves or rejects.

**Open: low**

- Recurring 10% commission can be earned on one's own purchases through a second account (sock-puppet referral); withdrawal requires one paid business referral, which a second account can also satisfy.

---

## 17. Do not do in this pass

No secret rotation from this document, no firewall changes, no data deletion. Fix order is in `LANDMARK_IMPLEMENTATION_ROADMAP.md`.
