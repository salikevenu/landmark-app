from .auth_routes import auth_bp
from .public_routes import public_bp
from .listing_routes import listing_bp
from .nearby_routes import nearby_bp
from .payment_routes import payment_bp
from .admin_routes import admin_bp
from .user_routes import user_bp
from .geo_routes import geo_bp
from .service_routes import service_bp
from .promotions_routes import promotions_bp
from .analytics_routes import analytics_bp, analytics_api_bp
from .review_routes import review_bp
from .reviews_routes import reviews_api_bp
from .transaction_routes import transaction_bp, transactions_api_bp
from .wallet_routes import wallet_bp

def register_routes(app):
    # Public pages (no /api prefix)
    app.register_blueprint(public_bp)
    
    # API routes
    app.register_blueprint(auth_bp, url_prefix="/api/auth", name="main_auth")
    app.register_blueprint(listing_bp, url_prefix="/api/listing")
    app.register_blueprint(nearby_bp, url_prefix="/api/nearby")
    app.register_blueprint(payment_bp, url_prefix="/api/payment")
    app.register_blueprint(admin_bp)
    app.register_blueprint(user_bp, url_prefix="/api/user")
    app.register_blueprint(geo_bp)
    app.register_blueprint(service_bp)
    app.register_blueprint(promotions_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(analytics_api_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(reviews_api_bp)
    app.register_blueprint(transaction_bp)
    app.register_blueprint(transactions_api_bp)
    app.register_blueprint(wallet_bp)

    # Visible in Render logs so a missing analytics mount cannot stay silent
    print(
        "[BOOT] analytics blueprint registered at /analytics "
        "(page) and /api/analytics (api)",
        flush=True,
    )
    print(
        "[BOOT] reviews blueprint registered at /reviews (page) "
        "and /api/reviews (list|stats|reply)",
        flush=True,
    )
    print(
        "[BOOT] transactions blueprint registered at /transactions "
        "(page) and /api/transactions (list|stats)",
        flush=True,
    )