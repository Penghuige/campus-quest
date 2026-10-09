"""Operator conflict declarations and distinct innovation decision events."""

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_OLD_EVENTS = (
    "ASSIGNMENT_DEADLINE_24H",
    "ASSIGNMENT_DEADLINE_4H",
    "REVISION_REQUIRED",
    "SUBMISSION_APPROVED",
    "SUBMISSION_VALIDATION_FAILED",
    "REWARD_REDEMPTION_APPROVED",
    "REWARD_REDEMPTION_REJECTED",
    "ACCOUNT_SECURITY",
)
_IE_EVENTS = ("IE_ACHIEVEMENT_APPROVED", "IE_ACHIEVEMENT_RETURNED")


def _events(values: tuple[str, ...]) -> None:
    check = "event_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"
    for table in ("notifications", "notification_templates"):
        op.drop_constraint(op.f(f"ck_{table}_event_type"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_event_type"), table, check)


def upgrade() -> None:
    op.create_table(
        "ie_review_conflicts",
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("ie_project_drafts.id"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _events(_OLD_EVENTS + _IE_EVENTS)


def downgrade() -> None:
    # Existing innovation notifications deliberately prevent a destructive
    # downgrade; no migration silently erases durable business history.
    _events(_OLD_EVENTS)
    op.drop_table("ie_review_conflicts")
