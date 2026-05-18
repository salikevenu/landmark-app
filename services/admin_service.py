import csv
import io
from datetime import datetime, timedelta
from database.init_db import get_db
from services.wallet_service import credit_wallet, debit_wallet
from services.payment_service import activate_subscription

# -------------------------------
# AUDIT LOGGING
# -------------------------------
def log_admin_action(admin_id, admin_phone, action, target_type, target_id, details, ip_address):
    conn = get_db()
    conn.execute("""
        INSERT INTO admin_audit_log (admin_id, admin_phone, action, target_type, target_id, details, ip_address)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (admin_id, admin_phone, action, target_type, target_id, details, ip_address))
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
    stats['total_users'] = conn.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 0").fetchone()[0]
    stats['total_listings'] = conn.execute("SELECT COUNT(*) FROM listings WHERE is_active = 1").fetchone()[0]
    stats['total_payments'] = conn.execute("SELECT COUNT(*) FROM payments WHERE status = 'verified'").fetchone()[0]
    stats['total_withdrawals'] = conn.execute("SELECT COUNT(*) FROM withdraw_requests").fetchone()[0]
    stats['total_revenue'] = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status='verified'").fetchone()[0]
    stats['pending_withdrawals'] = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM withdraw_requests WHERE status='pending'").fetchone()[0]

    # Time-series data
    if start_date:
        rows = conn.execute("""
            SELECT DATE(created_at) as date, COUNT(*) as count, COALESCE(SUM(amount),0) as revenue
            FROM payments
            WHERE status='verified' AND created_at >= ?
            GROUP BY DATE(created_at)
            ORDER BY date
        """, (start_date,)).fetchall()
        stats['revenue_series'] = [{'date': r['date'], 'revenue': r['revenue'], 'count': r['count']} for r in rows]
    else:
        stats['revenue_series'] = []

    # Top categories
    top_cats = conn.execute("""
        SELECT category, COUNT(*) as count
        FROM listings
        WHERE is_active = 1 AND category IS NOT NULL
        GROUP BY category
        ORDER BY count DESC
        LIMIT 5
    """).fetchall()
    stats['top_categories'] = [dict(r) for r in top_cats]

    # Recent activity
    recent_payments = conn.execute("""
        SELECT id, user_phone, amount, created_at FROM payments
        WHERE status='verified'
        ORDER BY created_at DESC LIMIT 5
    """).fetchall()
    stats['recent_payments'] = [dict(r) for r in recent_payments]

    recent_users = conn.execute("""
        SELECT id, phone, name, created_at FROM users
        ORDER BY created_at DESC LIMIT 5
    """).fetchall()
    stats['recent_users'] = [dict(r) for r in recent_users]

    return stats

# -------------------------------
# USER MANAGEMENT
# -------------------------------
def get_admin_users(page=1, limit=50, search='', role_filter='', status_filter=''):
    conn = get_db()
    offset = (page - 1) * limit
    params = []
    where_clause = "WHERE 1=1"
    if search:
        where_clause += " AND (phone LIKE ? OR name LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])
    if role_filter:
        where_clause += " AND role = ?"
        params.append(role_filter)
    if status_filter == 'active':
        where_clause += " AND is_blocked = 0"
    elif status_filter == 'banned':
        where_clause += " AND is_blocked = 1"

    count_query = f"SELECT COUNT(*) FROM users {where_clause}"
    total = conn.execute(count_query, params).fetchone()[0]

    query = f"""
        SELECT id, phone, name, role, subscription_expiry, wallet_balance, is_blocked, created_at
        FROM users {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    users = []
    for r in rows:
        u = dict(r)
        # Compute subscription status from expiry date
        if u['subscription_expiry']:
            expiry_date = datetime.strptime(u['subscription_expiry'], '%Y-%m-%d')
            u['subscription_status'] = 'active' if expiry_date > datetime.utcnow() else 'expired'
        else:
            u['subscription_status'] = 'inactive'
        users.append(u)
    return {'users': users, 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

def ban_user(user_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE users SET is_blocked = 1 WHERE id = ?", (user_id,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'ban', 'user', str(user_id), f'Banned user {user_id}', ip)
    return {'status': 'banned'}

def unban_user(user_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE users SET is_blocked = 0 WHERE id = ?", (user_id,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'unban', 'user', str(user_id), f'Unbanned user {user_id}', ip)
    return {'status': 'unbanned'}

def change_user_role(user_id, new_role, admin_id, admin_phone, ip):
    valid_roles = ['user', 'business_basic', 'business_premium', 'admin']
    if new_role not in valid_roles:
        return {'error': 'Invalid role'}
    conn = get_db()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'change_role', 'user', str(user_id), f'Role changed to {new_role}', ip)
    return {'status': 'role_updated'}

def reset_user_subscription(user_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE users SET role = 'user', subscription_expiry = NULL WHERE id = ?", (user_id,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'reset_subscription', 'user', str(user_id), 'Subscription reset', ip)
    return {'status': 'subscription_reset'}

# -------------------------------
# LISTING MANAGEMENT
# -------------------------------
def get_admin_listings(page=1, limit=50, search='', status_filter='', category_filter=''):
    conn = get_db()
    offset = (page - 1) * limit
    params = []
    where_clause = "WHERE 1=1"
    if search:
        where_clause += " AND (business_name LIKE ? OR city LIKE ? OR phone LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    if status_filter:
        where_clause += " AND status = ?"
        params.append(status_filter)
    if category_filter:
        where_clause += " AND category = ?"
        params.append(category_filter)

    count_query = f"SELECT COUNT(*) FROM listings {where_clause}"
    total = conn.execute(count_query, params).fetchone()[0]

    query = f"""
        SELECT id, business_name, category, city, phone, status, is_verified, is_premium, is_active, created_at
        FROM listings {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return {'listings': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

def approve_listing_admin(listing_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE listings SET status = 'approved', is_active = 1 WHERE id = ?", (listing_id,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'approve_listing', 'listing', str(listing_id), f'Approved listing {listing_id}', ip)
    return {'status': 'approved'}

def disable_listing_admin(listing_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE listings SET is_active = 0 WHERE id = ?", (listing_id,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'disable_listing', 'listing', str(listing_id), f'Disabled listing {listing_id}', ip)
    return {'status': 'disabled'}

def verify_listing_admin(listing_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE listings SET is_verified = 1 WHERE id = ?", (listing_id,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'verify_listing', 'listing', str(listing_id), f'Verified listing {listing_id}', ip)
    return {'status': 'verified'}

def delete_listing_admin(listing_id, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'delete_listing', 'listing', str(listing_id), f'Deleted listing {listing_id}', ip)
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
    params = []
    where_clause = "WHERE 1=1"
    if search:
        where_clause += " AND (user_phone LIKE ? OR payment_id LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])
    if status_filter:
        where_clause += " AND status = ?"
        params.append(status_filter)

    count_query = f"SELECT COUNT(*) FROM payments {where_clause}"
    total = conn.execute(count_query, params).fetchone()[0]

    query = "SELECT * FROM payments WHERE 1=1"
    params = []
    if start_date:
        query += " AND created_at >= ?"
        params.append(start_date)
    if end_date:
        query += " AND created_at <= ?"
        params.append(end_date + " 23:59:59")

    query = f"""
        SELECT id, user_id, user_phone, amount, status, created_at
        FROM payments {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return {'payments': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

def approve_payment_admin(payment_id, admin_id, admin_phone, ip):
    conn = get_db()
    payment = conn.execute("SELECT user_id, amount, user_phone FROM payments WHERE id = ? AND status='pending'", (payment_id,)).fetchone()
    if not payment:
        return {'error': 'Payment not found or already processed'}
    # Activate subscription (you need plan info – adjust as needed)
    # For now, assume default plan 'business_basic'
    expiry = activate_subscription(payment['user_phone'], 'business_basic', 30)
    conn.execute("UPDATE payments SET status='verified' WHERE id=?", (payment_id,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'approve_payment', 'payment', str(payment_id), f'Approved payment {payment_id}', ip)
    return {'status': 'approved', 'expiry': expiry}

# -------------------------------
# WITHDRAWAL MANAGEMENT
# -------------------------------
def get_withdraw_requests(page=1, limit=50, status_filter=''):
    conn = get_db()
    offset = (page - 1) * limit
    params = []
    where_clause = "WHERE 1=1"
    if status_filter:
        where_clause += " AND status = ?"
        params.append(status_filter)

    count_query = f"SELECT COUNT(*) FROM withdraw_requests {where_clause}"
    total = conn.execute(count_query, params).fetchone()[0]

    query = f"""
        SELECT wr.id, u.phone, u.name, wr.amount, wr.upi_id, wr.status, wr.created_at
        FROM withdraw_requests wr
        JOIN users u ON wr.user_id = u.id
        {where_clause}
        ORDER BY wr.id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return {'withdrawals': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

def approve_withdraw_request(wid, admin_id, admin_phone, ip):
    conn = get_db()
    row = conn.execute("SELECT user_id, amount FROM withdraw_requests WHERE id = ? AND status='pending'", (wid,)).fetchone()
    if not row:
        return {'error': 'Withdrawal not found or already processed'}
    success = debit_wallet(row['user_id'], row['amount'], f"Withdraw approved WD-{wid}", f"WD-{wid}")
    if not success:
        return {'error': 'Insufficient balance'}
    conn.execute("UPDATE withdraw_requests SET status = 'approved' WHERE id = ?", (wid,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'approve_withdraw', 'withdraw', str(wid), f'Approved withdrawal {wid}', ip)
    return {'status': 'approved'}

def reject_withdraw_request(wid, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE withdraw_requests SET status = 'rejected' WHERE id = ?", (wid,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'reject_withdraw', 'withdraw', str(wid), f'Rejected withdrawal {wid}', ip)
    return {'status': 'rejected'}

def mark_withdraw_paid(wid, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE withdraw_requests SET status = 'paid' WHERE id = ?", (wid,))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'mark_paid', 'withdraw', str(wid), f'Marked withdrawal {wid} as paid', ip)
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
    params = []
    where_clause = "WHERE 1=1"
    if search:
        where_clause += " AND (u1.phone LIKE ? OR u2.phone LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])

    count_query = f"""
        SELECT COUNT(*) FROM referral_transactions rt
        JOIN users u1 ON rt.referrer_id = u1.id
        JOIN users u2 ON rt.referred_user_id = u2.id
        {where_clause}
    """
    total = conn.execute(count_query, params).fetchone()[0]

    query = f"""
        SELECT rt.id, u1.phone AS referrer_phone, u2.phone AS referred_phone,
               rt.reward_amount, rt.status, rt.created_at
        FROM referral_transactions rt
        JOIN users u1 ON rt.referrer_id = u1.id
        JOIN users u2 ON rt.referred_user_id = u2.id
        {where_clause}
        ORDER BY rt.id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return {'referrals': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit, 'pages': (total + limit - 1) // limit}

# -------------------------------
# CSV EXPORTS
# -------------------------------
def export_users_csv():
    conn = get_db()
    rows = conn.execute("SELECT id, phone, name, role, subscription_expiry, wallet_balance, is_banned, created_at FROM users ORDER BY id").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Phone', 'Name', 'Role', 'Subscription Expiry', 'Wallet Balance', 'Banned', 'Created At'])
    for r in rows:
        writer.writerow([r['id'], r['phone'], r['name'], r['role'], r['subscription_expiry'], r['wallet_balance'], r['is_banned'], r['created_at']])
    return output.getvalue()

def export_payments_csv():
    conn = get_db()
    rows = conn.execute("SELECT id, user_phone, amount, status, created_at FROM payments ORDER BY id").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'User Phone', 'Amount', 'Status', 'Created At'])
    for r in rows:
        writer.writerow([r['id'], r['user_phone'], r['amount'], r['status'], r['created_at']])
    return output.getvalue()

def export_withdrawals_csv():
    conn = get_db()
    rows = conn.execute("""
        SELECT wr.id, u.phone, wr.amount, wr.upi_id, wr.status, wr.created_at
        FROM withdraw_requests wr JOIN users u ON wr.user_id = u.id
        ORDER BY wr.id
    """).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'User Phone', 'Amount', 'UPI ID', 'Status', 'Created At'])
    for r in rows:
        writer.writerow([r['id'], r['phone'], r['amount'], r['upi_id'], r['status'], r['created_at']])
    return output.getvalue()

# -------------------------------
# SYSTEM SETTINGS
# -------------------------------
def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM admin_settings").fetchall()
    return {r['key']: r['value'] for r in rows}

def update_setting(key, value, admin_id, admin_phone, ip):
    conn = get_db()
    conn.execute("UPDATE admin_settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?", (value, key))
    conn.commit()
    log_admin_action(admin_id, admin_phone, 'update_setting', 'setting', key, f'Set {key}={value}', ip)
    return {'status': 'updated'}