"""Business Power V2 additive schema: organizations, membership, invitations.

Does NOT drop/rewrite/reassign anything. listings.user_id keeps its exact
existing meaning; the only listings change is a new nullable
organization_id column. Existing rows all get organization_id = NULL and
are completely unaffected by this migration.
"""
from sqlalchemy import text
from database.init_db import get_db_connection
import logging

logger = logging.getLogger(__name__)


def migrate_business_power_v2_organizations():
    conn = get_db_connection()
    try:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS organizations (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_organizations_owner ON organizations(owner_user_id)"
        ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS organization_members (
                id SERIAL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES organizations(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                role TEXT NOT NULL
                    CHECK (role IN ('owner','manager','staff')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('invited','active','suspended','removed')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_org_members_org ON organization_members(organization_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_org_members_user ON organization_members(user_id)"
        ))
        # Invariant: one person has at most one ACTIVE membership row per org.
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_org_members_active_person
            ON organization_members (organization_id, user_id)
            WHERE status = 'active'
        """))
        # Invariant (DB-enforced single owner): at most one ACTIVE owner-role
        # row per org, regardless of which user holds it. This is a
        # DIFFERENT constraint from the one above and both are required.
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_org_members_single_active_owner
            ON organization_members (organization_id)
            WHERE status = 'active' AND role = 'owner'
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS organization_invitations (
                id SERIAL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES organizations(id),
                invited_phone TEXT NOT NULL,
                invited_by INTEGER NOT NULL REFERENCES users(id),
                role TEXT NOT NULL
                    CHECK (role IN ('manager','staff')),
                token_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','accepted','cancelled','expired')),
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                accepted_at TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_org_invitations_org ON organization_invitations(organization_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_org_invitations_phone ON organization_invitations(invited_phone)"
        ))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_org_invitations_token_hash
            ON organization_invitations(token_hash)
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_org_invitations_pending
            ON organization_invitations (organization_id, invited_phone)
            WHERE status = 'pending'
        """))

        # Additive-only listings change: nullable FK, existing rows unaffected.
        conn.execute(text(
            "ALTER TABLE listings ADD COLUMN IF NOT EXISTS organization_id INTEGER REFERENCES organizations(id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_listings_organization ON listings(organization_id)"
        ))

        conn.commit()
        logger.info("Business Power V2 organization schema applied")
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


if __name__ == "__main__":
    migrate_business_power_v2_organizations()
