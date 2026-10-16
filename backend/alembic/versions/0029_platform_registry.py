"""The platform registry (V1.1 spec 2026-10-16 §1-§2).

- ``platforms`` table + the seven shipped seeds (bulk-inserted from the
  importable constant ``SEED_PLATFORMS`` so DB and code cannot drift).
- ``assignments.platform``: the closed CHECK set becomes a FK to
  ``platforms.code`` — platform growth is admin rows, zero migrations.
- Production placeholder backfill (§2): the 20 zhihu-placeholder rows
  whose keyword carries a recruitment prefix are moved to the real code
  — ``BOSS直聘%→boss-zhipin；智联招聘%→zhaopin；58同城%→58city；猎聘%
  →lietou``. **keyword text is NEVER touched** (published content).
  Rows without a prefix (the local-stack demo world's xiaohongshu/
  douyin/zhihu data) stay as they are — the seeds already cover them.

Downgrade restores the CHECK and drops the table; it fails loudly on
any backfilled row (the restored CHECK rejects the new codes), which is
the deliberate guard — un-backfilling would rewrite published keywords.
"""

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

#: spec §2: keyword prefix -> real platform code. Ordered — a keyword
#: matching several prefixes is an input bug the LAST match wins for;
#: the production set is disjoint.
_BACKFILL_RULES: tuple[tuple[str, str], ...] = (
    ("BOSS直聘%", "boss-zhipin"),
    ("智联招聘%", "zhaopin"),
    ("58同城%", "58city"),
    ("猎聘%", "lietou"),
)

_PLATFORMS_COLUMNS = ("code", "display_name", "sort_order")
_SEED_ROWS: tuple[tuple[str, str, int], ...] = (
    ("xiaohongshu", "小红书", 10),
    ("douyin", "抖音", 20),
    ("zhihu", "知乎", 30),
    ("boss-zhipin", "BOSS直聘", 40),
    ("zhaopin", "智联招聘", 50),
    ("58city", "58同城", 60),
    ("lietou", "猎聘", 70),
)


def upgrade() -> None:
    platforms = op.create_table(
        "platforms",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code"),
    )
    op.bulk_insert(
        platforms,
        [dict(zip(_PLATFORMS_COLUMNS, row, strict=True)) for row in _SEED_ROWS],
    )
    # Order is load-bearing: the CHECK must drop BEFORE the backfill
    # writes codes outside the old closed set, and the FK lands last so
    # it finds every row already referencing a seeded code. Idempotent
    # by shape (LIKE only matches the placeholder pattern once).
    # The short name lets alembic's target-metadata naming convention
    # assemble the real DB name (ck_assignments_platform); the full
    # name would be wrapped twice.
    op.drop_constraint("platform", "assignments", type_="check")
    for prefix, code in _BACKFILL_RULES:
        op.execute(
            sa.text(
                "UPDATE assignments SET platform = :code "
                "WHERE platform = 'zhihu' AND keyword LIKE :prefix"
            ).bindparams(code=code, prefix=prefix)
        )
    op.create_foreign_key(
        "fk_assignments_platform_code",
        "assignments",
        "platforms",
        ["platform"],
        ["code"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_assignments_platform_code", "assignments", type_="foreignkey"
    )
    # Raw DDL for the CHECK's convention name: op.create_check_constraint
    # does not apply the metadata naming convention, and the restored
    # name must be exactly ck_assignments_platform or the next upgrade's
    # drop would miss it.
    op.execute(
        "ALTER TABLE assignments ADD CONSTRAINT ck_assignments_platform "
        "CHECK (platform IN ('xiaohongshu', 'douyin', 'zhihu'))"
    )
    op.drop_table("platforms")
