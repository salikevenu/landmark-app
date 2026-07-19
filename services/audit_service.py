# services/audit_service.py
from sqlalchemy import text
from database.init_db import get_db_connection
from datetime import datetime

def log_admin_action(admin_id, admin_phone, action, target_type, target_id, details=None, ip_address=None):
    conn = get_db_connection()
    conn.execute(text("""
        INSERT INTO admin_audit_log
            (admin_id, admin_phone, action, target_type, target_id, details, ip_address, created_at)
        VALUES (:admin_id, :admin_phone, :action, :target_type, :target_id, :details, :ip_address, NOW())
    """), {
        "admin_id": admin_id,
        "admin_phone": admin_phone,
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id) if target_id else None,
        "details": details,
        "ip_address": ip_address
    })
    conn.commit()