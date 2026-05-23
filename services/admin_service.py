import csv
import io
from datetime import datetime, timedelta
from sqlalchemy import text
from database.init_db import get_db
from services.wallet_service import credit_wallet, debit_wallet
from services.payment_service import activate_subscription

# Helper to convert SQLAlchemy row to dict (for compatibility with old code)
def _row_to_dict(row):
    return dict(row._mapping) if row else None

# -------------------------------
# AUDIT LOGGING
# -------------------------------
def log_admin_action(admin_id, admin_phone, action, target_type, target_id, details, ip_address):
    conn = get_db()
    conn.execute(
        text("""
            INSERT INTO admin_audit_log (admin_id, admin_phone, action, target_type, target_id, details, ip_address)
            VALUES (:admin_id, :admin_phone, :action, :target_type, :target_id, :details, :ip_address)
        """),
        {
            "admin_id": admin_id,
            "admin_phone": admin_phone,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "details": details,
            "ip_address": ip_address
        }
    )
    conn.commit()

# -------------------------------
# DASHBOARD STATS (Advanced)
# -------------------------------
def get_admin_stats(period='week'):
    conn = get_db()
    now = datetime.utcnow()
    if period == 'day':
        start_date = now - timedelta(days=1)
    elif period == 'week':
        start_date = now - timedelta(days=7)
    elif period == 'month':
        start_date = now - timedelta(days=30)
    else:
        start_date = None

    stats = {}

    # Total active users (not blocked)
    result = conn.execute(text("SELECT COUNT(*) FROM users WHERE is_blocked = 0"))
    stats['total_users'] = result.scalar()

    result = conn.execute(text("SELECT COUNT(*) FROM listings WHERE is_active = 1"))
    stats['total_listings'] = result.scalar()

    result = conn.execute(text("SELECT COUNT(*) FROM payments WHERE status = 'verified'"))
    stats['total_payments'] = result.scalar()

    result = conn.execute(text("SELECT COUNT(*) FROM withdraw_requests"))
    stats['total_withdrawals'] = result.scalar()

    result = conn.execute(text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status='verified'"))
    stats['total_revenue'] = result.scalar()

    result = conn.execute(text("SELECT COALESCE(SUM(amount), 0) FROM withdraw_requests WHERE status='pending'"))
    stats['pending_withdrawals'] = result.scalar()

    # Time-series data
    if start_date:
        rows = conn.execute(
            text("""
                SELECT DATE(created_at) as date, COUNT(*) as count, COALESCE(SUM(amount),0) as revenue
                FROM payments
                WHERE status='verified' AND created_at >= :start_date
                GROUP BY DATE(created_at)
                ORDER BY date
            """),
            {"start_date": start_date}
        ).fetchall()
        stats['revenue_series'] = [{"date": r[0], "count": r[1], "revenue": r[2]} for r in rows]
    else:
        stats['revenue_series'] = []

    # Top categories
    rows = conn.execute(
        text("""
            SELECT category, COUNT(*) as count
            FROM listings
            WHERE is_active = 1 AND category IS NOT NULL
            GROUP BY category
            ORDER BY count DESC
            LIMIT 5
        """)
    ).fetchall()
    stats['top_categories'] = [{"category": r[0], "count": r[1]} for r in rows]

    # Recent payments
    rows = conn.execute(
        text("""
            SELECT id, user_phone, amount, created_at
            FROM payments
            WHERE status='verified'
            ORDER BY created_at DESC LIMIT 5
        """)
    ).fetchall()
    stats['recent_payments'] = [{"id": r[0], "user_phone": r[1], "amount": r[2], "created_at": r[3]} for r in rows]

    # Recent users
    rows = conn.execute(
        text("""
            SELECT id, phone, name, created_at
            FROM users
            ORDER BY created_at DESC LIMIT 5
        """)
    ).fetchall()
    stats['recent_users'] = [{"id": r[0], "phone": r[1], "name": r[2], "created_at": r[3]} for r in rows]

    conn.close()
    return stats

# -------------------------------
# USER MANAGEMENT
# -------------------------------
def get_admin_users(page=1, limit=50, search='', role_filter='', status_filter=''):
    conn = get_db()
    offset = (page - 1) * limit
    params = {}
    where_clauses = []

    if search:
        where_clauses.append("(phone LIKE :search OR name LIKE :search)")
        params['search'] = f'%{search}%'
    if role_filter:
        where_clauses.append("role = :role_filter")
        params['role_filter'] = role_filter
    if status_filter == 'active':
        where_clauses.append("is_blocked = 0")
    elif status_filter == 'banned':
        where_clauses.append("is_blocked = 1")

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    count_query = f"SELECT COUNT(*) FROM users WHERE {where_sql}"
    total = conn.execute(text(count_query), params).scalar()

    query = f"""
        SELECT id, phone, name, role, subscription_expiry, wallet_balance, is_blocked, created_at
        FROM users
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT :limit OFFSET :offset
    """
    params['limit'] = limit
    params['offset'] = offset
    rows = conn.execute(text(query), params).fetchall()

    users = []
    for r in rows:
        u = {
            "id": r[0],
            "phone": r[1],
            "name": r[2],
            "role": r[3],
            "subscription_expiry": r[4],
            "wallet_balance": r[5],
            "is_blocked": r[6],
            "created_at": r[7]
        }
        if u['subscription_expiry']:
            expiry_date = datetime.strptime(u['subscription_expiry'], '%Y-%m-%d')
            u['subscription_status'] = 'active' if expiry_date > datetime.utcnow() else 'expired'
        else:
            u['subscription_status'] = 'inactive'
        users.append(u)
    conn.close()
    return {'users': users, 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

def ban_user(user_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE users SET is_blocked = 1 WHERE id = :user_id"), {"user_id": user_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'ban', 'user', str(user_id), f'Banned user {user_id}', ip)
    conn.close()
    return {'status': 'banned'}

def unban_user(user_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE users SET is_blocked = 0 WHERE id = :user_id"), {"user_id": user_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'unban', 'user', str(user_id), f'Unbanned user {user_id}', ip)
    conn.close()
    return {'status': 'unbanned'}

def change_user_role(user_id, new_role, admin_id, admin_phone, ip):
    valid_roles = ['user', 'business_basic', 'business_premium', 'admin']
    if new_role not in valid_roles:
        return {'error': 'Invalid role'}
    conn = get_db()
    conn.execute(text("UPDATE users SET role = :role WHERE id = :user_id"), {"role": new_role, "user_id": user_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'change_role', 'user', str(user_id), f'Role changed to {new_role}', ip)
    conn.close()
    return {'status': 'role_updated'}

def reset_user_subscription(user_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE users SET role = 'user', subscription_expiry = NULL WHERE id = :user_id"), {"user_id": user_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'reset_subscription', 'user', str(user_id), 'Subscription reset', ip)
    conn.close()
    return {'status': 'subscription_reset'}

# -------------------------------
# LISTING MANAGEMENT
# -------------------------------
def get_admin_listings(page=1, limit=50, search='', status_filter='', category_filter=''):
    conn = get_db()
    offset = (page - 1) * limit
    params = {}
    where_clauses = []

    if search:
        where_clauses.append("(business_name LIKE :search OR city LIKE :search OR user_phone LIKE :search)")
        params['search'] = f'%{search}%'
    if status_filter:
        where_clauses.append("status = :status_filter")
        params['status_filter'] = status_filter
    if category_filter:
        where_clauses.append("category = :category_filter")
        params['category_filter'] = category_filter

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    count_query = f"SELECT COUNT(*) FROM listings WHERE {where_sql}"
    total = conn.execute(text(count_query), params).scalar()

    query = f"""
        SELECT id, business_name, category, city, user_phone, status, is_verified, is_premium, is_active, created_at
        FROM listings
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT :limit OFFSET :offset
    """
    params['limit'] = limit
    params['offset'] = offset
    rows = conn.execute(text(query), params).fetchall()
    listings = [{"id": r[0], "business_name": r[1], "category": r[2], "city": r[3], "user_phone": r[4],
                 "status": r[5], "is_verified": r[6], "is_premium": r[7], "is_active": r[8], "created_at": r[9]} for r in rows]
    conn.close()
    return {'listings': listings, 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

def approve_listing_admin(listing_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE listings SET status = 'approved', is_active = 1 WHERE id = :listing_id"), {"listing_id": listing_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'approve_listing', 'listing', str(listing_id), f'Approved listing {listing_id}', ip)
    conn.close()
    return {'status': 'approved'}

def disable_listing_admin(listing_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE listings SET is_active = 0 WHERE id = :listing_id"), {"listing_id": listing_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'disable_listing', 'listing', str(listing_id), f'Disabled listing {listing_id}', ip)
    conn.close()
    return {'status': 'disabled'}

def verify_listing_admin(listing_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE listings SET is_verified = 1 WHERE id = :listing_id"), {"listing_id": listing_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'verify_listing', 'listing', str(listing_id), f'Verified listing {listing_id}', ip)
    conn.close()
    return {'status': 'verified'}

def delete_listing_admin(listing_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("DELETE FROM listings WHERE id = :listing_id"), {"listing_id": listing_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'delete_listing', 'listing', str(listing_id), f'Deleted listing {listing_id}', ip)
    conn.close()
    return {'status': 'deleted'}

def sponsor_listing_admin(listing_id, admin_id, admin_phone, ip):
    from services.listing_service import sponsor_listing_service
    result = sponsor_listing_service(listing_id)
    if result.get('status') == 'sponsored':
        log_admin_action(admin_id, admin_phone, 'sponsor_listing', 'listing', str(listing_id), f'Sponsored listing {listing_id}', ip)
    return result

# -------------------------------
# PAYMENT MANAGEMENT
# -------------------------------
def get_admin_payments(page=1, limit=50, search='', status_filter='', start_date=None, end_date=None):
    conn = get_db()
    offset = (page - 1) * limit
    params = {}
    where_clauses = []

    if search:
        where_clauses.append("(user_phone LIKE :search OR payment_id LIKE :search)")
        params['search'] = f'%{search}%'
    if status_filter:
        where_clauses.append("status = :status_filter")
        params['status_filter'] = status_filter
    if start_date:
        where_clauses.append("created_at >= :start_date")
        params['start_date'] = start_date
    if end_date:
        where_clauses.append("created_at <= :end_date")
        params['end_date'] = end_date + " 23:59:59"

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    count_query = f"SELECT COUNT(*) FROM payments WHERE {where_sql}"
    total = conn.execute(text(count_query), params).scalar()

    query = f"""
        SELECT id, user_id, user_phone, amount, status, created_at
        FROM payments
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT :limit OFFSET :offset
    """
    params['limit'] = limit
    params['offset'] = offset
    rows = conn.execute(text(query), params).fetchall()
    payments = [{"id": r[0], "user_id": r[1], "user_phone": r[2], "amount": r[3], "status": r[4], "created_at": r[5]} for r in rows]
    conn.close()
    return {'payments': payments, 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

def approve_payment_admin(payment_id, admin_id, admin_phone, ip):
    conn = get_db()
    payment = conn.execute(
        text("SELECT user_id, amount, user_phone FROM payments WHERE id = :payment_id AND status='pending'"),
        {"payment_id": payment_id}
    ).fetchone()
    if not payment:
        return {'error': 'Payment not found or already processed'}
    # Assuming default plan 'business_basic'
    expiry = activate_subscription(payment[2], 'business_basic', 30)
    conn.execute(text("UPDATE payments SET status='verified' WHERE id = :payment_id"), {"payment_id": payment_id})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'approve_payment', 'payment', str(payment_id), f'Approved payment {payment_id}', ip)
    conn.close()
    return {'status': 'approved', 'expiry': expiry}

# -------------------------------
# WITHDRAWAL MANAGEMENT
# -------------------------------
def get_withdraw_requests(page=1, limit=50, status_filter=''):
    conn = get_db()
    offset = (page - 1) * limit
    params = {}
    where_clause = ""
    if status_filter:
        where_clause = "WHERE wr.status = :status"
        params['status'] = status_filter
    else:
        where_clause = "WHERE 1=1"

    count_query = f"SELECT COUNT(*) FROM withdraw_requests wr {where_clause}"
    total = conn.execute(text(count_query), params).scalar()

    query = f"""
        SELECT wr.id, u.phone, u.name, wr.amount, wr.upi_id, wr.status, wr.created_at
        FROM withdraw_requests wr
        JOIN users u ON wr.user_id = u.id
        {where_clause}
        ORDER BY wr.id DESC
        LIMIT :limit OFFSET :offset
    """
    params['limit'] = limit
    params['offset'] = offset
    rows = conn.execute(text(query), params).fetchall()
    withdrawals = [{"id": r[0], "phone": r[1], "name": r[2], "amount": r[3], "upi_id": r[4], "status": r[5], "created_at": r[6]} for r in rows]
    conn.close()
    return {'withdrawals': withdrawals, 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

def approve_withdraw_request(wid, admin_id, admin_phone, ip):
    conn = get_db()
    row = conn.execute(
        text("SELECT user_id, amount FROM withdraw_requests WHERE id = :wid AND status='pending'"),
        {"wid": wid}
    ).fetchone()
    if not row:
        return {'error': 'Withdrawal not found or already processed'}
    success = debit_wallet(row[0], row[1], f"Withdraw approved WD-{wid}", f"WD-{wid}")
    if not success:
        return {'error': 'Insufficient balance'}
    conn.execute(text("UPDATE withdraw_requests SET status = 'approved' WHERE id = :wid"), {"wid": wid})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'approve_withdraw', 'withdraw', str(wid), f'Approved withdrawal {wid}', ip)
    conn.close()
    return {'status': 'approved'}

def reject_withdraw_request(wid, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE withdraw_requests SET status = 'rejected' WHERE id = :wid"), {"wid": wid})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'reject_withdraw', 'withdraw', str(wid), f'Rejected withdrawal {wid}', ip)
    conn.close()
    return {'status': 'rejected'}

def mark_withdraw_paid(wid, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE withdraw_requests SET status = 'paid' WHERE id = :wid"), {"wid": wid})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'mark_paid', 'withdraw', str(wid), f'Marked withdrawal {wid} as paid', ip)
    conn.close()
    return {'status': 'paid'}

def bulk_approve_withdrawals(wids, admin_id, admin_phone, ip):
    results = []
    for wid in wids:
        res = approve_withdraw_request(wid, admin_id, admin_phone, ip)
        results.append({'id': wid, 'status': res.get('status', 'error'), 'error': res.get('error')})
    return results

# -------------------------------
# REFERRAL MANAGEMENT
# -------------------------------
def get_admin_referrals(page=1, limit=50, search=''):
    conn = get_db()
    offset = (page - 1) * limit
    params = {}
    where_clause = "WHERE 1=1"
    if search:
        where_clause += " AND (u1.phone LIKE :search OR u2.phone LIKE :search)"
        params['search'] = f'%{search}%'

    count_query = f"""
        SELECT COUNT(*) FROM referral_transactions rt
        JOIN users u1 ON rt.referrer_id = u1.id
        JOIN users u2 ON rt.referred_user_id = u2.id
        {where_clause}
    """
    total = conn.execute(text(count_query), params).scalar()

    query = f"""
        SELECT rt.id, u1.phone AS referrer_phone, u2.phone AS referred_phone,
               rt.reward_amount, rt.status, rt.created_at
        FROM referral_transactions rt
        JOIN users u1 ON rt.referrer_id = u1.id
        JOIN users u2 ON rt.referred_user_id = u2.id
        {where_clause}
        ORDER BY rt.id DESC
        LIMIT :limit OFFSET :offset
    """
    params['limit'] = limit
    params['offset'] = offset
    rows = conn.execute(text(query), params).fetchall()
    referrals = [{"id": r[0], "referrer_phone": r[1], "referred_phone": r[2], "reward_amount": r[3], "status": r[4], "created_at": r[5]} for r in rows]
    conn.close()
    return {'referrals': referrals, 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

# -------------------------------
# CSV EXPORTS
# -------------------------------
def export_users_csv():
    conn = get_db()
    rows = conn.execute(text("SELECT id, phone, name, role, subscription_expiry, wallet_balance, is_blocked, created_at FROM users ORDER BY id")).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Phone', 'Name', 'Role', 'Subscription Expiry', 'Wallet Balance', 'Banned', 'Created At'])
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]])
    conn.close()
    return output.getvalue()

def export_payments_csv():
    conn = get_db()
    rows = conn.execute(text("SELECT id, user_phone, amount, status, created_at FROM payments ORDER BY id")).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'User Phone', 'Amount', 'Status', 'Created At'])
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], r[4]])
    conn.close()
    return output.getvalue()

def export_withdrawals_csv():
    conn = get_db()
    rows = conn.execute(text("""
        SELECT wr.id, u.phone, wr.amount, wr.upi_id, wr.status, wr.created_at
        FROM withdraw_requests wr JOIN users u ON wr.user_id = u.id
        ORDER BY wr.id
    """)).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'User Phone', 'Amount', 'UPI ID', 'Status', 'Created At'])
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5]])
    conn.close()
    return output.getvalue()

# -------------------------------
# SYSTEM SETTINGS
# -------------------------------
def get_settings():
    conn = get_db()
    rows = conn.execute(text("SELECT key, value FROM admin_settings")).fetchall()
    settings = {r[0]: r[1] for r in rows}
    conn.close()
    return settings

def update_setting(key, value, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute(text("UPDATE admin_settings SET value = :value, updated_at = CURRENT_TIMESTAMP WHERE key = :key"), {"value": value, "key": key})
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'update_setting', 'setting', key, f'Set {key}={value}', ip)
    conn.close()
    return {'status': 'updated'}