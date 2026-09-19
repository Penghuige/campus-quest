# backend/tests/unit/tasks/test_assignment_importer.py
"""Assignment batch-import preview/confirm (spec §7.1; plan 03 T4).

Strategy (documented per the task brief): parsing, canonicalization,
row-error taxonomy, token mint/consume semantics, and the friendly-vs-race
duplicate split are pure service logic over untrusted bytes, so these are
unit tests over duck-typed fakes — no PostgreSQL, no Redis. The fakes
implement exactly the surface `AssignmentImportService` uses:

- ``InMemoryRedis`` — ``set(key, value, ex)`` / ``getdel(key)`` (the OTP
  unit suite's pattern; the single-use consume is the behavior under test,
  not real Redis TTLs — expiry is driven by the payload timestamp checked
  against the injected frozen clock).
- ``FakeSession`` — ``scalar`` on the Task and TaskCollaborator lookups,
  ``execute`` returning seeded existing Assignment pairs, ``add_all`` /
  ``flush`` (programmable to raise a SQLAlchemy IntegrityError so the
  constraint translation is pinned deterministically), ``commit``.

Real PostgreSQL UNIQUE behavior, real GETDEL, and the concurrent-confirm
race live in tests/integration/tasks/test_assignment_import.py.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.tasks.importer import (
    SUPPORTED_IMPORT_PLATFORMS,
    AssignmentImportPreview,
    AssignmentImportResult,
    AssignmentImportService,
    DuplicateAssignmentsError,
    ImportErrorCode,
    InvalidPreviewTokenError,
)
from app.modules.tasks.models import Assignment, Task, TaskCollaborator
from app.modules.tasks.service import TaskNotFoundError

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MAX_FILE_BYTES = 64 * 1024
MAX_ROWS = 100
KEYWORD_MAX_LENGTH = 16
PREVIEW_TTL_SECONDS = 900

UNIQUE_CONSTRAINT = "uq_assignments_task_id_platform_keyword"
PREVIEW_KEY_PREFIX = "assignment-import:preview:"


def _key(token: str) -> str:
    return f"{PREVIEW_KEY_PREFIX}{token}"


# --- fakes ------------------------------------------------------------------------


class InMemoryRedis:
    """The exact async surface the importer touches (see module docstring)."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value
        self.ttls[key] = ex if ex is not None else 0

    async def getdel(self, key: str) -> str | None:
        self.ttls.pop(key, None)
        return self.strings.pop(key, None)


class FakeResult:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, str]]:
        return self._rows


class FakeSession:
    """Duck-typed AsyncSession for AssignmentImportService."""

    def __init__(
        self,
        *,
        tasks: list[Task] | None = None,
        collaborators: dict[tuple[UUID, UUID], list[str]] | None = None,
        existing_pairs: list[tuple[str, str]] | None = None,
    ) -> None:
        self.tasks = {task.id: task for task in (tasks or [])}
        self.collaborators = collaborators or {}
        self.existing_pairs = list(existing_pairs or [])
        self.added: list[Any] = []
        self.commits = 0
        self.flush_error: Exception | None = None

    def fail_flush_with(self, error: Exception) -> None:
        self.flush_error = error

    def add_all(self, objects: list[Any]) -> None:
        self.added.extend(objects)

    async def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    async def commit(self) -> None:
        await self.flush()
        self.commits += 1

    async def scalar(self, stmt: Any) -> Any:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Task:
            return self.tasks.get(self._where_values(stmt)[0])
        if entity is TaskCollaborator:
            values = self._where_values(stmt)
            for (task_id, teacher_id), permissions in self.collaborators.items():
                if {task_id, teacher_id} <= set(values):
                    return permissions
            return None
        raise AssertionError(f"unexpected scalar query for {entity!r}")

    async def execute(self, stmt: Any) -> FakeResult:
        entity = stmt.column_descriptions[0]["entity"]
        assert entity is Assignment, f"unexpected execute query for {entity!r}"
        return FakeResult(list(self.existing_pairs))

    @staticmethod
    def _where_values(stmt: Any) -> list[Any]:
        """Literal values from the WHERE clause (single or AND-joined)."""
        clause = stmt.whereclause
        values: list[Any] = []
        stack: list[Any] = [clause]
        while stack:
            node = stack.pop()
            clauses = getattr(node, "clauses", None)
            if clauses is not None:
                stack.extend(clauses)
            else:
                values.append(node.right.value)
        return values


