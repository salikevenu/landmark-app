# LANDMARK Architecture Map (as implemented)

**Audit date:** 2026-08-14  
**Scope:** GitHub repo `salikevenu/landmark-app` (this Flask backend). No production DB was queried. Findings are from source, templates, config, and deploy files.  
**Constraint:** This document describes current truth. It does not propose a rewrite.

---

## 1. What this repository is

This repository is a **Flask server-rendered web app**, not a native mobile app and not a separate SPA.

| Layer | Reality |
|---|---|
| Frontend | Jinja2 HTML templates + inline JavaScript. Dark/neon UI in `templates/`. |
| Mobile | PWA bits (`static/manifest.json`, `static/sw.js`). APK download routes exist; **no Android/iOS source** in this repo. |
| Backend | Flask (`app.py`) + blueprints under `routes/`. |
| Data | SQLAlchemy `text()` SQL against PostgreSQL in production. No ORM models. |
| Auth | Flask-JWT-Extended (cookies **and** headers). OTP via Message Central. |
| Payments | Razorpay Checkout on `/pricing`. |
| Deploy | Render (`start.sh` → Gunicorn `app:app`), GitHub Actions import-check only. |

---

## 2. Runtime topology

```
Browser (Jinja + fetch)
    │  cookies (httpOnly JWT) AND/OR localStorage Bearer
    ▼
Gunicorn (1 sync worker)  ← start.sh binds 0.0.0.0:$PORT
    ▼
Flask app.py
    ├── register_routes()  (routes/__init__.py)
    ├── extra routes in app.py (wallet overview, dummy webhook, payout, /map)
    ├── JWTManager
    ├── DummyLimiter (rate limit disabled)
    └── Razorpay client (extensions.py)
            │
            ▼
    database/init_db.py  engine  (sslmode=require)
            ▼
    PostgreSQL (DATABASE_URL)

External:
    Message Central CPaaS  (OTP)
    Razorpay               (orders / checkout)
    OpenStreetMap / Carto  (map tiles, client-side)
```

`MasterAgent` / APScheduler is **disabled** at boot (`master_agent = None`). `orchestration_routes.py` is **not registered**.

---

## 3. Frontend / UI map

| Area | Templates | Entry URLs |
|---|---|---|
| Public | `templates/public/index.html`, `login.html`, `register.html`, `browse.html`, `nearby.html` | `/`, `/api/auth/public/login`, `/register` |
| App chrome | `templates/layouts/layout_app.html` | all logged-in pages |
| Dashboard | `templates/users/dashboard.html` | `/dashboard` → `/api/user/dashboard` |
| Profile | `templates/users/profile.html` | `/profile` → `/api/user/profile` |
| Map | `templates/map.html` (canonical), `templates/nearby/map.html` (legacy) | `/map`, `/browse` |
| List browse (cards) | `templates/users/browse.html` | `/api/user/browse` |
| Create listing | `templates/users/create_listing.html` | `/create-listing` → `/api/user/create-listing` |
| My listings | `templates/users/my_listings.html` | `/my-listings` → `/api/listing/my-listings` |
| Pricing | `templates/users/pricing.html` | `/pricing` |
| Wallet | `templates/users/wallet.html` | `/wallet` |
| Invite | `templates/users/invite.html` | `/invite` → `/api/user/invite` |
| Promotions | `templates/promotions/index.html` | `/promotions/` |
| Analytics | `templates/analytics/index.html` | `/analytics/` |
| Reviews | `templates/reviews/index.html` | `/reviews/` |
| Transactions | `templates/transactions/index.html` | `/transactions/` |
| Services | `templates/services/add_service.html`, `my_services.html` | `/service/add` |
| Admin | `templates/admin/*` | `/admin/*` |
| Legal | `privacy.html`, `terms.html` | `/privacy`, `/terms` |

There is **no** React/Vue/Flutter/Kotlin/Swift app in this repo.

---

## 4. Backend route map (registered vs orphaned)

### Registered in `routes/__init__.py`

| Blueprint | Prefix | Purpose |
|---|---|---|
| `public_bp` | none | `/register`, `/public/login` |
| `auth_bp` | `/api/auth` | OTP send/verify, login page |
| `listing_bp` | `/api/listing` | CRUD listings, reviews, clicks |
| `nearby_bp` | `/api/nearby` | nearby search + map APIs |
| `payment_bp` | `/api/payment` | Razorpay order/verify/webhook |
| `admin_bp` | none | `/admin/*` and `/api/admin/*` |
| `user_bp` | `/api/user` | dashboard, profile, browse, invite, another verify-payment |
| `geo_bp` | none | `/api/distance` |
| `service_bp` | `/service` | add/list services |
| `promotions_bp` | `/promotions` | ads UI + onboard API |
| `analytics_bp` / `analytics_api_bp` | `/analytics`, `/api/analytics` | listing view/click totals |
| `review_bp` / `reviews_api_bp` | `/reviews`, `/api/reviews` | owner review dashboard |
| `transaction_bp` / `transactions_api_bp` | `/transactions`, `/api/transactions` | wallet tx list |
| `wallet_bp` | none | `/wallet`, `/api/wallet/*`, `/api/withdraw` |

### Also defined on `app.py` (duplicates / internals)

- `/map`, `/browse`, `/wallet`, `/pricing`
- `/api/wallet/overview` (duplicate of wallet blueprint)
- `/api/add-business` (legacy `businesses` table)
- `/api/payment/webhook` — **dummy** `{status: ok}`
- `/internal/saturday-payout`
- `/api/refresh`

