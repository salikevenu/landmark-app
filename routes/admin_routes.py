import io
import os
from flask import Blueprint, jsonify, render_template, request, send_file, redirect
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity, create_access_token
from database.init_db import get_db
from functools import wraps
from datetime import timedelta, datetime
from sqlalchemy import text

from services.admin_service import (
    get_admin_stats, get_admin_users, ban_user, unban_user, change_user_role, reset_user_subscription,
    get_admin_listings, approve_listing_admin, disable_listing_admin, verify_listing_admin,
    delete_listing_admin, sponsor_listing_admin,
    get_admin_payments, approve_payment_admin,
    get_withdraw_requests, approve_withdraw_request, reject_withdraw_request, mark_withdraw_paid,
    bulk_approve_withdrawals,
    get_admin_referrals,
    export_users_csv, export_payments_csv, export_withdrawals_csv,
    get_settings, update_setting,
    log_admin_action
)
from services.payment_service import activate_subscription

admin_bp = Blueprint("admin", __name__)

def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper

def get_admin_info():
    """Helper to get current admin id and phone from JWT"""
    identity = get_jwt_identity()
    conn = get_db()
    user = conn.execute(
        text("SELECT id, phone FROM users WHERE phone = :phone"),
        {"phone": identity}
    ).fetchone()
    if not user:
        return None, None
    return user._mapping['id'], user._mapping['phone']

# -------------------------------
# HTML PAGES (shells)
# -------------------------------

@admin_bp.route("/admin/login")
def admin_login_page():
    return render_template("admin/admin_login.html")

@admin_bp.route("/admin")
@admin_required
def admin_index():
    return redirect("/admin/dashboard")

@admin_bp.route("/admin/control")
@admin_required
def admin_control():
    return render_template("admin/admin_control.html")

@admin_bp.route("/admin/users")
@admin_required
def admin_users_page():
    return render_template("admin/admin_users.html")

@admin_bp.route("/admin/dashboard")
@admin_required
def admin_dashboard_page():
    return render_template("admin/admin_dashboard.html")

@admin_bp.route("/admin/listings")
@admin_required
def admin_listings_page():
    return render_template("admin/admin_listings.html")

@admin_bp.route("/admin/payments")
@admin_required
def admin_payments_page():
    return render_template("admin/admin_payments.html")

@admin_bp.route("/admin/withdraws")
@admin_required
def admin_withdraws_page():
    return render_template("admin/withdraws.html")

@admin_bp.route("/admin/referrals")
@admin_required
def admin_referrals_page():
    return render_template("admin/admin_referrals.html")

@admin_bp.route("/admin/settings")
@admin_required
def admin_settings_page():
    return render_template("admin/admin_settings.html")

# -------------------------------
# API ENDPOINTS
# -------------------------------
@admin_bp.route("/api/admin/stats")
@admin_required
def stats():
    period = request.args.get('period', 'week')
    stats = get_admin_stats(period)
    return jsonify(stats)

# Users
@admin_bp.route("/api/admin/users")
@admin_required
def api_users():
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    search = request.args.get('search', '')
    role = request.args.get('role', '')
    status = request.args.get('status', '')
    result = get_admin_users(page, limit, search, role, status)
    return jsonify(result)

