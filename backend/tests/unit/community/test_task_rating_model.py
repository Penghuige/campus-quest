# backend/tests/unit/community/test_task_rating_model.py
"""TaskRating's frozen column shape (spec §20; task-ratings matrix TR5,
gap G-5 — closed in the test-gap-closures batch).

The spec's §20 field list is five entries (task_id / user_id / rating /
created_at / updated_at); the table adds only the surrogate ``id``
primary key. This freeze pins the FULL column set and the load-bearing
types so a future column addition, rename, or type edit fails a test
instead of drifting silently — the by-construction discipline of
identity's ``test_user_public_excludes_private_fields`` and points'
``test_points_column_types``.

Pure ORM-metadata assertions: no database needed. The BEHAVIORAL half
of G-5 (re-rating moves ``updated_at`` forward while ``created_at``
keeps the insert instant) is pinned in the integration suite
(``test_ratings.py::test_re_rate_moves_updated_at_forward_and_keeps_
created_at``) because PostgreSQL's transaction-stable ``now()`` makes
the savepoint harness unable to show the movement.
"""

from __future__ import annotations

from sqlalchemy import DateTime, Integer

from app.modules.community.models import TaskRating


def test_task_rating_columns_are_exactly_the_spec_fields() -> None:
    # The closed set: spec §20's five fields plus the surrogate primary
    # key and nothing else — no anonymity flag, no per-rater display
    # name, no soft-delete trio. Anything new must change this test
    # consciously (and the spec first).
    assert set(TaskRating.__table__.columns.keys()) == {
        "id",  # the surrogate PK — the spec list's silent companion
        "task_id",
        "user_id",
        "rating",
        "created_at",
        "updated_at",
    }


def test_task_rating_column_types_are_frozen() -> None:
    # The value column stays INTEGER (the 1-5 closed interval; a float
    # column would break the CHECK's meaning), and both clock columns
    # stay timezone-aware TIMESTAMPs — task_ratings is read across
    # sessions, so a naive column would silently shift display times.
    table = TaskRating.__table__
    assert type(table.columns["rating"].type) is Integer
    for clock_column in ("created_at", "updated_at"):
        column = table.columns[clock_column]
        assert type(column.type) is DateTime
        assert column.type.timezone is True
