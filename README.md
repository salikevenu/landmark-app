# LANDMARK Backend

Business directory platform with subscriptions, referrals, wallet, and admin panel.

## Features
- User authentication (OTP & JWT)
- Business listings with geo‑search (grid‑based nearby lookup)
- Subscription plans (free, basic, premium)
- Razorpay payment integration
- Wallet system & withdrawals (admin approval workflow)
- Referral rewards (10% first‑time + 5% recurring commission)
- Admin dashboard with stats, user management, listing approval, payments, withdrawals
- Multi‑language support (English, Hindi, Telugu, Tamil, Kannada)
- Bulk withdrawal approval, CSV exports, referral tree view

## Tech Stack
- **Flask** (Python web framework)
- **PostgreSQL** (primary database)
- **SQLAlchemy** with raw `text()` queries and named parameters (no ORM)
- **Flask‑JWT‑Extended** for authentication
- **Razorpay** API for payments
- **Chart.js** for admin charts
- **Flask‑Limiter** for rate limiting
- **Flask‑CORS** for cross‑origin requests

## Project Structure
LANDMARK Backend/
├── app.py # Application entry point, core routes, JWT config
├── config/ # Payment config, constants
├── database/
│ └── init_db.py # PostgreSQL engine & get_db() helper
├── extensions.py # Razorpay client & rate limiter setup
├── middleware/ # Security headers, fraud check, admin required, rate limit
├── migrations/ # DB migration scripts (PostgreSQL)
├── routes/ # Blueprint route files (auth, listing, payment, admin, wallet, etc.)
├── schemas/ # Pydantic request validation models
├── services/ # Business logic (wallet, payment, referral, admin, listing, geo, etc.)
├── static/ # Uploads, images, QR codes
├── templates/ # Jinja2 HTML templates
├── utils/ # Geo utilities
├── .env # Environment variables (secret keys, DB URL, etc.)
├── requirements.txt # Python dependencies
└── README.md


## Local Setup

### 1. Clone the repository
```bash
git clone https://github.com/salikevenu/landmark-app.git
cd landmark-app
test