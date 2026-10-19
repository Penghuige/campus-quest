"""Wave 2a §3.4 migration: the last-active-admin DB backstop trigger.

The service counts under the target's row lock (friendly 409 on the
sequential path); this trigger re-adjudicates every UPDATE — the
count itself takes FOR UPDATE on every ACTIVE ADMIN row, so two racing
demotions serialize on those locks and the second one re-counts after
the first commits: exactly one lands.
"""

revision = "0031_last_admin_trigger"
down_revision = "0030"
branch_labels = None
depends_on = None

_TRIGGER = """
CREATE OR REPLACE FUNCTION guard_last_active_admin() RETURNS trigger AS $$
DECLARE remaining int;
BEGIN
    IF OLD.role = 'ADMIN' AND NEW.role <> 'ADMIN' THEN
        -- Lock every OTHER active-admin row first (aggregates cannot
        -- take FOR UPDATE), so two racing demotions serialize here and
        -- the count below re-evaluates after the first commits.
        PERFORM 1 FROM users
        WHERE role = 'ADMIN' AND status = 'ACTIVE' AND id <> OLD.id
        FOR UPDATE;
        SELECT count(*) INTO remaining
        FROM users
        WHERE role = 'ADMIN' AND status = 'ACTIVE' AND id <> OLD.id;
        IF remaining = 0 THEN
            RAISE EXCEPTION 'the last ACTIVE ADMIN cannot be demoted'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_DROP = """
DROP TRIGGER IF EXISTS trg_users_guard_last_admin ON users;
DROP FUNCTION IF EXISTS guard_last_active_admin();
"""


def upgrade() -> None:
    from alembic import op

    # asyncpg prepared statements take ONE command each — split.
    for statement in _DROP.strip().split(";"):
        if statement.strip():
            op.execute(statement)
    op.execute(_TRIGGER)
    op.execute(
        "CREATE TRIGGER trg_users_guard_last_admin BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION guard_last_active_admin();"
    )


def downgrade() -> None:
    from alembic import op

    for statement in _DROP.strip().split(";"):
        if statement.strip():
            op.execute(statement)
