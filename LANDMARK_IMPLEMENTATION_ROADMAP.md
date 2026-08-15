# LANDMARK Implementation Roadmap

**Audit date:** 2026-08-14  
**Rules:** Do not rewrite the app. Do not delete working code until a replacement is wired. Do not fake completeness. POS/catalog stay future until listing + subscription actually work.

This roadmap is sequenced so each step makes an existing path true end-to-end.

---

## CURRENTLY COMPLETE

- Process can boot on Render (`bash start.sh`, Gunicorn bind `$PORT`)
- Health endpoints
- Public landing page
- Message Central OTP send/verify (cookie JWT issued)
- Map page + nearby businesses/search APIs (data-dependent)
- Invite page UX (share / copy toast / WhatsApp) given a referral code
- Partial i18n
- Basic security headers
- Canonical Razorpay verify/activation mapping (unit-tested; live charge not run)
- Cookie JWT session helper + frontend localStorage token gates removed (unit-tested; **browser verified 2026-08-15**)

---

## PARTIALLY COMPLETE

- Profile, dashboard, reviews UI, transactions UI
- Listings data model + admin listing screens
- Pricing page + Razorpay order creation (needs staging payment)
- Wallet tables + overview API
- Admin panel shells
- Analytics UI (counters not written)
- Voice search in browse (browser API only)

---

## BROKEN

- Wallet transactions query (`description`)
- Withdraw UI route unregistered
- `call_clicks` / `services` / `interactions` columns
- Register API missing

---

## MISSING

- Catalog/products, POS, inventory, in-app chat, ambassador tree, real ads checkout, referral attribution, native apps

---

## LEGACY / DUPLICATE (consolidate later, do not delete first)

- `businesses` vs `listings`
- `/api/user/verify-payment` delegates to canonical verifier (extra_business still local)
- Two wallet overviews
- Two map templates
- Unregistered `referral_routes` / `withdraw_routes` / `heatmap_routes` / agents
- Fast2SMS otp_service

---

## CRITICAL BLOCKERS

1. Referral 10%/5% still not attached to signup.
2. Confirm one Razorpay test-mode payment on staging before live keys.

---

## NEXT FEATURE (one)

**Feature #3 — Referral attribution (existing invite/commission path, not a new product).**

OTP/login must persist `users.referred_by` from `?ref=` / invite code, and wallet `source` values must match Saturday payout. Do not build catalog or POS.

---

## MISSING

- Catalog/products, POS, inventory, in-app chat, ambassador tree, real ads checkout, referral attribution, feature tests, native apps

---

## LEGACY / DUPLICATE (consolidate later, do not delete first)

- `businesses` vs `listings`
- Two payment verifiers
- Two wallet overviews
- Two map templates
- Unregistered `referral_routes` / `withdraw_routes` / `heatmap_routes` / agents
- Fast2SMS otp_service

---

## CRITICAL BLOCKERS

1. Users who log in with OTP cannot use Bearer-only screens.
2. Create-listing POST URL is wrong.
3. Referral 10%/5% is not attached to signup.
4. Confirm one Razorpay test-mode payment on staging before live keys.

---

## Recommended sequence (after this audit, when coding is allowed)

### Step 1 — Session that all existing pages can use

- On OTP verify JSON, include `access_token` / `refresh_token` **and** keep cookies.
- Login JS stores them in localStorage (many pages already read them).
- Fix refresh path **or** send refresh via header consistently.
- Do not switch architecture.

### Step 2 — Payment activation (DONE 2026-08-14)

Canonical `/api/payment/verify-payment` now requires JWT, verifies Razorpay signature/amount from the server-side order, writes `plan`+`role` snake_case, 30-day expiry, `{success: true}`. Debug order 404. Dummy webhook 403.

### Step 3 — Listing create actually submits

- Change `create_listing.html` to `POST /api/listing/create-listing` (or add a compatibility alias). Do not rebuild the form.

### Step 4 — Referral attribution

- Accept `?ref=` on login/register and set `users.referred_by` to the referrer’s **id**.
- Align wallet `source` with `/internal/saturday-payout`.
- Leave locked-until-Saturday logic in `referral_commission.py`.

### Step 5 — Wallet withdraw one path

- Point `wallet.html` at the registered `POST /api/withdraw` **or** register `withdraw_bp`.
- Fix `get_wallet_transactions` column list.
- Do not debit until policy is explicit (pending vs immediate debit).

### Step 6 — Stop fake completeness

- Promotions: stop hardcoded `listing_id=1` and fake analytics.
- Analytics: increment `views` on listing/map open.
- Do not add catalog/POS/AI.

### Step 7 — Tests

- One pytest: create order amount, verify rejects bad signature, verify with mock sets plan.
- One pytest: listing create 403 without plan, 200/pending with plan.

---

## Explicitly out of scope until the above is true

- LANDMARK POS
- Product catalog
- Ambassador hierarchy
- Agent/orchestration revival
- SQLite → Postgres data migration
- Deleting `businesses` table
- Rewriting templates into a SPA

---

## Feature traces (selected)

### A. OTP login (partial)

USER enters phone → `login.html` → `POST /api/auth/send-otp` → `sms_service` → Message Central → `otp_verifications`  
USER enters OTP → `POST /api/auth/verify-otp` → Message Central validate → insert/select `users` → set JWT **HttpOnly cookies** → redirect `/dashboard`  
Frontend uses `credentials: include` + CSRF; does not require localStorage.  
**Status:** browser-verified 2026-08-15.

### B. Pay for Business Basic (canonical path, 2026-08-14)

USER `/pricing` → `POST /api/payment/create-order` `{plan: "Business Basic"}` (JWT) → Razorpay Checkout  
→ `POST /api/payment/verify-payment` (JWT, no trusted frontend plan) → signature + Razorpay order amount + `payments` row  
→ `users.plan=business_basic`, `role=business_basic`, expiry +30 days, `business_limit=1`  
→ JSON `{success: true, status: "success", redirect: "/dashboard"}`  
**Remaining:** browser-verify cookie session on create-listing submit.

### C. Map discover (partial, closest to complete)

USER `/map` → Leaflet Carto tiles → `GET /api/nearby/businesses` → `listings` where active + coords → markers / flyTo search  
**Break:** empty DB / pending listings / auth optional.

### D. Referral 10% + 5% (not connected)

Invite UI shows code → `/register?ref=` → **register API missing**; OTP verify **does not read ref** → `referred_by` null → `process_referral_commission` no-ops  
Webhook path can call commission but dummy webhook is what is mounted at `/api/payment/webhook`.

### E. Withdraw (not connected)

USER wallet form → `POST /api/withdraw/request` → **blueprint not registered** → 404  
Registered `POST /api/withdraw` debits immediately.

---

## Access control truth

| Actor | DB fields | Backend lock | Frontend lock |
|---|---|---|---|
| Normal / free | `role='free'`, `plan` default `free` | listing create 403 if `plan==free` | pricing eligibleRoles |
| Business after Razorpay verify | `plan`+`role` snake_case, expiry | listing API allows if not expired | pricing hides lower plans |
| Expired | `subscription_expiry` past | listing 403; page decorator demotes | weak |
| Admin | `role` claim `admin` | `admin_required` | `/admin/login` |

Backend listing create still enforces plan+expiry. Feature #1 stops here.
