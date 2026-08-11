from flask import Blueprint, jsonify, render_template
from flask_jwt_extended import jwt_required, get_jwt_identity
from database.init_db import get_db_connection
from sqlalchemy import text
from datetime import datetime, timedelta

analytics_bp = Blueprint('analytics', __name__)

@analytics_bp.route('/analytics')
@jwt_required()
def analytics_page():
    return render_template('analytics/index.html')

@analytics_bp.route('/api/analytics/data')
@jwt_required()
def get_analytics_data():
    user_id = get_jwt_identity()
    conn = get_db_connection()

    # 1. Get Total Views, Clicks, and Calls for user's listings
    totals = conn.execute(text("""
        SELECT 
            COALESCE(SUM(views), 0) as total_views,
            COALESCE(SUM(clicks), 0) as total_clicks,
            COALESCE(SUM(whatsapp_clicks), 0) as total_whatsapp
        FROM listings 
        WHERE user_id = :uid
    """), {"uid": user_id}).fetchone()

    # 2. Get Daily stats for the last 7 days (for Chart.js)
    daily_stats = []
    for i in range(6, -1, -1):
        date = datetime.now() - timedelta(days=i)
        date_str = date.strftime('%Y-%m-%d')
        
        row = conn.execute(text("""
            SELECT 
                COALESCE(SUM(views), 0) as views,
                COALESCE(SUM(clicks), 0) as clicks
            FROM listings 
            WHERE user_id = :uid AND DATE(created_at) = :date
        """), {"uid": user_id, "date": date_str}).fetchone()
        
        daily_stats.append({
            'date': date_str,
            'views': row._mapping['views'],
            'clicks': row._mapping['clicks']
        })

    # 3. Top performing listings
    top_listings = conn.execute(text("""
        SELECT id, business_name, views, clicks, whatsapp_clicks
        FROM listings
        WHERE user_id = :uid
        ORDER BY views DESC
        LIMIT 5
    """), {"uid": user_id}).fetchall()

    return jsonify({
        'totals': {
            'views': totals._mapping['total_views'],
            'clicks': totals._mapping['total_clicks'],
            'whatsapp': totals._mapping['total_whatsapp'],
            'calls': 0  # Placeholder for future column
        },
        'daily': daily_stats,
        'top_listings': [dict(row._mapping) for row in top_listings]
    })