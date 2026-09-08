# services/pos_billing_activation.py
"""Phase G-C: applies a legitimately paid pos_billing_orders row to
pos_subscriptions, exactly once. This is the ONLY code path in this
codebase that writes to pos_subscriptions from the billing flow -- G-A
(order creation) and G-B (payment verification/webhook) never touch it,
and nothing here re-verifies a Razorpay signature or trusts anything
except pos_billing_orders.status == 'paid' (already established,
independently, by G-B).

Kept separate from routes/pos_routes.py (rather than another private
`_helper` in that file, the pattern G-A/G-B used) because the task
explicitly asks for reusable activation logic/service that isn't
duplicated if a later phase wires webhook-to-activation -- this module has
no Flask/HTTP concerns at all, only a SQLAlchemy `conn` it's handed and a
plain result dict, so it can be called from a route, a future webhook
path, or a test without going through HTTP.
"""
from datetime import datetime

from sqlalchemy import text

from services.subscription_access import is_pos_subscription_active


def add_one_calendar_month(dt):
    """Deterministic calendar-month arithmetic, no external dependency
    (python-dateutil is present transitively in this environment but is
    not a declared project dependency, so it is deliberately not used
    here). September 8 -> October 8. Clamps to the last valid day of the
    target month for month-end dates (e.g. January 31 -> February 28, or
    29 in a leap year) rather than overflowing into the following month.
    """
    month = dt.month + 1
    year = dt.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = dt.day
    while True:
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1


def compute_activation_target(current_row, paid_plan, now):
    """Pure policy function -- the one place the G-C lifecycle rules
    (Policies 2-7 in the Phase G-C task) are decided. Never touches the
    database itself, so it's directly unit-testable without a fake conn.

    `current_row` is the owner's existing pos_subscriptions row as a dict
    (`pos_plan`, `status`, `expires_at`) or None if they have none yet.
    `paid_plan` is the plan the JUST-PAID billing order is for -- never a
    client-supplied value; the caller reads it from pos_billing_orders.

    Returns (action, target):
      action="apply"  -- target is {"pos_plan", "status", "expires_at"};
                          the caller should write this to pos_subscriptions.
      action="skip"   -- target is None; the caller must NOT modify
                          pos_subscriptions (Case E: an active Growth
                          subscriber's Starter order is paid -- downgrade
                          protection, Policy 6). The billing order is
                          still marked activated by the caller so it can
                          never be reprocessed.

    Reuses the canonical is_pos_subscription_active() (never re-derives
    "is this row currently active" from status/expires_at itself) --
    matches services/subscription_access.py being the sole authority for
    that determination, including for suspended rows and calendar-based
    expiry, per the Phase G-C instruction not to duplicate entitlement
    logic here.
    """
    if current_row is None:
        # Case A -- no existing subscription at all.
        return "apply", {
            "pos_plan": paid_plan,
            "status": "active",
            "expires_at": add_one_calendar_month(now),
        }

    if not is_pos_subscription_active(current_row):
        # Case B (expired) and Case F (suspended) -- both restart fresh
        # from the new activation time, regardless of the prior plan.
        # Neither an expired nor a suspended row has any "remaining time"
        # policy requires preserving (only an *active* renewal does).
        return "apply", {
            "pos_plan": paid_plan,
            "status": "active",
            "expires_at": add_one_calendar_month(now),
        }

    current_plan = current_row.get("pos_plan")

    if current_plan == paid_plan:
        # Case C -- renewal. Extend from the EXISTING expiry, never from
        # "now" (Policy 3: a customer must never lose already-paid time).
        # A currently-active row's expires_at is virtually always a
        # concrete timestamp (every G-C write sets one); a NULL here can
        # only come from a row created outside this module. Treated as
        # "extend from now" -- see DECISIONS note in the Phase G-C report
        # for why this specific fallback was chosen instead of leaving it
        # NULL forever.
        base = current_row.get("expires_at") or now
        return "apply", {
            "pos_plan": current_plan,
            "status": "active",
            "expires_at": add_one_calendar_month(base),
        }

    if current_plan == "starter" and paid_plan == "growth":
        # Case D -- upgrade. Starts fresh from now; no proration (Policy 5).
        return "apply", {
            "pos_plan": "growth",
            "status": "active",
            "expires_at": add_one_calendar_month(now),
        }

    if current_plan == "growth" and paid_plan == "starter":
        # Case E -- downgrade protection (Policy 6). Growth is left
        # completely unchanged; the caller still marks the billing order
        # activated so this paid Starter order can never be reprocessed.
        return "skip", None

    # Unreachable given POS_PLANS only ever contains "starter"/"growth"
    # (enforced by pos_billing_orders' own CHECK constraint before a row
    # can exist), but fails toward the safe, well-defined Case A/B
    # behavior rather than silently doing nothing if that ever changes.
    return "apply", {
        "pos_plan": paid_plan,
        "status": "active",
        "expires_at": add_one_calendar_month(now),
    }


def _load_billing_order_for_update(conn, billing_order_id, owner_user_id):
    row = conn.execute(
        text("""
            SELECT id, owner_user_id, pos_plan, amount_paise, currency,
                   status, activated_at
            FROM pos_billing_orders
            WHERE id = :id AND owner_user_id = :uid
            FOR UPDATE
        """),
        {"id": billing_order_id, "uid": owner_user_id},
    ).fetchone()
    return dict(row._mapping) if row else None


