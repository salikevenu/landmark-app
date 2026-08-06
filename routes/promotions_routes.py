from flask import Blueprint, request, jsonify, render_template
from flask_jwt_extended import jwt_required, get_jwt_identity
from database.init_db import get_db_connection
from sqlalchemy import text
from datetime import datetime

promotions_bp = Blueprint('promotions', __name__, url_prefix='/promotions')

@promotions_bp.route('/')
@jwt_required()
def promotions():
    user_id = get_jwt_identity()
    conn = get_db_connection()

    # 1. Fetch User's Business Info (for Live Preview)
    business = conn.execute(
        text("SELECT business_name, category, rating, phone, whatsapp, logo_url, latitude, longitude FROM businesses WHERE user_id = :uid LIMIT 1"),
        {"uid": user_id}
    ).fetchone()

    # 2. Fetch Wallet Balance
    wallet = conn.execute(
        text("SELECT balance FROM wallet_balance WHERE user_id = :uid"),
        {"uid": user_id}
    ).fetchone()
    wallet_balance = wallet._mapping['balance'] if wallet else 0.0

    # 3. Fetch Active Promotions
    active_promos = conn.execute(
        text("SELECT plan, start_date, end_date, is_active FROM sponsored_ads WHERE user_id = :uid AND end_date > NOW()"),
        {"uid": user_id}
    ).fetchall()
    
    active_promos_list = []
    for row in active_promos:
        if row._mapping['plan'] is None:
            continue
        active_promos_list.append({
            'plan': row._mapping['plan'] or 'Unknown',
            'days_left': max(0, (row._mapping['end_date'] - datetime.now()).days),
            'status': 'Active' if row._mapping['is_active'] else 'Paused'
        })

    # 4. Fetch Analytics (Static dummy data for now - connect your real analytics table)
    analytics = {'views': 1245, 'clicks': 329, 'calls': 54, 'whatsapp': 28, 'ctr': 13}

    # 5. Promotion Plans (From DB - or hardcoded as constants for now)
    promotion_plans = [
        {'name': 'Starter', 'price': 99, 'days': 3, 'features': ['Featured Listing', 'Top Search', 'Nearby Boost']},
        {'name': 'Popular', 'price': 299, 'days': 7, 'features': ['Featured Badge', 'Top Search', 'Home Page', 'AI Boost']},
        {'name': 'Premium', 'price': 999, 'days': 30, 'features': ['Home Banner', 'Top Listing', 'Push Notifications', 'Analytics']}
    ]

    # 6. AI Score & Recommendations (Placeholder logic)
    ai_score = 82
    ai_recommendations = ["Upload Logo", "Add 5 Photos", "Add WhatsApp", "Add Description"]

        # 7. Safely prepare business data for JSON serialization
        business_data = None
        if business:
            business_data = {
                'business_name': business._mapping.get('business_name') or '',
                'category': business._mapping.get('category') or '',
                'rating': business._mapping.get('rating') or 0,
                'phone': business._mapping.get('phone') or '',
                'whatsapp': business._mapping.get('whatsapp') or '',
                'logo_url': business._mapping.get('logo_url') or '',
                'latitude': business._mapping.get('latitude') or 0,
                'longitude': business._mapping.get('longitude') or 0,
            }

        return render_template(
            'promotions/index.html',
            business=business_data,
            wallet_balance=wallet_balance,
            active_promos=active_promos_list,
            analytics=analytics,
            promotion_plans=promotion_plans,
            ai_score=ai_score,
            ai_recommendations=ai_recommendations,
            now=datetime.now()
        )

@promotions_bp.route("/api/promotions/onboard", methods=["POST"])
@jwt_required()
def create_promotion():
    user_id = get_jwt_identity()
    data = request.json
    plan = data.get('plan')
    listing_url = data.get('listing_url')
    headline = data.get('headline')

    conn = get_db_connection()
    conn.execute(
        text("INSERT INTO sponsored_ads (user_id, listing_id, plan, start_date, end_date, is_active) VALUES (:uid, 1, :plan, NOW(), NOW() + INTERVAL '1 month', 1)"),
        {"uid": user_id, "plan": plan}
    )
    conn.commit()
    
    return jsonify({"success": True, "message": "Promotion started successfully!"})