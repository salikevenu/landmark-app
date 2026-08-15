# LANDMARK Database Audit

**Audit date:** 2026-08-14  
**Method:** Source inspection of `database/init_db.py`, `migrations/`, route SQL, and `.env.example`. Production was **not** queried. Do not migrate from this document automatically.

---

## 1. Engine and DATABASE_URL

| Item | Truth |
|---|---|
| Intended production | PostgreSQL (`psycopg2-binary`) |
| Connection | `database/init_db.py` `create_engine(DATABASE_URL, poolclass=NullPool, connect_args={sslmode: require, ...})` |
| Alternate unused engine | `database/connection.py` (pool_pre_ping, no sslmode) |
| Boot | `init_db()` on Render in a **daemon thread**; local skips `init_db` |
| `.env.example` | `sqlite:///landmark.db` — contradicts production engine (`sslmode=require` will not work on SQLite) |
| CI | `DATABASE_URL=sqlite:///test.db` for import test |

**Must change (later, not now):** one engine helper that applies SSL only for `postgres://` URLs; stop documenting SQLite as the default if production is Postgres-only.

---

## 2. Schema ownership

There is **no ORM** and **no live Alembic history**. Schema is:

1. `CREATE TABLE IF NOT EXISTS` in `init_db()`
2. Scattered `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
3. One-off scripts: `migrations/migrate.py`, `migrations/add_admin_tables.py`, `migrations/add_admin_audit_log.py`
4. Admin endpoint `run-migration/withdrawal-policy` adding `had_first_withdrawal`, `active_business_referrals_count`

`CREATE TABLE IF NOT EXISTS` **does not alter** existing production columns. Older Render DBs can miss columns that new `CREATE` statements include.

---

## 3. Tables created in `init_db()`

| Table | Role |
|---|---|
| `users` | accounts, plan, referral, wallet_balance column, geo |
| `wallet_balance` | second balance store |
| `subscriptions` | Razorpay subscription ids — **barely used** by checkout (checkout is one-shot order) |
| `wallet_transactions` | credits/debits/locks |
| `businesses` | **legacy** business rows |
| `referral_transactions` | older referral ledger |
| `withdraw_requests` | UPI withdraw workflow |
| `listings` | canonical businesses/services on the map |
| `listing_images` | photos |
| `business_media` | media for legacy businesses |
| `payments` | order/payment records |
| `payment_transactions` | second Razorpay ledger |
| `sponsored_ads` | promotions |
| `reviews` | ratings + owner_reply |
| `business_leads` | invite-business (blueprint unregistered) |
| `cities` | city launch list |
| `admin_audit_log` | admin actions |
| `admin_settings` | key/value including commission percents |
| `otp_verifications` | Message Central verification ids |

---

## 4. Duplicate / conflicting schemas

### Users: plan vs role vs subscription_status

`users` has `role`, `plan`, `subscription_expiry`.  
`activate_subscription()` also writes `subscription_status` — **column not in `init_db` users CREATE**. If the column was never ALTERed in production, pricing verify can 500 after a successful Razorpay charge.

Pricing sets `role` to `"Business Basic"` etc. Listing limits check `plan == "business_basic"`.

### Two business entities

- `listings` — used by map, listing APIs, analytics
- `businesses` — used by promotions preview, `/api/add-business`, recommend

Promotions insert `sponsored_ads.listing_id = 1` regardless of the user’s listing.

### Two payment ledgers

- `payments`: `user_id, order_id, user_phone, payment_id UNIQUE, amount, status`
- `payment_transactions`: `user_id TEXT, razorpay_order_id, razorpay_payment_id, amount INTEGER, status`

Create-order stores Razorpay **order id** in `payments.payment_id`. Verify/`process_payment` inserts Razorpay **payment id** into the same unique column. Duplicate-protection is therefore “same payment id”, not “same order”.

Manual proof INSERT uses `phone, plan, payment_method, reference_id` — **not in CREATE**.

### Two wallets

- `wallet_balance.balance`
- `users.wallet_balance`

Saturday payout updates **both**. `credit_wallet` / `debit_wallet` update **only** `wallet_balance` (+ a transaction row). UI overview reads `wallet_balance`.

### Referral ledgers

- `referral_transactions` (old)
- `wallet_transactions` with sources `5%_base_+_5%_activation`, `referral_recurring`
- Payout SQL looks for `referral_first_bonus`, `referral_recurring`

First 10% lock **never matches** Saturday unlock query.

`referred_by` is `INTEGER REFERENCES users(id)` in schema. `wallet_service.process_referral` treats it as a **referral_code string**. OTP signup never sets it.

---

## 5. Missing tables / columns referenced in code

| Code expects | In init_db? | Impact |
|---|---|---|
| `services` | no | `/service/add` INSERT fails |
| `interactions` | no | `/api/user/api/track` INSERT fails |
| `users.subscription_status` | no | `activate_subscription` may fail |
| `listings.call_clicks` | no | `/click-call` UPDATE fails |
| `listings.phone` (listing_service) | column is `user_phone` | service helpers broken |
| `listings.premium` / `sponsored` | columns are `is_premium` / `is_sponsored` | `create_listing()` in listing_service |
| `wallet_transactions.description` | no (`source` exists) | `/api/wallet/transactions` 500 |
| `wallet_balance.had_first_withdrawal` | only via optional admin migration | unregistered withdraw_bp |
| `payments.phone` / `plan` / `payment_method` | no | manual proof |

---

## 6. Constraints, FKs, indexes

**Present (good):**

- `users.phone` UNIQUE, `referral_code` UNIQUE
- FKs on many child tables → `users(id)`, `listings(id)`
- Listing geo indexes (`latitude/longitude`, grids, category+active)
- `payments.payment_id` UNIQUE
- `otp_verifications.phone` UNIQUE
- Withdraw status CHECK
- Listing type CHECK (`business`/`service`)
- Review rating 1–5 CHECK

**Weak / missing:**

- No unique `(referrer, referred, period)` for commissions → duplicate 5% possible
- No unique Razorpay order id column separate from payment id
- `sponsored_ads.listing_id` NOT NULL but onboard hardcodes `1` (FK can fail if listing 1 missing)
- `reviews` has no unique (listing, user) — spam ratings
- `wallet_transactions` CHECK includes `'lock'` type but credits use type `credit` + status `locked`
- JWT identity is string; Postgres often coerces to int for `users.id` — fragile if a non-numeric identity is stored (`test_user_001` on optional verify)

---

## 7. Transaction handling

- Some paths: `conn.commit()` after execute
- `process_payment`: `BEGIN`/`COMMIT` via `text()` on SQLAlchemy connection — not a reliable pattern with SQLAlchemy 2 + NullPool
- Referral commission: insert txs then `commit`; if `first_sub_commission_paid` updates but later step fails, partial commissions possible
- Verify path: credit full payment to wallet, then debit subscription price — two commits; failure after credit leaves inflated wallet
- Create listing: insert listing then images; commit timing depends on rest of handler (need remaining lines) — images can orphan if later failure

NullPool = no connection reuse; every `get_db_connection()` is a new connection. Callers often **do not close** connections.

---

## 8. SQLite leftovers

- `.env.example` sqlite URL
- CI sqlite URL
- `setup_render_db.py` has a placeholder postgres URL comment
- Some SQL is Postgres-specific: `ILIKE`, `INTERVAL`, `ON CONFLICT`, `ADD COLUMN IF NOT EXISTS`, `RETURNING`

Do **not** run a SQLite production. Do **not** auto-migrate SQLite → Postgres from this audit.

---

## 9. What must change (report only)

Priority order when implementation is allowed:

1. Confirm production columns: `subscription_status`, `avatar_url`, `owner_reply`, `sponsored_ads.user_id`, withdraw policy columns.
2. Single source of truth: `listings` (stop writing new features to `businesses`).
3. Single payment table or explicit mapping order_id vs payment_id.
4. Single wallet balance table.
5. Align commission `source` strings with unlock job.
6. Create missing tables only when the feature is connected (`services`, `interactions`) — or remove the routes.
7. Connection lifecycle: `with engine.connect()` / close everywhere.
8. Real migrations (Alembic) instead of CREATE IF NOT EXISTS + random ALTER.

**Do not migrate in this audit pass.**
