"""Business Power V2 — centralized organization authorization.

Single authorization gateway for every V2 organization route. Never trust
JWT role/organization claims; every check here is a fresh DB read joined
through organization_members -> organizations -> users(owner), gated by
the same canonical Business Power subscription check every other paid
plan uses (services.subscription_access.is_active_business_power_owner).

Deny-by-default: a missing/suspended/removed membership row, a suspended
organization, a disallowed role, or a lapsed owner subscription all
collapse to the same "no row returned" outcome. Callers must treat None
as a generic denial and must never distinguish the reason to the client.

Role-set SQL uses explicit named parameters (IN (:role_0, :role_1, ...)),
not array binding (`= ANY(:list)`) or string interpolation of role
values -- this repository has exactly one existing multi-value-IN
precedent (services/referral_commission.py: "source IN (:src_first,
:src_recurring)") and it uses named parameters, not array adaptation.
Allowed-role sets are fixed, code-controlled tuples below -- never derived
from request data.
"""
from sqlalchemy import text

from services.subscription_access import is_active_business_power_owner

# ---------------------------------------------------------------------------
# Centralized, code-controlled permission matrix (Section 10/E of the design).
# ---------------------------------------------------------------------------
CAN_VIEW_BUSINESSES = ("owner", "manager", "staff")
CAN_CREATE_BUSINESS = ("owner", "manager")
CAN_EDIT_BUSINESS = ("owner", "manager")
CAN_DELETE_BUSINESS = ("owner",)
CAN_VIEW_ANALYTICS = ("owner", "manager")
CAN_INVITE_MEMBERS = ("owner", "manager")
CAN_REMOVE_MEMBER = ("owner",)
CAN_CHANGE_ROLE = ("owner",)
CAN_CHANGE_SETTINGS = ("owner",)
CAN_VIEW_ORGANIZATION = ("owner", "manager", "staff")

ALL_ROLES = ("owner", "manager", "staff")
INVITABLE_ROLES = ("manager", "staff")


def _role_in_clause(allowed_roles, prefix="role"):
    """Build ('(:role_0, :role_1, ...)', {params}) for a SQL IN clause using
    only named parameters -- never array binding, never string
    interpolation of the role values themselves."""
    roles = tuple(allowed_roles)
    keys = [f"{prefix}_{i}" for i in range(len(roles))]
    clause = "(" + ", ".join(f":{k}" for k in keys) + ")"
    return clause, dict(zip(keys, roles))


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def authorize_organization(conn, user_id, organization_id, allowed_roles):
    """Fresh-DB authorization for an organization-level action.

    Establishes, in ONE query: active membership + active organization +
    role in allowed_roles. The Business Power subscription check is then
    applied via the one canonical function (never re-implemented in SQL).

    Returns the member's role string on success, or None (deny).
    """
    uid = _as_int(user_id)
    org_id = _as_int(organization_id)
    if uid is None or org_id is None:
        return None
    clause, params = _role_in_clause(allowed_roles)
    row = conn.execute(text(f"""
        SELECT om.role, owner.plan AS owner_plan,
               owner.subscription_expiry AS owner_subscription_expiry
        FROM organization_members om
        JOIN organizations o ON o.id = om.organization_id
        JOIN users owner ON owner.id = o.owner_user_id
        WHERE om.organization_id = :org_id
          AND om.user_id = :user_id
          AND om.status = 'active'
          AND o.status = 'active'
          AND om.role IN {clause}
    """), {"org_id": org_id, "user_id": uid, **params}).fetchone()
    if not row:
        return None
    m = row._mapping
    if not is_active_business_power_owner({
        "plan": m["owner_plan"], "subscription_expiry": m["owner_subscription_expiry"],
    }):
        return None
    return m["role"]


def authorize_organization_listing(conn, user_id, listing_id, allowed_roles):
    """Fresh-DB authorization for a specific organization-owned listing.

    Establishes, in ONE query: listing -> organization -> active membership
    -> active organization -> role in allowed_roles. A member never gains
    access merely by knowing an organization ID or listing ID -- both must
    resolve together through this single join.

    Returns {"role": ..., "organization_id": ...} on success (the listing's
    OWN, actual organization_id -- resolved from the join, never trusted
    from a caller-supplied value), or None (deny).
    """
    uid = _as_int(user_id)
    lid = _as_int(listing_id)
    if uid is None or lid is None:
        return None
    clause, params = _role_in_clause(allowed_roles)
    row = conn.execute(text(f"""
        SELECT l.id, l.organization_id, om.role,
               owner.plan AS owner_plan, owner.subscription_expiry AS owner_subscription_expiry
        FROM listings l
        JOIN organization_members om ON om.organization_id = l.organization_id
        JOIN organizations o ON o.id = om.organization_id
        JOIN users owner ON owner.id = o.owner_user_id
        WHERE l.id = :listing_id
          AND om.user_id = :user_id
          AND om.status = 'active'
          AND o.status = 'active'
          AND om.role IN {clause}
    """), {"listing_id": lid, "user_id": uid, **params}).fetchone()
    if not row:
        return None
    m = row._mapping
    if not is_active_business_power_owner({
        "plan": m["owner_plan"], "subscription_expiry": m["owner_subscription_expiry"],
    }):
        return None
    return {"role": m["role"], "organization_id": m["organization_id"]}


def get_membership_status(conn, user_id, organization_id):
    """Raw membership status lookup (no Business Power/role gating) — used
    only where the caller needs to distinguish invited/active/suspended/
    removed for its own display purposes (e.g. admin inspection). Never use
    this for an authorization decision; use authorize_organization(_listing)."""
    uid = _as_int(user_id)
    org_id = _as_int(organization_id)
    if uid is None or org_id is None:
        return None
    row = conn.execute(text("""
        SELECT role, status
        FROM organization_members
        WHERE organization_id = :org_id AND user_id = :user_id
        ORDER BY id DESC
        LIMIT 1
    """), {"org_id": org_id, "user_id": uid}).fetchone()
    if not row:
        return None
    return dict(row._mapping)