### Present on disk, **not registered**

| File | Consequence |
|---|---|
| `routes/referral_routes.py` | leaderboard, nearby-leads, invite-business, referral/info **unreachable** |
| `routes/withdraw_routes.py` | `/api/withdraw/request` **404** (wallet UI calls this) |
| `routes/heatmap_routes.py` | heatmap API unused |
| `routes/language_routes.py` | unused (`/set-language` lives on `app.py`) |
| `routes/orchestration_routes.py` | agent workflows unused |
| `auth/otp_service.py` | Fast2SMS in-memory OTP — unused by live login |
| `database/connection.py` | unused; live engine is `database/init_db.py` |

---

## 5. Services vs agents vs routes

**Live business logic** is mostly in `services/` plus SQL inside route handlers.

| Service | Used by |
|---|---|
| `sms_service.py` | `auth_routes` (Message Central) |
| `payment_service.py` | `payment_routes.verify-payment` |
| `referral_commission.py` | payment webhook + `user_routes.verify-payment` (not the pricing checkout path) |
| `wallet_service.py` | wallet + payment_service credit/debit |
| `listing_service.py` | mixed; create path in `listing_routes` does its own SQL |
| `nearby_service.py` | `/api/nearby/nearby` |
| `admin_service.py` | admin APIs |
| `geo_service.py` | nearby friends |
| `user_service.py` | some user ops |
| `audit_service.py` | admin actions |

**Agents** (`agents/*`, `master_agent.py`) are a parallel unfinished architecture. Boot disables MasterAgent. Do not treat agents as production.

---

## 6. Database layer

- **No SQLAlchemy models.** Schema is `CREATE TABLE IF NOT EXISTS` in `database/init_db.py`.
- Production: PostgreSQL via `DATABASE_URL`.
- Engine always sets `sslmode=require` (PostgreSQL-oriented; SQLite in `.env.example` / CI is inconsistent).
- `init_db()` runs in a **background thread on Render**, not at import time.
- Ad-hoc migrations in `migrations/` and admin “run-migration” endpoints. **No Alembic revision chain in use.**
- Dual tables: `businesses` (legacy) vs `listings` (current); `payments` vs `payment_transactions`; `users.wallet_balance` vs `wallet_balance`.

---

## 7. Authentication architecture (two sessions)

1. **OTP login** (`templates/public/login.html` → `/api/auth/verify-otp`) sets **httpOnly JWT cookies**. JSON body does **not** include tokens. Comment in login JS: “nothing to store client-side.”
2. **Most app pages** read `localStorage.access_token` and send `Authorization: Bearer`.
3. JWT config: `JWT_TOKEN_LOCATION = ["cookies", "headers"]`, `JWT_COOKIE_SECURE = True`, `JWT_COOKIE_CSRF_PROTECT = True`.
4. Refresh cookie path is `/token/refresh` but refresh route is `/api/refresh` — refresh cookie is **not sent** to the refresh endpoint.

Result: cookie-authenticated pages can work; Bearer-only pages fail after OTP login unless something else wrote localStorage.

---

## 8. Payment / subscription architecture (two verifiers)

| Path | Plan IDs | Activate fields |
|---|---|---|
| Pricing UI → `/api/payment/create-order` → `/api/payment/verify-payment` | `"Service Provider"`, `"Business Basic"`, `"Business Premium"` | `activate_subscription()` sets **`role = plan name`**, `subscription_status`, `subscription_expiry`. Does **not** set `users.plan`. |
| `/api/user/verify-payment` | `"service"`, `"basic"`, `"premium"` | Sets `role` + `plan` + `business_limit`. Pricing UI **does not call this**. |

Listing create checks `users.plan` in `business_basic` / `business_premium` / `service_provider` form. Those values are not what pricing verification writes.

---

## 9. Business system

Canonical entity is **`listings`**, not `businesses`.

Flow intended: paid user → create listing (pending) → admin approve → map/search.

Subscription gate exists on `POST /api/listing/create-listing`. Frontend posts to **wrong URL** (`/api/listing/api/create-listing`). Promotions and `/api/add-business` still use `businesses`.

---

## 10. Configuration / env

Required at boot: `SECRET_KEY`, `JWT_SECRET_KEY`, `DATABASE_URL`.

Also used: `RAZORPAY_*`, `MESSAGE_CENTRAL_*`, `DEBUG_SMS`, `ADMIN_SECRET`, `SATURDAY_PAYOUT_SECRET`, `BASE_URL`, `REDIS_URL` (imported, not required for boot), `RENDER`, `PORT`.

`.env.example` still shows `DATABASE_URL=sqlite:///landmark.db`.

---

## 11. Tests and deploy

- CI (`.github/workflows/test.yml`): import `app` with dummy secrets + SQLite. No feature tests.
- `test_agents.py`: agent smoke script, not CI.
- `render.yaml`: Python 3.11.9, `bash start.sh`.
- Gunicorn: 1 worker, timeout 120s.

---

## 12. Deprecated / duplicate inventory (summary)

- `businesses` table vs `listings`
- `auth/otp_service.py` vs `services/sms_service.py`
- Two payment verify routes and two plan-name vocabularies
- Two wallet overview routes
- Two map templates
- Two browse UIs (`/map` vs `/api/user/browse`)
- Two withdraw APIs (one unregistered)
- `agents/` vs `services/`
- `database/connection.py` vs `database/init_db.py`
- Dummy `/api/payment/webhook` vs signed `/api/payment/razorpay/webhook`
- `#firebase_client.py` leftover