@admin_bp.route("/api/admin/users/<int:user_id>/ban", methods=["POST"])
@admin_required
def api_ban_user(user_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = ban_user(user_id, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/users/<int:user_id>/unban", methods=["POST"])
@admin_required
def api_unban_user(user_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = unban_user(user_id, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def api_change_role(user_id):
    data = request.json
    new_role = data.get('role')
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = change_user_role(user_id, new_role, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/users/<int:user_id>/reset-subscription", methods=["POST"])
@admin_required
def api_reset_subscription(user_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = reset_user_subscription(user_id, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/users/<int:user_id>/referral-tree")
@admin_required
def user_referral_tree(user_id):
    conn = get_db()
    # Get the user
    user = conn.execute(
        text("SELECT id, phone, name, referral_code, referred_by FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Find referrer
    referrer = None
    if user._mapping["referred_by"]:
        referrer = conn.execute(
            text("SELECT id, phone, name FROM users WHERE id = :ref_id"),
            {"ref_id": user._mapping["referred_by"]}
        ).fetchone()

    # Find direct referrals (users who were referred by this user's code)
    referrals = conn.execute(
        text("SELECT id, phone, name, created_at FROM users WHERE referred_by = :uid ORDER BY created_at DESC"),
        {"uid": user_id}
    ).fetchall()

    return jsonify({
        "user": dict(user._mapping),
        "referrer": dict(referrer._mapping) if referrer else None,
        "referrals": [dict(r._mapping) for r in referrals]
    })

# Listings
@admin_bp.route("/api/admin/listings")
@admin_required
def api_listings():
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    category = request.args.get('category', '')
    result = get_admin_listings(page, limit, search, status, category)
    return jsonify(result)

@admin_bp.route("/api/admin/listings/<int:listing_id>/approve", methods=["POST"])
@admin_required
def api_approve_listing(listing_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = approve_listing_admin(listing_id, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/listings/<int:listing_id>/disable", methods=["POST"])
@admin_required
def api_disable_listing(listing_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = disable_listing_admin(listing_id, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/listings/<int:listing_id>/verify", methods=["POST"])
@admin_required
def api_verify_listing(listing_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = verify_listing_admin(listing_id, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/listings/<int:listing_id>/delete", methods=["DELETE"])
@admin_required
def api_delete_listing(listing_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = delete_listing_admin(listing_id, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/listings/<int:listing_id>/sponsor", methods=["POST"])
@admin_required
def api_sponsor_listing(listing_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = sponsor_listing_admin(listing_id, admin_id, admin_phone, ip)
    return jsonify(result)

# Payments
@admin_bp.route("/api/admin/payments")
@admin_required
def api_payments():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    result = get_admin_payments(page, limit, search, status, start_date, end_date)
    return jsonify(result)

@admin_bp.route("/api/admin/payments/<int:payment_id>/approve", methods=["POST"])
@admin_required
def api_approve_payment(payment_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = approve_payment_admin(payment_id, admin_id, admin_phone, ip)
    return jsonify(result)

# Withdrawals
@admin_bp.route("/api/admin/withdrawals")
@admin_required
def api_withdrawals():
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    status = request.args.get('status', '')
    result = get_withdraw_requests(page, limit, status)
    return jsonify(result)

@admin_bp.route("/api/admin/withdrawals/<int:wid>/approve", methods=["POST"])
@admin_required
def api_approve_withdraw(wid):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = approve_withdraw_request(wid, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/withdrawals/<int:wid>/reject", methods=["POST"])
@admin_required
def api_reject_withdraw(wid):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = reject_withdraw_request(wid, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/withdrawals/<int:wid>/paid", methods=["POST"])
@admin_required
def api_mark_paid(wid):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = mark_withdraw_paid(wid, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/withdrawals/bulk-approve", methods=["POST"])
@admin_required
def api_bulk_approve():
    data = request.json
    wids = data.get('ids', [])
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    results = bulk_approve_withdrawals(wids, admin_id, admin_phone, ip)
    return jsonify(results)

# Referrals
@admin_bp.route("/api/admin/referrals")
@admin_required
def api_referrals():
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    search = request.args.get('search', '')
    result = get_admin_referrals(page, limit, search)
    return jsonify(result)

# CSV Exports
@admin_bp.route("/api/admin/export/users.csv")
@admin_required
def export_users():
    csv_data = export_users_csv()
    return send_file(io.BytesIO(csv_data.encode()), mimetype='text/csv', as_attachment=True, download_name='users.csv')

@admin_bp.route("/api/admin/export/payments.csv")
@admin_required
def export_payments():
    csv_data = export_payments_csv()
    return send_file(io.BytesIO(csv_data.encode()), mimetype='text/csv', as_attachment=True, download_name='payments.csv')

@admin_bp.route("/api/admin/export/withdrawals.csv")
@admin_required
def export_withdrawals():
    csv_data = export_withdrawals_csv()
    return send_file(io.BytesIO(csv_data.encode()), mimetype='text/csv', as_attachment=True, download_name='withdrawals.csv')

# Settings
@admin_bp.route("/api/admin/settings", methods=["GET"])
@admin_required
def api_get_settings():
    settings = get_settings()
    return jsonify(settings)

@admin_bp.route("/api/admin/settings", methods=["POST"])
@admin_required
def api_update_setting():
    data = request.json
    key = data.get('key')
    value = data.get('value')
    if not key:
        return jsonify({'error': 'Missing key'}), 400
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = update_setting(key, value, admin_id, admin_phone, ip)
    return jsonify(result)

# Legacy endpoints (for compatibility)
@admin_bp.route("/api/admin/activate", methods=["POST"])
@admin_required
def activate():
    data = request.json
    phone = data.get("phone")
    plan = data.get("plan", "business_basic")
    days = data.get("days", 30)
    expiry = activate_subscription(phone, plan, days)
    return jsonify({"status": "activated", "phone": phone, "expiry": expiry})

@admin_bp.route("/api/admin/approve-payment", methods=["POST"])
@admin_required
def approve_payment_legacy():
    # kept for backward compatibility
    data = request.json
    payment_id = data.get("payment_id")
    if not payment_id:
        return jsonify({"error": "payment_id required"}), 400
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = approve_payment_admin(payment_id, admin_id, admin_phone, ip)
    return jsonify(result)

@admin_bp.route("/api/admin/users/<int:user_id>/impersonate", methods=["POST"])
@admin_required
def impersonate_user(user_id):
    conn = get_db()
    user = conn.execute(
        text("SELECT id, phone, role FROM users WHERE id = :uid"),
        {"uid": user_id}
    ).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Generate short-lived token (10 minutes)
    token = create_access_token(
        identity=str(user._mapping["id"]),
        additional_claims={
            "role": user._mapping["role"],
            "phone": user._mapping["phone"],
            "impersonated": True
        },
        expires_delta=timedelta(minutes=10)
    )
    return jsonify({"access_token": token, "phone": user._mapping["phone"]})

@admin_bp.route("/api/admin/stats/chart")
@admin_required
def admin_chart_data():
    conn = get_db()
    days = 7
    dates = []
    user_counts = []
    listing_counts = []
    revenue_daily = []

    for i in range(days - 1, -1, -1):
        date_obj = datetime.utcnow() - timedelta(days=i)
        date_str = date_obj.strftime("%Y-%m-%d")
        dates.append(date_str)

        # Users registered on that day
        uc = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE DATE(created_at) = :date"),
            {"date": date_str}
        ).scalar()
        user_counts.append(uc)

        # Listings created on that day
        lc = conn.execute(
            text("SELECT COUNT(*) FROM listings WHERE DATE(created_at) = :date"),
            {"date": date_str}
        ).scalar()
        listing_counts.append(lc)

        # Revenue (sum of payments on that day)
        rev = conn.execute(
            text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status='verified' AND DATE(created_at) = :date"),
            {"date": date_str}
        ).scalar()
        revenue_daily.append(rev)

    return jsonify({
        "labels": dates,
        "users": user_counts,
        "listings": listing_counts,
        "revenue": revenue_daily
    })