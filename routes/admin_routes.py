import io
import os
from flask import Blueprint, jsonify, render_template, request, send_file, redirect, make_response, url_for
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity, create_access_token, set_access_cookies
from database.init_db import get_db_connection
from functools import wraps
from datetime import timedelta, datetime
from sqlalchemy import text
from flask_limiter.util import get_remote_address
from services.sms_service import get_sms_service
from services.audit_service import log_admin_action
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
import logging
logger = logging.getLogger(__name__)
from services.payment_service import activate_subscription
from services.authz import db_user_is_admin

admin_bp = Blueprint("admin", __name__)

def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        if not db_user_is_admin(get_jwt_identity()):
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper

def get_admin_info():
    """Helper to get current admin id and phone from JWT (id or phone identity)."""
    identity = get_jwt_identity()
    conn = get_db_connection()
    try:
        if str(identity).isdigit():
            user = conn.execute(
                text("SELECT id, phone, role FROM users WHERE id = :id"),
                {"id": int(identity)},
            ).fetchone()
        else:
            user = conn.execute(
                text("SELECT id, phone, role FROM users WHERE phone = :phone"),
                {"phone": identity},
            ).fetchone()
        if not user or (user._mapping.get("role") or "") != "admin":
            return None, None
        return user._mapping["id"], user._mapping["phone"]
    finally:
        try:
            conn.close()
        except Exception:
            pass

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

    conn = get_db_connection()
    try:
        # Razorpay rows are stored as status='activated' with amount in paise.
        # Legacy/admin-approved rows use status='verified' (amount in rupees).
        row = conn.execute(text("""
            SELECT COALESCE(SUM(
                CASE
                    WHEN status = 'activated' THEN amount / 100.0
                    ELSE amount
                END
            ), 0)
            FROM payments
            WHERE status IN ('verified', 'activated')
        """)).scalar()
        revenue = float(row or 0)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    stats["revenue"] = round(revenue, 2)
    stats["total_revenue"] = stats["revenue"]
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

