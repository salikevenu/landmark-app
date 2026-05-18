from .auth_routes import auth_bp
from .listing_routes import listing_bp
from .nearby_routes import nearby_bp
from .payment_routes import payment_bp
from .admin_routes import admin_bp
from .user_routes import user_bp
from .geo_routes import geo_bp
from .service_routes import service_bp
from .promotions_routes import promo_bp
from .analytics_routes import analytics_bp
from .review_routes import review_bp
from .transaction_routes import transaction_bp

def register_routes(app):

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(listing_bp, url_prefix="/api/listing")
    app.register_blueprint(nearby_bp, url_prefix="/api/nearby")
    app.register_blueprint(payment_bp, url_prefix="/api/payment")
    app.register_blueprint(admin_bp)
    app.register_blueprint(user_bp, url_prefix="/api/user")
    app.register_blueprint(geo_bp)   # or with url_prefix="/api/geo"
    app.register_blueprint(service_bp)          # url_prefix='/service' already set
    app.register_blueprint(promo_bp)            # url_prefix='/promotions'
    app.register_blueprint(analytics_bp)        # url_prefix='/analytics'
    app.register_blueprint(review_bp)           # url_prefix='/reviews'
    app.register_blueprint(transaction_bp)      # url_prefix='/transactions'