# --- helpers ----------------------------------------------------------------------


def _actor(role: Role, user_id: UUID | None = None) -> Actor:
    return Actor(user_id=user_id or uuid4(), role=role)


def _owner() -> Actor:
    return _actor(Role.TEACHER)


def _task(owner: Actor | None = None) -> Task:
    owner = owner or _owner()
    return Task(
        id=uuid4(),
        owner_teacher_id=owner.user_id,
        title="已就绪任务",
        description="desc",
        task_type="DATA_CRAWL",
        rarity="NORMAL",
        base_reward_points=100,
        status="PUBLISHED",
        deadline_mode="RELATIVE",
        duration_minutes=7200,
        allowed_file_types=["CSV"],
        max_file_size_bytes=1024,
        notification_channels=["SMS"],
    )


def _service(
    redis: InMemoryRedis | None = None,
    clock: _FrozenClockProxy | None = None,
    **caps: int,
) -> tuple[AssignmentImportService, InMemoryRedis]:
    values: dict[str, int] = {
        "max_file_bytes": MAX_FILE_BYTES,
        "max_rows": MAX_ROWS,
        "keyword_max_length": KEYWORD_MAX_LENGTH,
        "preview_ttl_seconds": PREVIEW_TTL_SECONDS,
    }
    values.update(caps)
    redis_fake = redis or InMemoryRedis()
    return (
        AssignmentImportService(
            redis=redis_fake,  # type: ignore[arg-type]
            clock=clock or _FrozenClockProxy(),
            **values,  # type: ignore[arg-type]
        ),
        redis_fake,
    )


class _FrozenClockProxy:
    """Clock pinned at NOW; tests flip `.current` to advance time."""

    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


def _csv(
    *rows: tuple[str, str], header: tuple[str, str] = ("platform", "keyword")
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


async def _preview(
    service: AssignmentImportService,
    db: FakeSession,
    task: Task,
    data: bytes,
    actor: Actor | None = None,
) -> AssignmentImportPreview:
    return await service.preview_assignments(
        db, actor or _actor(Role.TEACHER, task.owner_teacher_id), task.id, data
    )


# --- preview: valid rows and canonicalization -------------------------------------


async def test_valid_rows_canonicalized_and_token_minted() -> None:
    service, redis = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    preview = await _preview(
        service,
        db,
        task,
        _csv(
            ("XiaoHongShu", "  考研 经验帖  "),
            ("DOUYIN", "Python 入门"),
            ("zhihu", "café 指南"),
        ),
    )

    assert preview.task_id == task.id
    assert preview.total_rows == 3
    assert [(row.platform, row.keyword, row.row_number) for row in preview.valid] == [
        ("xiaohongshu", "考研 经验帖", 1),
        ("douyin", "Python 入门", 2),
        ("zhihu", "café 指南", 3),
    ]
    assert preview.errors == ()
    assert preview.preview_token is not None
    assert preview.expires_at == NOW + timedelta(seconds=PREVIEW_TTL_SECONDS)

    # The immutable payload is stored server-side under the token, holding
    # exactly the canonicalized rows (not the raw upload).
    stored = json.loads(redis.strings[_key(preview.preview_token)])
    assert stored["task_id"] == str(task.id)
    assert stored["rows"] == [
        {"row_number": 1, "platform": "xiaohongshu", "keyword": "考研 经验帖"},
        {"row_number": 2, "platform": "douyin", "keyword": "Python 入门"},
        {"row_number": 3, "platform": "zhihu", "keyword": "café 指南"},
    ]


async def test_platform_set_matches_spec_codes() -> None:
    assert {"xiaohongshu", "douyin", "zhihu"} == SUPPORTED_IMPORT_PLATFORMS


# --- preview: row-level errors (row number + stable code) --------------------------


def _error_codes(preview: AssignmentImportPreview) -> list[tuple[int | None, str]]:
    return [(error.row_number, error.code) for error in preview.errors]


async def test_blank_keyword_error() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    preview = await _preview(
        service,
        db,
        task,
        _csv(("xiaohongshu", "考研"), ("douyin", "   ")),
    )

    assert _error_codes(preview) == [(2, ImportErrorCode.EMPTY_KEYWORD)]
    assert [row.keyword for row in preview.valid] == ["考研"]
    assert preview.preview_token is not None  # remaining rows stay confirmable


async def test_empty_platform_error() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    preview = await _preview(
        service, db, task, _csv(("", "考研"), ("douyin", "Python"))
    )

    assert _error_codes(preview) == [(1, ImportErrorCode.EMPTY_PLATFORM)]


async def test_unsupported_platform_error() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    preview = await _preview(
        service, db, task, _csv(("weibo", "热搜"), ("zhihu", "留学"))
    )

    assert _error_codes(preview) == [(1, ImportErrorCode.UNSUPPORTED_PLATFORM)]
    # The raw value is echoed for the per-row error display.
    assert preview.errors[0].platform == "weibo"


async def test_duplicate_within_file_marks_later_row() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    # Case differences canonicalize to the same pair, so this is a dup.
    preview = await _preview(
        service,
        db,
        task,
        _csv(
            ("douyin", "Python 入门"),
            ("xiaohongshu", "考研"),
            ("DOUYIN", "  Python 入门  "),
        ),
    )

    assert _error_codes(preview) == [(3, ImportErrorCode.DUPLICATE_IN_FILE)]
    assert [row.row_number for row in preview.valid] == [1, 2]


async def test_duplicate_against_database_marks_row() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task], existing_pairs=[("douyin", "Python 入门")])

    preview = await _preview(
        service,
        db,
        task,
        _csv(("douyin", "Python 入门"), ("zhihu", "留学")),
    )

    assert _error_codes(preview) == [(1, ImportErrorCode.DUPLICATE_IN_DB)]
    assert [row.keyword for row in preview.valid] == ["留学"]