def _load_admin_user(user_id):
    conn = get_db_connection()
    try:
        row = conn.execute(
            text("""
                SELECT u.id, u.phone, u.name, u.role, u.plan, u.subscription_expiry,
                       COALESCE(wb.balance, 0) AS wallet_balance, u.is_blocked, u.referral_code, u.created_at,
                       u.latitude, u.longitude
                FROM users u
                LEFT JOIN wallet_balance wb ON wb.user_id = u.id
                WHERE u.id = :uid
            """),
            {"uid": user_id},
        ).fetchone()
        return dict(row._mapping) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_admin_user_payments(user_id):
    conn = get_db_connection()
    try:
        rows = conn.execute(
            text("""
                SELECT id, amount, status, created_at
                FROM payments
                WHERE user_id = :uid
                ORDER BY created_at DESC, id DESC
            """),
            {"uid": user_id},
        ).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_bp.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail_page(user_id):
    user = _load_admin_user(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    payments = _load_admin_user_payments(user_id)
    return render_template("admin/admin_user_detail.html", user=user, payments=payments)


@admin_bp.route("/api/admin/users/<int:user_id>", methods=["GET"])
@admin_required
def api_user_detail(user_id):
    """Keep old links working; HTML lives under /admin so JWT cookies stay on /."""
    return redirect(url_for("admin.admin_user_detail_page", user_id=user_id))

@admin_bp.route("/api/admin/users/<int:user_id>/ban", methods=["POST"])
@admin_required
def api_ban_user(user_id):
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = ban_user(user_id, admin_id, admin_phone, ip)
    # Log it
    log_admin_action(admin_id, admin_phone, "ban_user", "user", user_id,
                     details="User banned", ip_address=ip)
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
    conn = get_db_connection()
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
    if result.get("error"):
        return jsonify(result), result.get("_http") or 409
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
    frozen = {
        "withdrawal_min_amount",
        "withdrawal_max_amount",
        "commission_rate",
        "referral_bonus_percent",
        "recurring_commission_percent",
    }
    if key in frozen:
        return jsonify({
            "error": "This setting is frozen and cannot be changed from the admin API",
            "key": key,
        }), 403
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

@admin_bp.route("/api/admin/trigger-payout", methods=["POST"])
@admin_required
def admin_trigger_payout():
    from app import _execute_payout
    released = _execute_payout()
    return jsonify({"released": released})

@admin_bp.route("/api/admin/users/<int:user_id>/impersonate", methods=["POST"])
@admin_required
def impersonate_user(user_id):
    conn = get_db_connection()
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
    resp = make_response(jsonify({
        "success": True,
        "phone": user._mapping["phone"],
        "redirect": "/api/user/dashboard",
    }))
    set_access_cookies(resp, token, max_age=600)
    return resp

@admin_bp.route("/api/admin/stats/chart")
@admin_required
def admin_chart_data():
    conn = get_db_connection()
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

@admin_bp.route("/api/admin/audit-log")
@admin_required
def api_audit_log():
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    offset = (page - 1) * limit

    conn = get_db_connection()
    rows = conn.execute(text("""
        SELECT * FROM admin_audit_log
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """), {"limit": limit, "offset": offset}).fetchall()

    total = conn.execute(text("SELECT COUNT(*) FROM admin_audit_log")).scalar()

    logs = [dict(r._mapping) for r in rows]
    return jsonify({"logs": logs, "total": total, "page": page})

# ============================
# VERIFY REFERRAL (CONVERT PENDING TO CREDIT)
# ============================
@admin_bp.route("/api/admin/referrals/<int:ref_id>/verify", methods=["POST"])
@admin_required
def verify_referral(ref_id):
    """LEGACY. Must not credit wallets or mutate spendable ledger.

    Live commissions: services.referral_commission.
    """
    return jsonify({
        "success": False,
        "error": "This endpoint is disabled. Referral commissions are processed automatically.",
    }), 410


# ============================
# MARK WITHDRAWAL AS PAID (WITH FIRST WITHDRAWAL FLAG)
# ============================
@admin_bp.route("/api/admin/withdrawals/<int:wid>/paid-with-flag", methods=["POST"])
@admin_required
def mark_withdraw_paid_with_flag(wid):
    """Mark withdrawal as paid using the canonical state machine."""
    admin_id, admin_phone = get_admin_info()
    ip = request.remote_addr
    result = mark_withdraw_paid(wid, admin_id, admin_phone, ip)
    if result.get("error"):
        return jsonify({"error": result["error"]}), 400
    return jsonify({"message": "Withdrawal marked as paid", "status": "paid"}), 200


# ============================
# ONE-TIME MIGRATION (RUN ONCE, THEN REMOVE)
# ============================
@admin_bp.route("/api/admin/run-migration/withdrawal-policy", methods=["POST"])
@admin_required
def run_withdrawal_policy_migration():
    """Disabled: must not ALTER production from a live HTTP route."""
    return jsonify({
        "success": False,
        "error": "This migration endpoint is disabled",
    }), 410

 
@admin_bp.route("/api/send-sms", methods=["POST"])
@jwt_required()
def send_sms():
    """Disabled: arbitrary SMS send is an abuse and cost vector."""
    return jsonify({"success": False, "error": "This endpoint is disabled"}), 410


@admin_bp.route("/api/send-otp", methods=["POST"])
@jwt_required()
def send_otp():
    """Disabled: must not send OTP or return codes outside /api/auth/send-otp."""
    return jsonify({"success": False, "error": "This endpoint is disabled"}), 410
    
@admin_bp.route("/api/test-sms-ui", methods=["GET"])
@jwt_required()
def test_sms_ui():
    """Disabled development SMS console."""
    return jsonify({"success": False, "error": "This endpoint is disabled"}), 410


@admin_bp.route("/api/make-me-admin", methods=["GET"])
def make_me_admin():
    """Disabled: unauthenticated role grant is a financial-control bypass."""
    return jsonify({
        "success": False,
        "error": "This endpoint is disabled",
    }), 410

