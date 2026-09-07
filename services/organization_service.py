"""Business Power V2 — organization business logic.

Routes stay thin; this module owns the actual DB work. Every function that
returns data to a non-owner-context caller goes through
services/organization_authz.py first -- this module does not re-implement
authorization decisions.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database.init_db import get_db_connection
from services.audit_service import log_admin_action
from services.organization_authz import (
    authorize_organization,
    authorize_organization_listing,
    CAN_VIEW_ORGANIZATION,
    CAN_INVITE_MEMBERS,
    CAN_CHANGE_ROLE,
    CAN_REMOVE_MEMBER,
    CAN_CREATE_BUSINESS,
    CAN_EDIT_BUSINESS,
    CAN_DELETE_BUSINESS,
    CAN_VIEW_ANALYTICS,
    INVITABLE_ROLES,
)

INVITATION_TTL_DAYS = 7
_PHONE_RE = re.compile(r'^[6-9]\d{9}$')

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_page(page):
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    return max(1, min(page, 100000))


def _clamp_limit(limit):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))


def ensure_organization_for_user(user_id, name=None):
    """Idempotent: returns the organization owned by user_id, creating one
    (+ an active owner membership row) in a single transaction only if none
    exists yet. Lookup-first -- a retry never creates a duplicate
    organization or a duplicate active owner; the DB partial unique index
    (uq_org_members_single_active_owner) is a backstop against bugs/races,
    not the primary idempotency mechanism.
    """
    uid = _as_int(user_id)
    if uid is None:
        return None
    conn = get_db_connection()
    try:
        existing = conn.execute(text("""
            SELECT id, name, owner_user_id, status
            FROM organizations
            WHERE owner_user_id = :uid
            ORDER BY id
            LIMIT 1
        """), {"uid": uid}).fetchone()
        if existing:
            return dict(existing._mapping)

        org_name = (name or "").strip() or f"Organization {uid}"
        created = conn.execute(text("""
            INSERT INTO organizations (name, owner_user_id, status)
            VALUES (:name, :uid, 'active')
            RETURNING id, name, owner_user_id, status
        """), {"name": org_name, "uid": uid}).fetchone()
        org = dict(created._mapping)
        conn.execute(text("""
            INSERT INTO organization_members (organization_id, user_id, role, status)
            VALUES (:org_id, :uid, 'owner', 'active')
        """), {"org_id": org["id"], "uid": uid})
        conn.commit()
        return org
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_organizations_for_user(user_id, page=1, limit=DEFAULT_PAGE_SIZE):
    """Every organization where user_id has an ACTIVE membership row (any
    role) -- a user may belong to multiple organizations (Section 19)."""
    uid = _as_int(user_id)
    if uid is None:
        return {"organizations": [], "page": 1, "limit": DEFAULT_PAGE_SIZE, "total": 0, "pages": 1}
    page = _clamp_page(page)
    limit = _clamp_limit(limit)
    offset = (page - 1) * limit
    conn = get_db_connection()
    try:
        total = conn.execute(text("""
            SELECT COUNT(*) FROM organization_members
            WHERE user_id = :uid AND status = 'active'
        """), {"uid": uid}).scalar() or 0

        rows = conn.execute(text("""
            SELECT o.id, o.name, o.status, om.role
            FROM organization_members om
            JOIN organizations o ON o.id = om.organization_id
            WHERE om.user_id = :uid AND om.status = 'active'
            ORDER BY o.id
            LIMIT :limit OFFSET :offset
        """), {"uid": uid, "limit": limit, "offset": offset}).fetchall()

        orgs = [dict(r._mapping) for r in rows]
        pages = (total + limit - 1) // limit if total else 1
        return {"organizations": orgs, "page": page, "limit": limit, "total": total, "pages": pages}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _clean_phone(raw_phone):
    digits = "".join(ch for ch in str(raw_phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def invite_member(inviter_user_id, organization_id, invited_phone, role, ip_address=None):
    """Owner/manager only. Never invites 'owner' (DB CHECK also enforces
    this). Returns the raw token ONCE -- only its SHA-256 hash is stored."""
    if role not in INVITABLE_ROLES:
        return {"error": "Invalid role", "_http": 400}
    phone = _clean_phone(invited_phone)
    if not _PHONE_RE.match(phone):
        return {"error": "Enter a valid 10-digit mobile number starting with 6-9.", "_http": 400}

    conn = get_db_connection()
    try:
        actor_role = authorize_organization(conn, inviter_user_id, organization_id, CAN_INVITE_MEMBERS)
        if actor_role is None:
            return {"error": "Not found or unauthorized", "_http": 404}
        org_id = int(organization_id)

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = datetime.utcnow() + timedelta(days=INVITATION_TTL_DAYS)

        try:
            row = conn.execute(text("""
                INSERT INTO organization_invitations
                    (organization_id, invited_phone, invited_by, role, token_hash, status, expires_at)
                VALUES
                    (:org_id, :phone, :invited_by, :role, :token_hash, 'pending', :expires_at)
                RETURNING id, expires_at
            """), {
                "org_id": org_id, "phone": phone, "invited_by": int(inviter_user_id),
                "role": role, "token_hash": token_hash, "expires_at": expires_at,
            }).fetchone()
            conn.commit()
        except IntegrityError:
            conn.rollback()
            return {"error": "An invitation is already pending for this phone number", "_http": 409}

        log_admin_action(
            int(inviter_user_id), None, "organization_member_invited", "organization", str(org_id),
            f"invited phone={phone} role={role}", ip_address,
        )
        return {
            "invitation_id": row._mapping["id"],
            "organization_id": org_id,
            "role": role,
            "token": token,
            "expires_at": row._mapping["expires_at"],
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def accept_invitation(user_id, organization_id, token):
    """Token hash + pending status + not-expired + the AUTHENTICATED user's
    own DB phone (never a client-supplied phone) must all match. Per the
    locked policy: acceptance succeeds even if the organization owner's
    Business Power subscription has lapsed -- it only records membership,
    it never grants operational access (that is gated separately, on every
    subsequent organization action, by authorize_organization[_listing])."""
    uid = _as_int(user_id)
    org_id = _as_int(organization_id)
    if uid is None or org_id is None or not token:
        return {"error": "Invalid or expired invitation", "_http": 400}
    token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    conn = get_db_connection()
    try:
        user_row = conn.execute(
            text("SELECT phone FROM users WHERE id = :uid"), {"uid": uid}
        ).fetchone()
        if not user_row:
            return {"error": "Invalid or expired invitation", "_http": 400}
        user_phone = user_row._mapping["phone"]

        invitation = conn.execute(text("""
            SELECT id, invited_phone, role
            FROM organization_invitations
            WHERE token_hash = :hash AND organization_id = :org_id
              AND status = 'pending' AND expires_at > CURRENT_TIMESTAMP
        """), {"hash": token_hash, "org_id": org_id}).fetchone()
        if not invitation:
            return {"error": "Invalid or expired invitation", "_http": 400}
        inv = dict(invitation._mapping)
        if inv["invited_phone"] != user_phone:
            return {"error": "Invalid or expired invitation", "_http": 400}

        result = conn.execute(text("""
            UPDATE organization_invitations
            SET status = 'accepted', accepted_at = CURRENT_TIMESTAMP
            WHERE id = :id AND status = 'pending'
        """), {"id": inv["id"]})
        if getattr(result, "rowcount", 0) != 1:
            conn.rollback()
            return {"error": "Invalid or expired invitation", "_http": 400}

        try:
            conn.execute(text("""
                INSERT INTO organization_members (organization_id, user_id, role, status)
                VALUES (:org_id, :uid, :role, 'active')
            """), {"org_id": org_id, "uid": uid, "role": inv["role"]})
            conn.commit()
        except IntegrityError:
            conn.rollback()
            return {"error": "You are already an active member of this organization", "_http": 409}

        log_admin_action(
            uid, user_phone, "organization_invitation_accepted", "organization", str(org_id),
            f"role={inv['role']}", None,
        )
        return {"status": "accepted", "organization_id": org_id, "role": inv["role"]}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_members(user_id, organization_id, page=1, limit=DEFAULT_PAGE_SIZE):
    conn = get_db_connection()
    try:
        role = authorize_organization(conn, user_id, organization_id, CAN_VIEW_ORGANIZATION)
        if role is None:
            return None
        org_id = int(organization_id)
        page = _clamp_page(page)
        limit = _clamp_limit(limit)
        offset = (page - 1) * limit

        total = conn.execute(text("""
            SELECT COUNT(*) FROM organization_members
            WHERE organization_id = :org_id AND status = 'active'
        """), {"org_id": org_id}).scalar() or 0

        rows = conn.execute(text("""
            SELECT om.id, om.user_id, om.role, om.status, u.phone, u.name
            FROM organization_members om
            JOIN users u ON u.id = om.user_id
            WHERE om.organization_id = :org_id AND om.status = 'active'
            ORDER BY om.id
            LIMIT :limit OFFSET :offset
        """), {"org_id": org_id, "limit": limit, "offset": offset}).fetchall()

        members = [dict(r._mapping) for r in rows]
        pages = (total + limit - 1) // limit if total else 1
        return {"members": members, "page": page, "limit": limit, "total": total, "pages": pages}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def change_member_role(actor_user_id, organization_id, member_id, new_role, ip_address=None):
    """Owner only. Never targets a role='owner' row (no ownership transfer
    in V2) and never sets new_role='owner' (DB CHECK also enforces this)."""
    if new_role not in INVITABLE_ROLES:
        return {"error": "Invalid role", "_http": 400}
    conn = get_db_connection()
    try:
        actor_role = authorize_organization(conn, actor_user_id, organization_id, CAN_CHANGE_ROLE)
        if actor_role is None:
            return {"error": "Not found or unauthorized", "_http": 404}
        org_id = int(organization_id)
        mid = int(member_id)

        result = conn.execute(text("""
            UPDATE organization_members
            SET role = :new_role, updated_at = CURRENT_TIMESTAMP
            WHERE id = :mid AND organization_id = :org_id
              AND status = 'active' AND role <> 'owner'
        """), {"new_role": new_role, "mid": mid, "org_id": org_id})
        if getattr(result, "rowcount", 0) != 1:
            conn.rollback()
            return {"error": "Member not found or role cannot be changed", "_http": 404}
        conn.commit()
        log_admin_action(
            int(actor_user_id), None, "organization_member_role_changed", "organization_member", str(mid),
            f"organization_id={org_id} new_role={new_role}", ip_address,
        )
        return {"status": "role_updated"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def remove_member(actor_user_id, organization_id, member_id, ip_address=None):
    """Owner only. Never removes a role='owner' row."""
    conn = get_db_connection()
    try:
        actor_role = authorize_organization(conn, actor_user_id, organization_id, CAN_REMOVE_MEMBER)
        if actor_role is None:
            return {"error": "Not found or unauthorized", "_http": 404}
        org_id = int(organization_id)
        mid = int(member_id)

        result = conn.execute(text("""
            UPDATE organization_members
            SET status = 'removed', updated_at = CURRENT_TIMESTAMP
            WHERE id = :mid AND organization_id = :org_id
              AND status = 'active' AND role <> 'owner'
        """), {"mid": mid, "org_id": org_id})
        if getattr(result, "rowcount", 0) != 1:
            conn.rollback()
            return {"error": "Member not found or cannot be removed", "_http": 404}
        conn.commit()
        log_admin_action(
            int(actor_user_id), None, "organization_member_removed", "organization_member", str(mid),
            f"organization_id={org_id}", ip_address,
        )
        return {"status": "member_removed"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_organization_detail(user_id, organization_id):
    """Owner/manager/staff (any active member) may view organization detail
    plus member/business counts. Returns None on any authorization failure
    -- callers must respond with a generic 403/404, never distinguishing
    the reason."""
    conn = get_db_connection()
    try:
        role = authorize_organization(conn, user_id, organization_id, CAN_VIEW_ORGANIZATION)
        if role is None:
            return None
        org_id = int(organization_id)
        org_row = conn.execute(text("""
            SELECT id, name, owner_user_id, status, created_at
            FROM organizations WHERE id = :org_id
        """), {"org_id": org_id}).fetchone()
        if not org_row:
            return None
        member_count = conn.execute(text("""
            SELECT COUNT(*) FROM organization_members
            WHERE organization_id = :org_id AND status = 'active'
        """), {"org_id": org_id}).scalar() or 0
        business_count = conn.execute(text("""
            SELECT COUNT(*) FROM listings WHERE organization_id = :org_id
        """), {"org_id": org_id}).scalar() or 0
        detail = dict(org_row._mapping)
        detail["your_role"] = role
        detail["member_count"] = member_count
        detail["business_count"] = business_count
        return detail
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parse_coord(value, lo, hi):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not (lo <= num <= hi):
        return None
    return num


def create_organization_business(actor_user_id, organization_id, data, user_phone=None):
    """Owner/manager only. listings.user_id is set to the CREATING member
    (never reassigned afterward); organization_id is the organization
    relationship. No business_limit check -- organization capacity comes
    from the owner's unlimited Business Power plan, already verified by
    authorize_organization via CAN_CREATE_BUSINESS."""
    conn = get_db_connection()
    try:
        role = authorize_organization(conn, actor_user_id, organization_id, CAN_CREATE_BUSINESS)
        if role is None:
            return {"error": "Not found or unauthorized", "_http": 404}
        org_id = int(organization_id)

        business_name = (data.get("business_name") or "").strip()
        category = (data.get("category") or "").strip()
        if not business_name or not category:
            return {"error": "Business name and category required", "_http": 400}
        latitude = _parse_coord(data.get("latitude"), -90, 90)
        longitude = _parse_coord(data.get("longitude"), -180, 180)
        if latitude is None or longitude is None:
            return {"error": "Invalid latitude/longitude", "_http": 400}
        listing_type = (data.get("listing_type") or "business").strip().lower()
        if listing_type not in ("business", "service"):
            listing_type = "business"

        result = conn.execute(text("""
            INSERT INTO listings (
                user_id, user_phone, organization_id, listing_type, business_name, category,
                city, state, latitude, longitude,
                description, whatsapp, website, status, is_active,
                is_premium, is_featured, is_sponsored, is_verified
            ) VALUES (
                :user_id, :user_phone, :org_id, :listing_type, :business_name, :category,
                :city, :state, :latitude, :longitude,
                :description, :whatsapp, :website, 'pending', 1,
                0, 0, 0, 0
            )
            RETURNING id
        """), {
            "user_id": int(actor_user_id),
            "user_phone": user_phone,
            "org_id": org_id,
            "listing_type": listing_type,
            "business_name": business_name,
            "category": category,
            "city": data.get("city", ""),
            "state": data.get("state", ""),
            "latitude": latitude,
            "longitude": longitude,
            "description": data.get("description", ""),
            "whatsapp": data.get("whatsapp", ""),
            "website": data.get("website", ""),
        })
        listing_id = result.fetchone()[0]
        conn.commit()
        log_admin_action(
            int(actor_user_id), user_phone, "organization_business_created", "listing", str(listing_id),
            f"organization_id={org_id}", None,
        )
        return {"success": True, "listing_id": listing_id, "message": "Business submitted for review"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_organization_businesses(user_id, organization_id, page=1, limit=DEFAULT_PAGE_SIZE):
    """Any active member (view permission) — paginated, bounded page size."""
    conn = get_db_connection()
    try:
        role = authorize_organization(conn, user_id, organization_id, CAN_VIEW_ORGANIZATION)
        if role is None:
            return None
        org_id = int(organization_id)
        page = _clamp_page(page)
        limit = _clamp_limit(limit)
        offset = (page - 1) * limit

        total = conn.execute(text("""
            SELECT COUNT(*) FROM listings WHERE organization_id = :org_id
        """), {"org_id": org_id}).scalar() or 0

        rows = conn.execute(text("""
            SELECT l.*,
                (SELECT image_url FROM listing_images WHERE listing_id = l.id LIMIT 1) as image_url
            FROM listings l
            WHERE l.organization_id = :org_id
            ORDER BY l.id DESC
            LIMIT :limit OFFSET :offset
        """), {"org_id": org_id, "limit": limit, "offset": offset}).fetchall()

        businesses = [dict(r._mapping) for r in rows]
        pages = (total + limit - 1) // limit if total else 1
        return {"businesses": businesses, "page": page, "limit": limit, "total": total, "pages": pages}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_organization_business(actor_user_id, organization_id, listing_id, data, ip_address=None):
    """Owner/manager only. Never touches listings.user_id."""
    conn = get_db_connection()
    try:
        auth = authorize_organization_listing(conn, actor_user_id, listing_id, CAN_EDIT_BUSINESS)
        if auth is None or int(auth["organization_id"]) != int(organization_id):
            return {"error": "Not found or unauthorized", "_http": 404}
        lid = int(listing_id)
        conn.execute(text("""
            UPDATE listings
            SET business_name = :bname, category = :cat, city = :city, state = :state, description = :desc
            WHERE id = :lid AND organization_id = :org_id
        """), {
            "bname": data.get("business_name"),
            "cat": data.get("category"),
            "city": data.get("city"),
            "state": data.get("state"),
            "desc": data.get("description"),
            "lid": lid,
            "org_id": int(organization_id),
        })
        conn.commit()
        log_admin_action(
            int(actor_user_id), None, "organization_business_updated", "listing", str(lid),
            f"organization_id={organization_id}", ip_address,
        )
        return {"message": "Business updated"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def delete_organization_business(actor_user_id, organization_id, listing_id, ip_address=None):
    """Owner only. Never touches listings.user_id on any other row."""
    conn = get_db_connection()
    try:
        auth = authorize_organization_listing(conn, actor_user_id, listing_id, CAN_DELETE_BUSINESS)
        if auth is None or int(auth["organization_id"]) != int(organization_id):
            return {"error": "Not found or unauthorized", "_http": 404}
        lid = int(listing_id)
        conn.execute(text("DELETE FROM listing_images WHERE listing_id = :lid"), {"lid": lid})
        result = conn.execute(text("""
            DELETE FROM listings WHERE id = :lid AND organization_id = :org_id
        """), {"lid": lid, "org_id": int(organization_id)})
        if getattr(result, "rowcount", 0) != 1:
            conn.rollback()
            return {"error": "Not found or unauthorized", "_http": 404}
        conn.commit()
        log_admin_action(
            int(actor_user_id), None, "organization_business_deleted", "listing", str(lid),
            f"organization_id={organization_id}", ip_address,
        )
        return {"message": "Business deleted"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_organization_analytics(user_id, organization_id):
    """Owner/manager only (staff has no analytics access). Aggregates only
    listings belonging to THIS organization -- every query below is scoped
    by organization_id, established via authorize_organization first, so a
    member of a different organization can never reach this data."""
    conn = get_db_connection()
    try:
        role = authorize_organization(conn, user_id, organization_id, CAN_VIEW_ANALYTICS)
        if role is None:
            return None
        org_id = int(organization_id)

        totals = conn.execute(text("""
            SELECT
                COALESCE(SUM(views), 0) as total_views,
                COALESCE(SUM(clicks), 0) as total_clicks,
                COALESCE(SUM(whatsapp_clicks), 0) as total_whatsapp
            FROM listings
            WHERE organization_id = :org_id
        """), {"org_id": org_id}).fetchone()

        daily_stats = []
        for i in range(6, -1, -1):
            date_str = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
            row = conn.execute(text("""
                SELECT
                    COALESCE(SUM(views), 0) as views,
                    COALESCE(SUM(clicks), 0) as clicks
                FROM listings
                WHERE organization_id = :org_id AND DATE(created_at) = :date
            """), {"org_id": org_id, "date": date_str}).fetchone()
            daily_stats.append({
                'date': date_str,
                'views': row._mapping['views'],
                'clicks': row._mapping['clicks'],
            })

        top_listings = conn.execute(text("""
            SELECT id, business_name, views, clicks, whatsapp_clicks
            FROM listings
            WHERE organization_id = :org_id
            ORDER BY views DESC
            LIMIT 5
        """), {"org_id": org_id}).fetchall()

        return {
            'totals': {
                'views': totals._mapping['total_views'],
                'clicks': totals._mapping['total_clicks'],
                'whatsapp': totals._mapping['total_whatsapp'],
                'calls': 0,
            },
            'daily': daily_stats,
            'top_listings': [dict(r._mapping) for r in top_listings],
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass
