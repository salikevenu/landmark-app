import os
import logging
logger = logging.getLogger(__name__)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from dotenv import load_dotenv

load_dotenv()

# Read DATABASE_URL from environment

DATABASE_URL = os.getenv("DATABASE_URL")

# Small bounded pool: this app runs a single Gunicorn sync worker on Render
# Free, so pool_size never needs to be large — it only needs to tolerate a
# connection or two not being returned promptly. pool_pre_ping guards against
# Neon silently closing an idle connection; pool_recycle proactively retires
# connections before that can happen.
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=2,
    max_overflow=1,
    pool_timeout=10,
    pool_recycle=280,
    pool_pre_ping=True,
    connect_args={
        "sslmode": "require",
        "connect_timeout": 10,
        "options": "-c statement_timeout=15000",
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
)

def get_db_connection():
    """Return a fresh database connection."""
    return engine.connect()

def init_db():
    """Run schema initialization on one connection, guaranteeing it is
    closed even if a DDL statement raises partway through."""
    conn = get_db_connection()
    try:
        _init_db_body(conn)
        conn.commit()
    finally:
        conn.close()


def _init_db_body(conn):

    # =====================================================
    # USERS TABLE
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            phone TEXT UNIQUE,
            name TEXT,
            role TEXT DEFAULT 'user',
            plan TEXT DEFAULT 'free',
            business_limit INTEGER DEFAULT 0,
            extra_businesses_purchased INTEGER DEFAULT 0,
            subscription_expiry TEXT,
            device_id TEXT,
            ip_address TEXT,
            referral_rewarded INTEGER DEFAULT 0,
            referral_code TEXT UNIQUE,
            referred_by INTEGER REFERENCES users(id),
            first_sub_commission_paid INTEGER DEFAULT 0,
            wallet_balance REAL DEFAULT 0,
            latitude REAL,
            longitude REAL,
            lat_grid INTEGER,
            lng_grid INTEGER,
            is_active INTEGER DEFAULT 1,
            is_blocked INTEGER DEFAULT 0,
            language TEXT DEFAULT 'en',
            avatar_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    # Defensive: covers any deployment whose users table predates these columns.
    # Safe/idempotent — CREATE TABLE above already includes both for fresh DBs.
    # Must run before the indexes below, which reference referral_code.
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT"))
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by INTEGER REFERENCES users(id)"))

    # Indexes for users table
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_code ON users(referral_code)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_first_sub_commission ON users(first_sub_commission_paid)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_grid ON users(lat_grid, lng_grid)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_active ON users(is_active)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_location ON users(latitude, longitude)"))
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT"))

    # =====================================================
    # WALLET_BALANCE TABLE
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS wallet_balance (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            balance REAL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    # =====================================================
    # SUBSCRIPTIONS TABLE
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            plan_name TEXT,
            amount REAL,
            status TEXT,
            next_billing_date TIMESTAMP,
            razorpay_subscription_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id)"))

    # =====================================================
    # WALLET TRANSACTIONS
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            amount REAL,
            type TEXT CHECK(type IN ('credit','debit','lock')),
            source TEXT,
            reference_id TEXT,
            status TEXT DEFAULT 'locked',
            unlock_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            razorpay_payment_id TEXT
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_transactions(user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_wallet_tx_created ON wallet_transactions(created_at DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_wallet_tx_type ON wallet_transactions(type)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_wallet_tx_status ON wallet_transactions(status)"))
    conn.execute(text(
        "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS razorpay_payment_id TEXT"
    ))
    # Unique commission indexes are created by
    # migrations/add_referral_commission_money_safety.py after duplicate preflight.

    # =====================================================
    # BUSINESSES TABLE (legacy)
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS businesses (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            business_name TEXT,
            plan TEXT,
            category TEXT,
            description TEXT,
            phone TEXT,
            location TEXT,
            image TEXT,
            whatsapp TEXT,
            city TEXT,
            state TEXT,
            is_active INTEGER DEFAULT 1,
            featured INTEGER DEFAULT 0,
            premium INTEGER DEFAULT 0,
            verified INTEGER DEFAULT 0,
            rating REAL DEFAULT 4.0,
            latitude REAL DEFAULT 0,
            longitude REAL DEFAULT 0,
            address TEXT,
            status TEXT DEFAULT 'pending',
            logo_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    # =====================================================
    # REFERRAL TRANSACTIONS
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS referral_transactions (
            id SERIAL PRIMARY KEY,
            referrer_id INTEGER REFERENCES users(id),
            referred_user_id INTEGER REFERENCES users(id),
            plan_type TEXT,
            reward_amount REAL,
            payment_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_referrer ON referral_transactions(referrer_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_referred_user ON referral_transactions(referred_user_id)"))

    # =====================================================
    # WITHDRAW REQUESTS
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            payment_method TEXT,
            upi_id TEXT,
            reference_id TEXT,
            admin_note TEXT,
            processed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT status_check CHECK (status IN ('pending','approved','rejected','paid'))
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_withdraw_user ON withdraw_requests(user_id)"))
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_withdraw_requests_user_reference
        ON withdraw_requests (user_id, reference_id)
        WHERE reference_id IS NOT NULL
    """))
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_withdraw_debit_ref
        ON wallet_transactions (reference_id)
        WHERE type = 'debit' AND source = 'withdraw_request' AND reference_id IS NOT NULL
    """))
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_tx_withdraw_refund_ref
        ON wallet_transactions (reference_id)
        WHERE type = 'credit' AND source = 'withdraw_refund' AND reference_id IS NOT NULL
    """))

    # =====================================================
    # LISTINGS TABLE
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS listings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            listing_type TEXT CHECK(listing_type IN ('business','service')),
            business_name TEXT NOT NULL,
            slug TEXT UNIQUE,
            category TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            lat_grid INTEGER,
            lng_grid INTEGER,
            description TEXT,
            user_phone TEXT,
            whatsapp TEXT,
            email TEXT,
            website TEXT,
            image TEXT,
            video TEXT,
            logo_url TEXT,
            image_url TEXT,
            opening_hours TEXT,
            price_range TEXT,
            rating REAL DEFAULT 0,
            rating_count INTEGER DEFAULT 0,
            total_reviews INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            is_verified INTEGER DEFAULT 0,
            is_premium INTEGER DEFAULT 0,
            is_sponsored INTEGER DEFAULT 0,
            is_featured INTEGER DEFAULT 0,
            views INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            whatsapp_clicks INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        )
    """))

    # Indexes for listings
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_lat_lng ON listings(latitude, longitude)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_grid ON listings(lat_grid, lng_grid)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_listings_location ON listings(latitude, longitude)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_city ON listings(city)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_category ON listings(category)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_listing_type ON listings(listing_type)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_active_listings ON listings(is_active)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_category_active ON listings(category, is_active)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_listings ON listings(user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_premium ON listings(is_premium)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_verified ON listings(is_verified)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_active_grid ON listings(is_active, lat_grid, lng_grid)"))

    # =====================================================
    # LISTING_IMAGES
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS listing_images (
            id SERIAL PRIMARY KEY,
            listing_id INTEGER REFERENCES listings(id),
            image_url TEXT,
            image_type TEXT CHECK(image_type IN ('logo','shop','service')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_listing_images ON listing_images(listing_id)"))

    # =====================================================
    # BUSINESS_MEDIA
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS business_media (
            id SERIAL PRIMARY KEY,
            business_id INTEGER REFERENCES businesses(id),
            file_url TEXT,
            media_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    # =====================================================
    # PAYMENTS TABLE
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            order_id TEXT,
            user_phone TEXT,
            payment_id TEXT UNIQUE,
            amount REAL,
            status TEXT,
            plan TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_id ON payments(payment_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)"))
    conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS plan TEXT"))
    conn.execute(text("ALTER TABLE payments ADD COLUMN IF NOT EXISTS order_id TEXT"))
    # Unique payments.order_id is created by
    # migrations/add_referral_commission_money_safety.py after duplicate preflight.

    # =====================================================
    # REFERRAL COMMISSION JOBS (durable outbox)
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS referral_commission_jobs (
            id SERIAL PRIMARY KEY,
            payment_id TEXT NOT NULL,
            razorpay_payment_id TEXT,
            referred_user_id INTEGER NOT NULL REFERENCES users(id),
            amount_rupees NUMERIC(12,2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            CONSTRAINT uq_referral_commission_jobs_payment UNIQUE (payment_id)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_referral_commission_jobs_status "
        "ON referral_commission_jobs(status)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_referral_commission_jobs_rzp "
        "ON referral_commission_jobs(razorpay_payment_id)"
    ))

    # =====================================================
    # PAYMENT TRANSACTIONS (Razorpay)
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS payment_transactions (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            razorpay_order_id TEXT NOT NULL,
            razorpay_payment_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_payment_transactions_user ON payment_transactions(user_id)"))

    # =====================================================
    # SPONSORED_ADS
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS sponsored_ads (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            listing_id INTEGER NOT NULL REFERENCES listings(id),
            plan TEXT,
            amount REAL,
            start_date TIMESTAMP,
            end_date TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sponsored_listing ON sponsored_ads(listing_id)"))

    # =====================================================
    # REVIEWS
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY,
            listing_id INTEGER REFERENCES listings(id),
            user_id INTEGER REFERENCES users(id),
            user_phone TEXT,
            rating INTEGER CHECK(rating BETWEEN 1 AND 5),
            review TEXT,
            owner_reply TEXT,
            replied_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_reviews_listing ON reviews(listing_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at DESC)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_reviews_rating ON reviews(rating)"))
    conn.execute(text(
        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS owner_reply TEXT"
    ))
    conn.execute(text(
        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS replied_at TIMESTAMP"
    ))

    # =====================================================
    # BUSINESS LEADS
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS business_leads (
            id SERIAL PRIMARY KEY,
            business_name TEXT,
            phone TEXT,
            category TEXT,
            city TEXT,
            latitude REAL,
            longitude REAL,
            lat_grid INTEGER,
            lng_grid INTEGER,
            invited_by INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_leads_grid ON business_leads(lat_grid, lng_grid)"))

    # =====================================================
    # CITIES TABLE
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS cities (
            id SERIAL PRIMARY KEY,
            city_name TEXT UNIQUE,
            state TEXT,
            is_active INTEGER DEFAULT 1,
            launch_status TEXT DEFAULT 'pending',
            total_businesses INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_city_active ON cities(city_name, is_active)"))

    # =====================================================
    # ADMIN AUDIT LOG (for admin panel)
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id SERIAL PRIMARY KEY,
            admin_id INTEGER,
            admin_phone TEXT,
            action TEXT,
            target_type TEXT,
            target_id TEXT,
            details TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_log(admin_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_log(created_at)"))

    # =====================================================
    # ADMIN SETTINGS (key-value store)
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    # Insert default settings if not present
    defaults = [
        ('commission_rate', '10'),
        ('withdrawal_min_amount', '100'),
        ('withdrawal_max_amount', '50000'),
        ('referral_bonus_percent', '10'),
        ('recurring_commission_percent', '5'),
        ('sponsor_price', '999'),
        ('verify_price', '499')
    ]
    for key, val in defaults:
        conn.execute(text("""
            INSERT INTO admin_settings (key, value)
            VALUES (:key, :val)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "val": val})

    # =====================================================
    # OTP VERIFICATIONS (for production multi-worker support)
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS otp_verifications (
            id SERIAL PRIMARY KEY,
            phone TEXT UNIQUE NOT NULL,
            verification_id TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_otp_phone ON otp_verifications(phone)"))

    # =====================================================
    # PENDING REFERRALS (durable OTP attribution)
    # =====================================================
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pending_referrals (
            phone TEXT PRIMARY KEY,
            ref_code TEXT NOT NULL,
            referrer_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pending_referrals_referrer ON pending_referrals(referrer_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pending_referrals_expires ON pending_referrals(expires_at)"))

    # Safe for older DBs that created sponsored_ads without user_id
    conn.execute(text(
        "ALTER TABLE sponsored_ads "
        "ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"
    ))

    # =====================================================
    # RANK SYSTEM (additive; reads users.referred_by, never writes it)
    # =====================================================
    # Index supporting downline traversal (recursive CTE joins on referred_by).
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)"))

    # Cached/materialized rank + qualification counts, rebuilt by the
    # rank_service batch recompute. One row per user; 1:1 with users.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS user_rank_stats (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            rank TEXT NOT NULL DEFAULT 'unranked',
            verified_users_count INTEGER NOT NULL DEFAULT 0,
            active_subscribers_count INTEGER NOT NULL DEFAULT 0,
            qualified_members_count INTEGER NOT NULL DEFAULT 0,
            qualified_guides_count INTEGER NOT NULL DEFAULT 0,
            qualified_leaders_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_rank_stats_rank ON user_rank_stats(rank)"))

    # Append-only achievement history. Never updated or deleted so a user's
    # past rank-ups survive even if their current rank later changes.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS rank_achievements (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            previous_rank TEXT NOT NULL,
            new_rank TEXT NOT NULL,
            milestone_key TEXT,
            achieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rank_achievements_user ON rank_achievements(user_id)"))

    # Reward ledger. Separate from wallet_transactions and
    # referral_transactions on purpose (Ranger/Leader rewards must never be
    # mixed with referral commissions). Originally a one-time
    # milestone_key-keyed ledger; the business model is now MONTHLY GROWTH
    # REWARDS (see services/rank_service.py), identified by
    # (user_id, reward_type, reward_period) e.g. ('leader_monthly','2026-09').
    # reward_type/reward_period are additive columns (see the ALTER below
    # for pre-existing deployments) — milestone_key is kept NOT NULL and
    # still populated (with a synthetic value) for new rows so no existing
    # constraint has to change.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS rank_rewards (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            milestone_key TEXT NOT NULL,
            reward_type TEXT,
            reward_period TEXT,
            rank TEXT NOT NULL,
            amount_inr NUMERIC(12,2) NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            approved_by INTEGER REFERENCES users(id),
            approved_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_rank_rewards_user_milestone UNIQUE (user_id, milestone_key),
            CONSTRAINT chk_rank_rewards_status CHECK (status IN ('pending','approved','paid','rejected'))
        )
    """))
    # Additive for deployments that already created rank_rewards before
    # the monthly-reward model existed.
    conn.execute(text("ALTER TABLE rank_rewards ADD COLUMN IF NOT EXISTS reward_type TEXT"))
    conn.execute(text("ALTER TABLE rank_rewards ADD COLUMN IF NOT EXISTS reward_period TEXT"))
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_rank_rewards_user_type_period
        ON rank_rewards (user_id, reward_type, reward_period)
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rank_rewards_user ON rank_rewards(user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rank_rewards_status ON rank_rewards(status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rank_rewards_period ON rank_rewards(reward_period)"))

    # =====================================================
    # POS BUSINESSES
    # =====================================================
    # LANDMARK POS is a separate product from this marketplace app. A POS
    # business is its own tenant concept — deliberately not `businesses`
    # (unused/legacy) or `listings` (marketplace directory entries). See
    # the LANDMARK POS repo's DECISIONS.md for the tenancy model. No
    # subscription/plan/limit columns yet — future work.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_businesses (
            id SERIAL PRIMARY KEY,
            owner_user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_businesses_owner ON pos_businesses(owner_user_id)"))

    # =====================================================
    # POS PRODUCTS
    # =====================================================
    # Catalog items for a POS business. `price` is INTEGER minor units
    # (paise) — deliberately not REAL like this backend's legacy money
    # columns (subscriptions.amount, wallet_transactions.amount, ...);
    # POS money must match the LANDMARK POS Flutter app's integer
    # minor-unit Money convention. See the LANDMARK POS repo's
    # DECISIONS.md.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_products (
            id SERIAL PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES pos_businesses(id),
            name TEXT NOT NULL,
            price INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_products_business ON pos_products(business_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_products_business_active ON pos_products(business_id, is_active)"))

    # =====================================================
    # POS INVENTORY
    # =====================================================
    # One stock row per product — single location, no warehouses/transfers
    # yet. `business_id` is stored directly (not just derived via a join
    # through `pos_products`) so authorization/scoping queries never need
    # a join, matching this backend's existing tenant-scoping pattern.
    # `quantity` is INTEGER, matching this backend's existing convention
    # for whole-unit counts. No write endpoint exists yet — rows are
    # created by a future "receive stock" feature; until then this table
    # is expected to be empty and the inventory read endpoint reports 0
    # for every active product via a LEFT JOIN.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_inventory (
            product_id INTEGER PRIMARY KEY REFERENCES pos_products(id),
            business_id INTEGER NOT NULL REFERENCES pos_businesses(id),
            quantity INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_inventory_business ON pos_inventory(business_id)"))

    # =====================================================
    # POS SALES
    # =====================================================
    # A completed sale (header) and its line items. `pos_sale_items`
    # snapshots `product_name`/`unit_price` at sale time — a historical
    # financial record must not change if the catalog is edited later.
    # `line_total`/`total_amount` are computed server-side and stored,
    # never recomputed from current catalog data. No `business_id` on
    # `pos_sale_items`: items are always accessed via their parent
    # `sale_id`, which is itself already business-scoped.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_sales (
            id SERIAL PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES pos_businesses(id),
            total_amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_sales_business ON pos_sales(business_id)"))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_sale_items (
            id SERIAL PRIMARY KEY,
            sale_id INTEGER NOT NULL REFERENCES pos_sales(id),
            product_id INTEGER NOT NULL REFERENCES pos_products(id),
            product_name TEXT NOT NULL,
            unit_price INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            line_total INTEGER NOT NULL
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_sale_items_sale ON pos_sale_items(sale_id)"))

    # =====================================================
    # POS CUSTOMERS
    # =====================================================
    # A POS business's own customer roster — deliberately separate from
    # `users` (the platform login identity, globally unique by phone).
    # A POS customer may have no LANDMARK account, and the same phone
    # number may be a customer of multiple businesses independently, so
    # phone is unique per business, not globally.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pos_customers (
            id SERIAL PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES pos_businesses(id),
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pos_customers_business ON pos_customers(business_id)"))
    conn.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pos_customers_business_phone
        ON pos_customers (business_id, phone)
    """))


if __name__ == "__main__":
    init_db()
    logger.info("✅ PostgreSQL tables created with all indexes and default settings.")