async def test_keyword_too_long_error() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    preview = await _preview(
        service,
        db,
        task,
        _csv(("zhihu", "超" * (KEYWORD_MAX_LENGTH + 1))),
    )

    assert _error_codes(preview) == [(1, ImportErrorCode.TEXT_TOO_LONG)]
    assert preview.valid == ()
    assert preview.preview_token is None  # nothing confirmable


async def test_wrong_column_count_is_row_level_malformed() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    data = ("platform,keyword\nxiaohongshu,考研,extra\ndouyin,Python\n").encode()
    preview = await _preview(service, db, task, data)

    assert _error_codes(preview) == [(1, ImportErrorCode.MALFORMED_CSV)]
    assert [row.keyword for row in preview.valid] == ["Python"]


async def test_blank_lines_skipped_without_row_numbers() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    data = b"platform,keyword\n\nxiaohongshu,\xe8\x80\x83\xe7\xa0\x94\n \n"
    preview = await _preview(service, db, task, data)

    assert preview.total_rows == 1
    assert [row.keyword for row in preview.valid] == ["考研"]


async def test_utf8_bom_header_accepted() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    # Excel's "CSV UTF-8" export prepends a BOM; it must not break the
    # header check (decoded as utf-8-sig).
    data = b"\xef\xbb\xbfplatform,keyword\nzhihu,\xe7\x95\x99\xe5\xad\xa6\n"
    preview = await _preview(service, db, task, data)

    assert preview.errors == ()
    assert [row.keyword for row in preview.valid] == ["留学"]


# --- preview: file-level rejections (no token) -------------------------------------


async def test_file_too_large_rejected() -> None:
    service, _ = _service(max_file_bytes=16)
    task = _task()
    db = FakeSession(tasks=[task])

    preview = await _preview(service, db, task, _csv(("zhihu", "留学")))

    assert _error_codes(preview) == [(None, ImportErrorCode.FILE_TOO_LARGE)]
    assert preview.valid == ()
    assert preview.preview_token is None


async def test_invalid_encoding_rejected() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    data = b"platform,keyword\nzhihu,\xd0\xff\xfe"
    preview = await _preview(service, db, task, data)

    assert _error_codes(preview) == [(None, ImportErrorCode.INVALID_ENCODING)]
    assert preview.preview_token is None


async def test_row_cap_exceeded_rejected() -> None:
    service, _ = _service(max_rows=2)
    task = _task()
    db = FakeSession(tasks=[task])

    preview = await _preview(
        service,
        db,
        task,
        _csv(("zhihu", "一"), ("zhihu", "二"), ("zhihu", "三")),
    )

    assert _error_codes(preview) == [(None, ImportErrorCode.LIMIT_EXCEEDED)]
    assert preview.preview_token is None


