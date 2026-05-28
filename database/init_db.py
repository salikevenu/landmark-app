import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

# Read DATABASE_URL from environment

DATABASE_URL = os.getenv("DATABASE_URL")

engine = None

# Create engine only if DATABASE_URL exists

if DATABASE_URL:
    engine = create_engine(DATABASE_URL, echo=False)

def get_db():
    if engine is None:
        raise Exception("DATABASE_URL is not configured")
    return engine.connect()

web: gunicorn app:app
def init_db():
    conn = get_db()

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    # Indexes for users table
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_code ON users(referral_code)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_first_sub_commission ON users(first_sub_commission_paid)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_grid ON users(lat_grid, lng_grid)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_active ON users(is_active)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_location ON users(latitude, longitude)"))

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_id ON payments(payment_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)"))

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
            user_phone TEXT,
            rating INTEGER CHECK(rating BETWEEN 1 AND 5),
            review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_reviews_listing ON reviews(listing_id)"))

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

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("✅ PostgreSQL tables created with all indexes and default settings.")