def _load_pos_subscription(conn, owner_user_id):
    row = conn.execute(
        text("""
            SELECT owner_user_id, pos_plan, status, expires_at
            FROM pos_subscriptions
            WHERE owner_user_id = :uid
        """),
        {"uid": owner_user_id},
    ).fetchone()
    return dict(row._mapping) if row else None


def activate_paid_billing_order(conn, billing_order_id, owner_user_id):
    """The single write path from a paid pos_billing_orders row to
    pos_subscriptions. Owns its own transaction on `conn` (commits or
    leaves it to the caller to roll back on exception) -- the caller is
    expected to `conn.close()` afterward, matching every other pos_routes
    helper's contract.

    Concurrency/idempotency, in order:
      1. `SELECT ... FOR UPDATE` locks the billing-order row first. A
         second call for the SAME billing_order_id (retry, duplicate
         client request, concurrent tab) blocks here until the first
         call's transaction commits, then observes activated_at already
         set and takes the "already_activated" branch below -- the
         subscription period is never applied twice for one paid order.
      2. For a brand-new owner (Case A), `INSERT ... ON CONFLICT
         (owner_user_id) DO NOTHING` is the actual race arbiter: if two
         DIFFERENT billing orders for the same first-time owner are
         activated concurrently, the owner_user_id UNIQUE constraint lets
         only one INSERT win; the other falls through to the locked
         read-then-UPDATE path below and correctly re-derives its
         action (renewal/upgrade/downgrade-protection) against the
         winner's just-committed row, rather than against a stale
         "no row existed" assumption.
      3. For an existing owner, `SELECT ... FOR UPDATE` on
         pos_subscriptions locks that single row (owner_user_id is
         UNIQUE) before compute_activation_target() decides the action,
         so two concurrent activations for the same existing owner
         serialize on this row too.
      4. The subscription write and the `activated_at` marker are set in
         the same transaction, committed together -- an exception anywhere
         in this function propagates to the caller with nothing committed,
         so a failure never leaves a half-activated payment (a paid order
         with no activated_at is always still safely retryable; one with
         activated_at set is always backed by a subscription write that
         already committed in the same transaction).

    Returns a dict:
      {"outcome": "not_found" | "not_paid" | "already_activated" | "activated",
       "billing_order": {...} | None,
       "subscription": {...} | None,
       "subscription_changed": bool}
    """
    order_row = _load_billing_order_for_update(conn, billing_order_id, owner_user_id)
    if order_row is None:
        conn.commit()  # nothing was locked; just end the transaction cleanly
        return {
            "outcome": "not_found",
            "billing_order": None,
            "subscription": None,
            "subscription_changed": False,
        }

    if order_row["status"] != "paid":
        conn.commit()  # release the row lock; nothing to change
        return {
            "outcome": "not_paid",
            "billing_order": order_row,
            "subscription": None,
            "subscription_changed": False,
        }

    if order_row.get("activated_at") is not None:
        sub_row = _load_pos_subscription(conn, owner_user_id)
        conn.commit()
        return {
            "outcome": "already_activated",
            "billing_order": order_row,
            "subscription": sub_row,
            "subscription_changed": False,
        }

    now = datetime.utcnow()
    paid_plan = order_row["pos_plan"]

    _, case_a_target = compute_activation_target(None, paid_plan, now)
    claimed = conn.execute(
        text("""
            INSERT INTO pos_subscriptions
                (owner_user_id, pos_plan, status, expires_at, created_at, updated_at)
            VALUES (:uid, :plan, :status, :expires_at, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (owner_user_id) DO NOTHING
            RETURNING pos_plan, status, expires_at
        """),
        {
            "uid": owner_user_id,
            "plan": case_a_target["pos_plan"],
            "status": case_a_target["status"],
            "expires_at": case_a_target["expires_at"],
        },
    ).fetchone()

    if claimed is not None:
        subscription_changed = True
    else:
        current_sub = conn.execute(
            text("""
                SELECT pos_plan, status, expires_at FROM pos_subscriptions
                WHERE owner_user_id = :uid
                FOR UPDATE
            """),
            {"uid": owner_user_id},
        ).fetchone()
        current_sub = dict(current_sub._mapping) if current_sub else None

        action, target = compute_activation_target(current_sub, paid_plan, now)
        if action == "apply":
            conn.execute(
                text("""
                    UPDATE pos_subscriptions
                    SET pos_plan = :plan, status = :status, expires_at = :expires_at,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE owner_user_id = :uid
                """),
                {
                    "uid": owner_user_id,
                    "plan": target["pos_plan"],
                    "status": target["status"],
                    "expires_at": target["expires_at"],
                },
            )
            subscription_changed = True
        else:
            subscription_changed = False

    conn.execute(
        text("""
            UPDATE pos_billing_orders
            SET activated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id
        """),
        {"id": billing_order_id},
    )

    sub_row = _load_pos_subscription(conn, owner_user_id)
    conn.commit()

    return {
        "outcome": "activated",
        "billing_order": order_row,
        "subscription": sub_row,
        "subscription_changed": subscription_changed,
    }