async def test_bad_header_rejected() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    preview = await _preview(
        service, db, task, _csv(("xiaohongshu", "考研"), header=("a", "b"))
    )

    assert _error_codes(preview) == [(None, ImportErrorCode.MALFORMED_CSV)]
    assert preview.preview_token is None


async def test_unsniffable_dialect_rejected() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    # Single-column text with no delimiter: the sniffer cannot decide.
    data = "platform keyword\nxiaohongshu 考研\ndouyin Python\n".encode()
    preview = await _preview(service, db, task, data)

    assert _error_codes(preview) == [(None, ImportErrorCode.MALFORMED_CSV)]


# --- preview: permissions ----------------------------------------------------------


async def test_preview_denied_for_stranger_teacher_and_student() -> None:
    service, _ = _service()
    task = _task()
    stranger = _actor(Role.TEACHER)
    student = _actor(Role.STUDENT)
    db = FakeSession(tasks=[task])

    for actor in (stranger, student):
        with pytest.raises(BusinessError) as denied:
            await service.preview_assignments(
                db, actor, task.id, _csv(("zhihu", "留学"))
            )
        assert denied.value.code == ErrorCode.PERMISSION_DENIED
        assert denied.value.status_code == 403


async def test_preview_allows_owner_admin_and_manage_assignments() -> None:
    service, _ = _service()
    task = _task()
    manager = _actor(Role.TEACHER)
    viewer = _actor(Role.TEACHER)
    db = FakeSession(
        tasks=[task],
        collaborators={
            (task.id, manager.user_id): ["MANAGE_ASSIGNMENTS"],
            (task.id, viewer.user_id): ["VIEW_TASK"],
        },
    )
    data = _csv(("zhihu", "留学"))

    for actor in (_actor(Role.TEACHER, task.owner_teacher_id), _actor(Role.ADMIN)):
        preview = await service.preview_assignments(db, actor, task.id, data)
        assert preview.valid_count == 1

    preview = await service.preview_assignments(db, manager, task.id, data)
    assert preview.valid_count == 1

    with pytest.raises(BusinessError) as denied:
        await service.preview_assignments(db, viewer, task.id, data)
    assert denied.value.code == ErrorCode.PERMISSION_DENIED


async def test_preview_unknown_task_not_found() -> None:
    service, _ = _service()
    db = FakeSession()

    with pytest.raises(TaskNotFoundError):
        await service.preview_assignments(db, _actor(Role.ADMIN), uuid4(), b"")


# --- confirm ----------------------------------------------------------------------


async def _confirmed_preview(
    service: AssignmentImportService,
    db: FakeSession,
    task: Task,
    data: bytes,
    actor: Actor | None = None,
) -> str:
    preview = await _preview(service, db, task, data, actor)
    assert preview.preview_token is not None
    return preview.preview_token


async def test_confirm_imports_previewed_rows_in_one_commit() -> None:
    service, redis = _service()
    task = _task()
    db = FakeSession(tasks=[task])
    token = await _confirmed_preview(
        service,
        db,
        task,
        _csv(("XiaoHongShu", "  考研 经验  "), ("zhihu", "留学")),
    )

    result = await service.confirm_assignments(
        db, _actor(Role.TEACHER, task.owner_teacher_id), task.id, token
    )

    assert isinstance(result, AssignmentImportResult)
    assert result.inserted == 2
    assert [(row.platform, row.keyword) for row in result.rows] == [
        ("xiaohongshu", "考研 经验"),
        ("zhihu", "留学"),
    ]
    # Rows land with the canonical platform, the trimmed keyword, AVAILABLE
    # status, exactly one commit — and the token is consumed.
    assert len(db.added) == 2
    for assignment in db.added:
        assert isinstance(assignment, Assignment)
        assert assignment.task_id == task.id
        assert assignment.availability_status == "AVAILABLE"
    assert db.commits == 1
    assert redis.strings == {}


async def test_confirm_token_single_use() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])
    token = await _confirmed_preview(service, db, task, _csv(("zhihu", "留学")))

    await service.confirm_assignments(
        db, _actor(Role.TEACHER, task.owner_teacher_id), task.id, token
    )
    with pytest.raises(InvalidPreviewTokenError) as consumed:
        await service.confirm_assignments(
            db, _actor(Role.TEACHER, task.owner_teacher_id), task.id, token
        )
    assert consumed.value.code == ErrorCode.NOT_FOUND
    assert consumed.value.status_code == 404
    assert db.commits == 1  # the second attempt never wrote anything


