"""Add the IN_APP recruitment confirmation event to frozen checks."""

from alembic import op

revision = "0045_recruitment_notifications"
down_revision = "0044_recruitments"
branch_labels = None
depends_on = None

_OLD = (
    "ASSIGNMENT_DEADLINE_24H",
    "ASSIGNMENT_DEADLINE_4H",
    "REVISION_REQUIRED",
    "SUBMISSION_APPROVED",
    "SUBMISSION_VALIDATION_FAILED",
    "REWARD_REDEMPTION_APPROVED",
    "REWARD_REDEMPTION_REJECTED",
    "ACCOUNT_SECURITY",
    "IE_ACHIEVEMENT_APPROVED",
    "IE_ACHIEVEMENT_RETURNED",
)


def _events(values: tuple[str, ...]) -> None:
    check = "event_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"
    for table in ("notifications", "notification_templates"):
        op.drop_constraint(op.f(f"ck_{table}_event_type"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_event_type"), table, check)


def upgrade() -> None:
    _events(_OLD + ("IE_RECRUITMENT_CONFIRMATION_DUE",))


def downgrade() -> None:
    # Preserve history: populated recruitment events prevent unsafe downgrade.
    _events(_OLD)
