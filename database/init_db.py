import os
import sqlite3
from flask import g

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "landmark.db")

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()

    # =========================
    # SQLite Performance Mode
    # =========================
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA temp_store=MEMORY;")

    # =====================================================
    # USERS TABLE
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            referred_by INTEGER,
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
    """)

    # Indexes for fast lookup
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_code ON users(referral_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_first_sub_commission ON users(first_sub_commission_paid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_grid ON users(lat_grid,lng_grid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_active ON users(is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_location ON users(latitude,longitude)")
    
    # =====================================================
    # wallet_balance TABLE
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS wallet_balance (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """) 
                                           
    # =====================================================
    # SUBSCRIPTIONS TABLE
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_name TEXT,
            amount REAL,
            status TEXT,
            next_billing_date DATETIME,
            razorpay_subscription_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id)")

    # =====================================================
    # WALLET TRANSACTIONS
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            type TEXT CHECK(type IN ('credit','debit','lock')),
            source TEXT,
            reference_id TEXT,
            status TEXT DEFAULT 'locked',
            unlock_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # =====================================================
    # Businesses table
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    # =====================================================
    # REFERRAL TRANSACTIONS
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referral_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_user_id INTEGER,
            plan_type TEXT,       
            reward_amount REAL,
            payment_id TEXT,       
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_referrer ON referral_transactions(referrer_id)
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_referred_user ON referral_transactions(referred_user_id)")    
    
    # =====================================================
    # WITHDRAW REQUESTS
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            amount REAL NOT NULL,

            status TEXT DEFAULT 'pending'
            CHECK(status IN ('pending','approved','rejected','paid')),

            payment_method TEXT,

            upi_id TEXT,

            reference_id TEXT,

            admin_note TEXT,

            processed_at DATETIME,

            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_withdraw_user ON withdraw_requests(user_id)")

    # =====================================================
    # LISTINGS TABLE (PRODUCTION READY)
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS listings (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER,
                   
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
    """)

    # Geo search indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_lat_lng ON listings(latitude, longitude)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_grid ON listings(lat_grid, lng_grid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_listings_location ON listings(latitude, longitude)")
    # Search indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_city ON listings(city)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_category ON listings(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_listing_type ON listings(listing_type)")

    # Status indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_active_listings ON listings(is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_category_active ON listings(category, is_active)")

    # User listings
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_listings ON listings(user_id)")

    # Premium search
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_premium ON listings(is_premium)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_verified ON listings(is_verified)")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_active_grid ON listings(is_active, lat_grid, lng_grid)"
    )
    # =====================================================
    # listing_images TABLE
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS listing_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER,
            image_url TEXT,
            image_type TEXT CHECK(image_type IN ('logo','shop','service')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_listing_images ON listing_images(listing_id)")
    # =====================================================
    # business_media  TABLE
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS business_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER,
            file_url TEXT,
            media_type TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(business_id) REFERENCES businesses(id)
        );
    """)                
    # =====================================================
    # PAYMENTS TABLE
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            order_id TEXT,       
            user_phone TEXT,
            payment_id TEXT UNIQUE,
            amount REAL,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Index for fast duplicate check
    cursor.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_id ON payments(payment_id)""")

    # Index for fast user payment history
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)""")
    
    # =====================================================
    # PAYMENT TRANSACTIONS for Razorpay (upgrade tracking)
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            razorpay_order_id TEXT NOT NULL,
            razorpay_payment_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payment_transactions_user ON payment_transactions(user_id)")

    # =====================================================
    # sponsored_ads TABLE
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sponsored_ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            plan TEXT,                 -- e.g., 'top_7_days','top_30_days'
            amount REAL,
            start_date TIMESTAMP,
            end_date TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_sponsored_listing ON sponsored_ads(listing_id)""")

    # =====================================================
    # reviews TABLE
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER,
            user_phone TEXT,
            rating INTEGER CHECK(rating BETWEEN 1 AND 5),
            review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_reviews_listing ON reviews(listing_id)""")
    

    # =====================================================
    # BUSINESS LEADS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS business_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    """)

    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_leads_grid ON business_leads(lat_grid,lng_grid)""")

    # =====================================================
    # CITIES TABLE
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city_name TEXT UNIQUE,
            state TEXT,
            is_active INTEGER DEFAULT 1,
            launch_status TEXT DEFAULT 'pending',
            total_businesses INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
        # Index for faster city lookup
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_city_active ON cities(city_name, is_active)""")

    conn.commit()
    conn.close()    
# Run once when app starts
init_db()

# -------- Per‑request connection management ----------
def get_db():
    """Return the database connection for the current request."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")   # optional but recommended
    return g.db

def close_db(e=None):
    """Close the database connection at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()