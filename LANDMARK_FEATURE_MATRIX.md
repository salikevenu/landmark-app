# LANDMARK Feature Matrix

**Legend**

| Symbol | Meaning |
|---|---|
| ✅ COMPLETE | UI → API → DB → external service works as claimed |
| 🟡 PARTIALLY IMPLEMENTED | Real code exists but a required link is missing or inconsistent |
| 🔴 NOT IMPLEMENTED | No real end-to-end implementation |
| ⚠️ IMPLEMENTED BUT BROKEN | Code exists and would fail or mis-apply in production |
| 🔵 IMPLEMENTED BUT NOT CONNECTED | Pieces exist but are not wired together |
| 🟣 DUPLICATED/LEGACY | Multiple overlapping implementations |

A feature is **not** complete because a file, table, button, or route exists.

---

## Matrix

| Feature | Status | Frontend | Backend | Database | External API | Auth | Subscription | Tests | Problems | Recommended next action |
|---|---|---|---|---|---|---|---|---|---|---|
| Public home / marketing | 🟡 | `public/index.html` | `app.py` `/` | n/a | n/a | public | n/a | none | Static; some CTAs to old paths | Keep; align links to `/map` and `/register` |
| OTP login (Message Central) | ✅ | `public/login.html` | `/api/auth/send-otp`, `/verify-otp` | `otp_verifications`, `users` | Message Central | HttpOnly JWT cookies | n/a | `tests/test_auth_session.py` | 60s OTP row vs 30s UX; `DEBUG_SMS` can bypass; new users `role='free'`; no `referred_by` | Persist referral on signup (Feature #3) |
| Fast2SMS OTP (`auth/otp_service.py`) | 🟣 | unused | unused | in-memory dict | Fast2SMS placeholder key | n/a | n/a | none | Dead code | Do not use |
| JWT session (canonical cookies) | ✅ | `static/js/session.js` + `credentials: include` + CSRF header | Flask-JWT-Extended cookies+headers; refresh `/api/refresh` | n/a | n/a | cookie JWT | n/a | `tests/test_auth_session.py` | Browser verified 2026-08-15; Secure cookie only when RENDER=true | Keep |
| Register page | 🔵 | `public/register.html` posts `/api/auth/register` + optional referral | **no `/register` route on `auth_bp`** | `users.referred_by` unused here | n/a | intended JWT | n/a | none | Register UI cannot create accounts | Implement register **or** fold referral into OTP verify |
| User profile | 🟡 | `profile.html`, layout widget | GET/PUT `/api/user/profile*`, avatar POST | `users.name`, `avatar_url` | n/a | cookie JWT | n/a | none | Avatar disk `static/uploads/avatars` | Keep |
| Dashboard | 🟡 | `dashboard.html` | `/api/user/dashboard` | listings counts likely | n/a | cookie JWT | n/a | none | Action cards mixed `/map` vs old browse | Keep UI; do not treat as product-complete |
| Business listing create | 🟡 | `create_listing.html` posts `/api/listing/create-listing` via cookie session | POST `/api/listing/create-listing` | `listings` status `pending` | n/a | cookie JWT | paid plan + expiry | payment + auth unit tests | Browser submit after OTP **NOT VERIFIED** | Browser-verify Feature #2 then listing e2e |
| Listing edit | 🟡 | `edit_listing.html` cookie session → `/api/listing/update-listing/<id>` | `/api/listing/update-listing/<id>` | `listings` | n/a | cookie JWT | owner role | none | Browser not verified | Browser-verify with Feature #2 |
| My listings | 🟡 | `my_listings.html` | `/api/listing/my-listings-data` | `listings` | n/a | cookie JWT | n/a | none | Browser not verified | Keep |
| Admin listing approve | 🟡 | `admin_listings.html` | `/api/admin/listings/*` | `listings.is_active`, `status` | n/a | JWT `role==admin` | n/a | none | Admin identity lookup uses **phone** vs JWT **user id** | Fix admin identity; require admin role in DB not only JWT claim |
| Nearby / map | 🟡 | `map.html` Leaflet + Carto Voyager + flyTo | `/api/nearby/businesses`, `/search`, `/businesses/<id>` | `listings` lat/lng | OSM/Carto tiles | JWT optional | n/a | none | Empty if no approved geo listings; `/api/nearby/nearby` still JWT+lat | Seed/approve listings; keep map |
| List browse (cards) | 🟡 | `users/browse.html` | `/api/user/api/browse` | `listings` | geolocation | mixed | n/a | none | Duplicate of map; `/browse` now map | Decide one discover UX |
| Categories | 🟡 | Hardcoded filter options in browse | listings.category text | no category master table | n/a | n/a | n/a | none | Not a taxonomy | Add category table only when product needs it |
| Explore / recommendations | 🔵 | none dedicated | `/api/user/api/recommend` uses **`businesses` + `interactions`** | `interactions` **not in init_db** | n/a | none on recommend | n/a | none | Wrong table; missing table | Do not ship as Explore |
| Product / catalog | 🔴 | none | none | no products table | n/a | n/a | n/a | none | Vision only | Defer until listing+subscription works |
| LANDMARK POS / inventory | 🔴 | none | none | none | n/a | n/a | n/a | none | Future | Do not implement yet |
| Advertising / promotions | ⚠️ | `promotions/index.html` | insert `sponsored_ads` with **`listing_id=1`**, fake analytics 1245/329 | `sponsored_ads`; reads **`businesses`** | n/a | JWT | not gated on paid plan in onboard | none | Hardcoded listing; fake metrics; no Razorpay for ads | Stop claiming ads complete |
| Call / WhatsApp | 🟡 | map popups `tel:` / `wa.me`; listing cards | click trackers exist | `whatsapp_clicks`; **`call_clicks` column not in init_db** | WhatsApp | public click APIs | n/a | none | Call increment likely errors; no in-app chat | Add column or stop calling it; keep tel/wa links |
| In-app chat | 🔴 | none | none | none | n/a | n/a | n/a | none | | Defer |
| Subscription plans UI | 🟡 | `/pricing` | `POST /api/payment/create-order` JWT required | `payments` created + `plan` | Razorpay order | cookies + optional Bearer | display → internal | unit | Live checkout not run here | Staging test with Razorpay test keys |
| Razorpay verify (pricing path) | 🟡 | `verifyData.success`; does not send plan for trust | canonical `verify_payment_service`; `{success: true}` | captured payment + `users.plan/role/expiry/limit` | Razorpay signature + order fetch | JWT required | snake_case plan+role, 30 days | 17 unit tests | Live Razorpay not executed in this environment | Staging payment with test keys |
| Razorpay debug create-order | ✅ | none | returns **404** | n/a | none | n/a | n/a | unit | Disabled | Keep disabled |
| Razorpay webhooks | 🟡 | n/a | dummy `/api/payment/webhook` **403**; signed `/api/payment/razorpay/webhook` HMAC | matching order row then activate | Razorpay HMAC | signature | same activator | unit | Point Razorpay dashboard at signed URL | Set `RAZORPAY_WEBHOOK_SECRET` |
| Manual payment proof | 🔵 | none | 404, JWT required | n/a | n/a | JWT | n/a | none | Disabled (schema was wrong) | Leave disabled |
| Referral invite UI | 🟡 | `invite.html` share/copy/WhatsApp | `/api/user/api/invite` | `users.referral_code` | Web Share / wa.me | JWT Bearer | n/a | none | Link `/register?ref=` not consumed by OTP | Attach `referred_by` on first login |
| Referral 10% + 5% | 🔵 | copy on invite page | `process_referral_commission` | locked `wallet_transactions` | n/a | n/a | on payment | none | Not called from pricing verify; first-bonus **source** `5%_base_+_5%_activation` ≠ payout query `referral_first_bonus`; `referred_by` never set on OTP | Wire signup + payment + unlock sources |
| Referral leaderboard / leads | 🔵 | none in app chrome | `referral_routes.py` **unregistered** | `referral_transactions`, `business_leads` | n/a | mixed | n/a | none | Dead blueprint | Register or delete later |
| Wallet overview UI | 🟡 | `wallet.html` | `/api/wallet/overview` (app + blueprint) | `wallet_balance`, locked txs | n/a | JWT | n/a | none | Pending filter misses actual first-bonus source | Align source names |
| Wallet transaction list API | ⚠️ | transactions page uses `/api/transactions/list` | wallet service `get_wallet_transactions` selects **`description`** | no `description` column | n/a | JWT | n/a | none | `/api/wallet/transactions` will 500 | Select existing columns |
| User withdrawal | 🔵 | `wallet.html` → **`/api/withdraw/request`** | that route is on **unregistered** `withdraw_bp`; registered is `POST /api/withdraw` | `withdraw_requests` | UPI manual | JWT | extra rules on unregistered bp | none | UI 404s | Point UI at registered route **after** policy is one place |
| Admin withdrawals | 🟡 | `admin/withdraws.html` | `/api/admin/withdrawals*` | `withdraw_requests` | n/a | admin JWT | n/a | none | Admin identity bug; approve does not always refund/debit consistently across copies | Single withdraw state machine |
| Saturday unlock job | 🟡 | none | `/internal/saturday-payout` | unlocks only `referral_first_bonus` + `referral_recurring` | bearer secret | secret header | n/a | none | First bonus source mismatch; dual write `wallet_balance` and `users.wallet_balance` | Align sources; one balance table |
| Anti-fraud | 🟡 | n/a | `fraud_check` IP>5 users; `agents/fraud_agent.py` unused | `users.ip_address` | n/a | n/a | n/a | none | Not applied to OTP/payment; in-memory rate limit unused (DummyLimiter) | Apply to signup/withdraw after auth works |
| Business Hub | 🔴 | dashboard cards only | no hub module | n/a | n/a | n/a | n/a | none | Marketing name | Do not invent |
| Business analytics | 🟡 | `/analytics` Chart.js | `/api/analytics/data` | `listings.views/clicks` | n/a | JWT | none | none | **views never incremented**; daily chart uses listing `created_at` not events; promotions page uses fake numbers | Increment views on detail/map open |
| AI / voice | 🟡 | browse “Voice search” Web Speech API | none | n/a | browser speech | n/a | n/a | none | Client-only; promotions “AI score” hardcoded 82 | Label as browser voice, not AI product |
| Multilingual | 🟡 | layout language select | `/set-language`; `language/translations.py` | `users.language` | n/a | optional JWT | n/a | none | Partial key coverage | Keep; expand strings later |
| Ambassador hierarchy | 🔴 | none | none | no tree table | n/a | n/a | n/a | none | | Defer |
| Admin panel | 🟡 | HTML shells + fetch | large `admin_routes.py` | users, listings, payments, settings | SMS send from admin | JWT role claim | n/a | none | `get_admin_info()` queries `users.phone = JWT identity` but identity is **user id**; impersonate exists | Fix identity; lock down impersonate |
| Services (service_provider) | ⚠️ | add/my services templates | INSERT into **`services`** | table **not created** in `init_db` | n/a | `requires_active_plan('service_provider')` | role name likely never set by pricing | none | Will 500; wrong role string vs “Service Provider” | Do not use until table+roles exist |
| Heatmap | 🔵 | none | unregistered blueprint | listings | n/a | JWT | n/a | none | | Ignore until needed |
| Distance API | 🟡 | none obvious | `/api/distance` | n/a | n/a | JWT | n/a | none | Isolated | Keep as util |
| PWA / APK | 🟡 | manifest/sw | `/download/android` vs `/download-app` different folders | n/a | n/a | public | n/a | none | APK may be missing in git | Confirm artifact on Render disk |
| Health / readiness | ✅ | n/a | `/ping`, `/api/health`, `/api/readiness` | SELECT 1 | n/a | public | n/a | CI import only | Readiness leaks exception text | Enough for ops |
| Render deploy | 🟡 | n/a | `start.sh` + gunicorn | background `init_db` | Render | env secrets | n/a | CI import | Worker=1; init_db errors swallowed | Keep start command `bash start.sh` |
| Automated tests | 🟡 | n/a | `tests/test_payment_subscription.py` + CI import | mocked / sqlite dummy | Redis unused by CI | dummy secrets | mapped | payment unit + import | CI does not run payment tests yet | Wire unittest into CI |

---

## CURRENTLY COMPLETE

Only features that appear to work for their narrow job:

- Flask boot + Gunicorn bind on Render (`start.sh`)
- `/ping`, `/api/health`, `/api/readiness` (DB ping)
- Public home page render
- Message Central OTP **send/verify** (when env + DEBUG_SMS off) — login cookie issuance
- Map **page render** + Leaflet/Carto (markers only if listings have coordinates)
- Nearby JSON APIs `/api/nearby/businesses` and `/search` (JWT optional)
- Invite **page chrome** (copy toast, Web Share, WhatsApp link) once a referral code is returned
- Language switcher cookie + Jinja `_()` for keys that exist
- Security response headers (nosniff, DENY frame)
- Debug Razorpay order endpoint disabled (404)

Nothing else is classified complete. Paid business, catalog, POS, ads, wallet payout, and referrals are not complete.

---

## PARTIALLY COMPLETE

- OTP login + cookie JWT session helper (unit-tested; browser flow not verified)
- Profile / dashboard / reviews / transactions UIs (cookie session wired; browser not verified)
- Map search + flyTo
- Listing data model and admin listing actions
- Analytics page (reads columns that are mostly never updated)
- Wallet overview (balance tables exist)
- Admin HTML + many APIs
- Multilingual strings
- Promotions UI (display only)
- Razorpay pricing checkout (canonical verify + plan mapping unit-tested; live charge not run here)

---

## BROKEN

- `GET /api/wallet/transactions` (`description` column)
- Wallet withdraw UI (`/api/withdraw/request` unregistered)
- Call click tracker (`call_clicks` missing)
- Service add (`services` table missing)
- Register page → missing API

---

## MISSING

- Product/catalog
- POS / inventory
- In-app chat
- Ambassador hierarchy
- Business Hub product
- Real ad checkout
- Referral attribution on signup
- Event-level analytics
- Feature tests
- Native mobile app source

---

## LEGACY / DUPLICATE

- `businesses` vs `listings`
- Legacy `/api/user/verify-payment` still exists but delegates to canonical verifier (except extra_business)
- Two wallet overview routes
- Two map templates; two browse UIs
- `referral_routes.py` unregistered vs `user_routes` invite
- `withdraw_routes.py` vs `wallet_routes./api/withdraw`
- `agents/` vs `services/`
- Fast2SMS vs Message Central
- `database/connection.py` vs `init_db.py`
- Dummy vs signed Razorpay webhook

---

## CRITICAL BLOCKERS (before real users / real money)

1. Persist `referred_by` on signup; align referral commission sources.
2. Run one live Razorpay **test-mode** payment on staging before enabling live keys.

---

## NEXT FEATURE (exactly one)

**Feature #3 — Referral attribution.** Wire invite `?ref=` into OTP login so `users.referred_by` is set, and align wallet transaction `source` with Saturday payout. Do not start catalog or POS.