async def test_confirm_unknown_token_rejected() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])

    with pytest.raises(InvalidPreviewTokenError):
        await service.confirm_assignments(
            db, _actor(Role.TEACHER, task.owner_teacher_id), task.id, "no-such-token"
        )
    assert db.added == []


async def test_confirm_expired_token_rejected() -> None:
    clock = _FrozenClockProxy()
    service, _ = _service(clock=clock)
    task = _task()
    db = FakeSession(tasks=[task])
    token = await _confirmed_preview(service, db, task, _csv(("zhihu", "留学")))

    clock.current = NOW + timedelta(seconds=PREVIEW_TTL_SECONDS + 1)
    with pytest.raises(InvalidPreviewTokenError):
        await service.confirm_assignments(
            db, _actor(Role.TEACHER, task.owner_teacher_id), task.id, token
        )
    assert db.added == []


async def test_confirm_token_bound_to_task() -> None:
    service, _ = _service()
    owner = _owner()
    task_a = _task(owner)
    task_b = _task(owner)  # same owner: only the task binding can differ
    db = FakeSession(tasks=[task_a, task_b])
    token = await _confirmed_preview(service, db, task_a, _csv(("zhihu", "留学")))

    with pytest.raises(InvalidPreviewTokenError):
        await service.confirm_assignments(db, owner, task_b.id, token)
    assert db.added == []


async def test_confirm_denied_before_token_consumed() -> None:
    service, redis = _service()
    task = _task()
    stranger = _actor(Role.TEACHER)
    db = FakeSession(tasks=[task])
    token = await _confirmed_preview(service, db, task, _csv(("zhihu", "留学")))

    with pytest.raises(BusinessError) as denied:
        await service.confirm_assignments(db, stranger, task.id, token)
    assert denied.value.code == ErrorCode.PERMISSION_DENIED
    # Authorization runs before the consume: an unauthorized caller cannot
    # burn a legitimate preview token.
    assert _key(token) in redis.strings


async def test_confirm_duplicate_rows_conflict() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task], existing_pairs=[("zhihu", "留学")])
    token = await _confirmed_preview(
        service, db, task, _csv(("zhihu", "留学"), ("douyin", "Python"))
    )

    with pytest.raises(DuplicateAssignmentsError) as conflict:
        await service.confirm_assignments(
            db, _actor(Role.TEACHER, task.owner_teacher_id), task.id, token
        )
    assert conflict.value.code == ErrorCode.VALIDATION_ERROR
    assert conflict.value.status_code == 409
    assert conflict.value.details is not None
    assert conflict.value.details["conflicts"] == [
        {"platform": "zhihu", "keyword": "留学"}
    ]
    assert db.added == []
    assert db.commits == 0


async def test_confirm_integrity_error_translated_to_conflict() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])
    token = await _confirmed_preview(service, db, task, _csv(("zhihu", "留学")))
    # The race-closing path: another importer committed this pair between
    # the pre-check and the flush, so PostgreSQL raises the UNIQUE.
    db.fail_flush_with(
        IntegrityError(
            "insert into assignments ...",
            {},
            Exception(
                f'duplicate key value violates unique constraint "{UNIQUE_CONSTRAINT}"'
            ),
        )
    )

    with pytest.raises(DuplicateAssignmentsError) as conflict:
        await service.confirm_assignments(
            db, _actor(Role.TEACHER, task.owner_teacher_id), task.id, token
        )
    assert conflict.value.status_code == 409


async def test_confirm_unknown_integrity_error_propagates() -> None:
    service, _ = _service()
    task = _task()
    db = FakeSession(tasks=[task])
    token = await _confirmed_preview(service, db, task, _csv(("zhihu", "留学")))
    db.fail_flush_with(
        IntegrityError(
            "insert into assignments ...",
            {},
            Exception('null value in column "keyword" violates not-null constraint'),
        )
    )

    # Only the expected UNIQUE constraint is converted (engineering §7);
    # unknown database failures propagate instead of becoming conflicts.
    with pytest.raises(IntegrityError):
        await service.confirm_assignments(
            db, _actor(Role.TEACHER, task.owner_teacher_id), task.id, token
        )
