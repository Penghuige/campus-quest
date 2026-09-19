# backend/app/modules/tasks/importer.py
"""Assignment batch import: preview then confirm (spec §7.1; plan 03 T4).

Flow (spec §7.1 MUST): upload -> parse -> pre-check -> present valid
count / error count / per-row errors -> explicit user confirmation ->
single-transaction write.

Design decisions:

- **CSV only for V1** (the binding minimum; spec §7.1 says Teacher 可
  CSV/XLSX 导入). The XLSX seam is `_parse_csv`: everything downstream of
  it speaks already-canonicalized ``(platform, keyword)`` pairs, so an
  XLSX parser is a sibling reader in front of the same
  validate/store/confirm pipeline — no other change needed here.
- **Teacher uploads are untrusted** (backend-engineering §14): bounded
  file size — the byte cap fast-fails BEFORE any parsing, which is what
  bounds all downstream parse work (the reader itself materializes the
  whole decoded file); bounded row count (validation stops one row past
  the cap); bounded cell length (``csv.field_size_limit`` is raised
  above the byte cap at construction so an oversized cell parses and
  reports TEXT_TOO_LONG instead of crashing the reader); strict UTF-8
  decode; and no formula execution — CSV cells are stored as plain text
  data, never interpreted. Every ``csv.Error`` the strict reader still
  raises (e.g. an unterminated quote) is converted into a file-level
  MALFORMED_CSV validation outcome — parser exceptions never escape
  this module.
- **Row numbers are 1-based data-row indexes** (the header is not a data
  row; blank lines are skipped without consuming a row number, matching
  what a teacher sees in a spreadsheet).
- **File-level vs row-level errors.** Oversize, bad encoding, an
  undialectable file, a wrong header, or exceeding the per-import row cap
  rejects the whole preview and mints NO token — nothing in such a file
  is confirmable. Row-level errors keep their valid rows importable.
- **Preview payload is stored server-side, immutable, single-use.**
  Preview mints a 256-bit ``secrets.token_urlsafe`` token and stores the
  canonicalized valid rows as one Redis string under
  ``assignment-import:preview:<token>``; confirm consumes it with a
  single ``GETDEL`` (the OTP verified-token pattern: exactly one
  concurrent caller receives the payload) and imports exactly the stored
  rows — never a re-upload. The Redis key holds the raw token rather
  than an HMAC of it (unlike OTP, spec §33.2 mandates no plaintext OTP
  material in Redis; replaying an import token only repeats an
  already-authorized insert of already-vetted rows, and the token is
  burned on first use anyway). Lifetime is a business TTL stamped inside
  the payload and checked against the injected Clock at confirm; the
  Redis TTL is the cleanup backstop (business TTL + grace, the OTP
  module's discipline).
- **The token is bound to the task, not the previewing account.** Both
  preview and confirm re-run the same authorization (owner,
  MANAGE_ASSIGNMENTS collaborator, or Admin): two authorized collaborators
  may share a preview/confirm hand-off, and an unauthorized caller can
  neither mint nor consume a token. Authorization runs before the
  consume, so a denied confirm never burns a legitimate token.
- **Races between preview and confirm** (spec §7.1: 正式写入时仍依赖
  数据库 UNIQUE 兜底) are closed by UNIQUE(task_id, platform, keyword):
  confirm pre-checks existing pairs for a friendly typed 409, and the
  IntegrityError a concurrent importer still triggers is translated into
  the SAME typed conflict — only that constraint name is converted, any
  other database failure propagates (backend-engineering §7).
- **Transaction shape** (§5): one ``flush`` to evaluate constraints, one
  ``commit`` at the end of the use case; helpers never commit.
- Canonicalization: platform is case-insensitively mapped onto the
  controlled codes (the models' CHECK set); keyword is edge-trimmed only
  (``str.strip`` removes Unicode edge whitespace) with internal Unicode —
  spacing, composed/decomposed forms — preserved byte-exactly, because
  spec §7 compares keyword 按精确文本 and the UNIQUE constraint dedups
  on exactly what we store.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.identity.events import Actor
from app.modules.tasks.collaborator_service import CollaboratorPermission
from app.modules.tasks.enums import AssignmentAvailability
from app.modules.tasks.models import Assignment, Task, TaskCollaborator
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "ImportErrorCode",
    "SUPPORTED_IMPORT_PLATFORMS",
    "AssignmentImportError",
    "AssignmentImportPreview",
    "AssignmentImportResult",
    "AssignmentImportService",
    "AssignmentPreviewRow",
    "DuplicateAssignmentsError",
    "InvalidPreviewTokenError",
]

logger = logging.getLogger(__name__)

# Canonical platform codes (spec §7): case-insensitive input, exact output.
# Parity with the assignments CHECK constraint set is pinned by tests.
SUPPORTED_IMPORT_PLATFORMS: frozenset[str] = frozenset(
    {"xiaohongshu", "douyin", "zhihu"}
)

_PREVIEW_KEY_PREFIX = "assignment-import:preview:"
_UNIQUE_CONSTRAINT = "uq_assignments_task_id_platform_keyword"
# Redis backstop TTL = business TTL + grace (see module docstring).
_REDIS_TTL_GRACE_SECONDS = 600
# Dialect sniffing: restrict the candidate delimiters so a one-column file
# cannot be sniffed as something odd, and bound the sample size.
_CSV_DELIMITERS = ",;\t"
_SNIFF_SAMPLE_BYTES = 8192
_HEADER = ("platform", "keyword")
# Cap on raw values echoed into preview error DTOs (§14 safe truncation).
_ECHO_MAX_LENGTH = 64


# --- stable row/file error codes (spec §7.1 detection list) -----------------------


class ImportErrorCode(StrEnum):
    """Stable code for every preview error; ``value == member name``.

    ``FILE_TOO_LARGE`` reuses the §29 registry code name for oversize
    uploads; the rest are import-specific and travel only inside the
    preview DTO (T9 may surface them through the API layer verbatim).
    """

    EMPTY_PLATFORM = "EMPTY_PLATFORM"
    EMPTY_KEYWORD = "EMPTY_KEYWORD"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    DUPLICATE_IN_FILE = "DUPLICATE_IN_FILE"
    DUPLICATE_IN_DB = "DUPLICATE_IN_DB"
    TEXT_TOO_LONG = "TEXT_TOO_LONG"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    INVALID_ENCODING = "INVALID_ENCODING"
    MALFORMED_CSV = "MALFORMED_CSV"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"


# --- messages (§29 envelope text) --------------------------------------------------

_DENIED_MESSAGE = (
    "只有任务所有者、拥有 MANAGE_ASSIGNMENTS 权限的协作者或管理员可以导入 Assignment"
)
_FILE_TOO_LARGE_MESSAGE = "导入文件超过大小上限"
_INVALID_ENCODING_MESSAGE = "文件编码必须是 UTF-8"
_BAD_DIALECT_MESSAGE = "无法识别 CSV 分隔符格式"
_BAD_STRUCTURE_MESSAGE = "CSV 结构无法解析（例如未闭合的引号）"
_BAD_HEADER_MESSAGE = "CSV 表头必须是 platform,keyword"
_EMPTY_FILE_MESSAGE = "CSV 文件没有数据行"
_ROW_COLUMNS_MESSAGE = "每行必须恰好是 platform,keyword 两列"
_EMPTY_PLATFORM_MESSAGE = "platform 不能为空"
_EMPTY_KEYWORD_MESSAGE = "keyword 不能为空"
_UNSUPPORTED_PLATFORM_MESSAGE = "不支持的 platform"
_DUPLICATE_IN_FILE_MESSAGE = "文件内重复的 platform + keyword 组合"
_DUPLICATE_IN_DB_MESSAGE = "与任务现有 Assignment 重复"
_TEXT_TOO_LONG_MESSAGE = "keyword 超过最大长度"
_LIMIT_EXCEEDED_MESSAGE = "单次导入行数超过上限"
_PREVIOUS_IMPORT_RACE_MESSAGE = "部分组合已被并发导入，请重新预览后确认"
_PREVIOUS_IMPORT_MESSAGE = "部分组合已存在于该任务，请移除后重新预览"
_TOKEN_INVALID_MESSAGE = "导入预览不存在、已失效或已被确认"


# --- typed exceptions (router-mapped; T9 seam) -------------------------------------


class DuplicateAssignmentsError(BusinessError):
    """Some previewed pairs already exist in the task (typed 409).

    ``conflicts`` carries the pre-checked pairs; the race-closed
    IntegrityError path passes none (the constraint violation names no
    row), which is why the message then points at re-previewing. The
    registry has no dedicated import-conflict code (same posture as
    ``DuplicateCollaboratorError``): ``VALIDATION_ERROR`` with 409.
    """

    def __init__(self, conflicts: Sequence[tuple[str, str]]) -> None:
        details: dict[str, Any] | None = None
        if conflicts:
            details = {
                "conflicts": [
                    {"platform": platform, "keyword": keyword}
                    for platform, keyword in conflicts
                ]
            }
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _PREVIOUS_IMPORT_RACE_MESSAGE
            if not conflicts
            else _PREVIOUS_IMPORT_MESSAGE,
            status_code=409,
            details=details,
        )


class InvalidPreviewTokenError(BusinessError):
    """Unknown, expired, already-consumed, or task-mismatched token.

    All four indistinguishable to the caller (NOT_FOUND/404): the remedy
    is the same — re-preview — and distinguishing them would leak preview
    lifecycle state.
    """

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _TOKEN_INVALID_MESSAGE,
            status_code=404,
        )


# --- DTOs (explicit, never serialized ORM objects) ---------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentImportError:
    """One preview error; ``row_number is None`` marks a file-level error.

    ``details`` carries the limits behind file-level rejections (size,
    rows, limit) so the error is actionable without re-uploading.
    """

    code: ImportErrorCode
    message: str
    row_number: int | None = None
    platform: str | None = None
    keyword: str | None = None
    details: Mapping[str, int] | None = None


@dataclass(frozen=True, slots=True)
class AssignmentPreviewRow:
    """One canonicalized importable row, with its source row number."""

    row_number: int
    platform: str
    keyword: str


@dataclass(frozen=True, slots=True)
class AssignmentImportPreview:
    """Preview outcome (spec §7.1 step 4: counts + per-row errors).

    ``preview_token`` is not None iff at least one row is importable and
    no file-level error rejected the file; it is the ONLY handle confirm
    accepts, and the payload behind it is exactly ``valid`` as shown.
    """

    task_id: UUID
    total_rows: int
    valid: tuple[AssignmentPreviewRow, ...]
    errors: tuple[AssignmentImportError, ...]
    preview_token: str | None
    expires_at: datetime | None

    @property
    def valid_count(self) -> int:
        return len(self.valid)

    @property
    def error_count(self) -> int:
        return len(self.errors)


@dataclass(frozen=True, slots=True)
class AssignmentImportResult:
    """Outcome of a successful confirm: what landed, all-or-nothing."""

    task_id: UUID
    inserted: int
    rows: tuple[AssignmentPreviewRow, ...]


def _preview_key(token: str) -> str:
    return f"{_PREVIEW_KEY_PREFIX}{token}"


def _header_match_dialect(text: str) -> csv.Dialect | None:
    """The first candidate delimiter whose header line reads
    ``platform,keyword`` (the sniff-failure fallback; see _parse_csv)."""
    header_line = text.splitlines()[0] if text.splitlines() else ""
    for delimiter in _CSV_DELIMITERS:
        if (
            tuple(cell.strip().lower() for cell in header_line.split(delimiter))
            == _HEADER
        ):
            # Sniffer's own dynamically-built dialect, same trick.
            dialect: csv.Dialect = type(
                "HeaderMatch", (csv.excel,), {"delimiter": delimiter}
            )()
            return dialect
    return None


# --- authorization -----------------------------------------------------------------


async def _require_import_access(db: AsyncSession, task: Task, actor: Actor) -> None:
    """Owner, MANAGE_ASSIGNMENTS collaborator, or Admin (spec §4.2)."""
    if actor.user_id == task.owner_teacher_id or is_admin(actor.role):
        return
    permissions = await db.scalar(
        select(TaskCollaborator.permissions).where(
            TaskCollaborator.task_id == task.id,
            TaskCollaborator.teacher_id == actor.user_id,
        )
    )
    if (
        permissions is None
        or CollaboratorPermission.MANAGE_ASSIGNMENTS not in permissions
    ):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _DENIED_MESSAGE,
            status_code=403,
        )


async def _existing_pairs(
    db: AsyncSession, task_id: UUID, candidates: Sequence[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Which candidate pairs already exist in the task (one query)."""
    result = await db.execute(
        select(Assignment.platform, Assignment.keyword).where(
            Assignment.task_id == task_id,
            tuple_(Assignment.platform, Assignment.keyword).in_(candidates),
        )
    )
    return {(platform, keyword) for platform, keyword in result.all()}


# --- the service -------------------------------------------------------------------


class AssignmentImportService:
    """Preview and confirm Assignment batch imports (spec §7.1).

    The scalars (caps, TTL) are injected — the composition root maps
    them from the ``assignment_import_*`` settings — so the service is
    testable without a deployment environment. ``redis`` follows the OTP
    module's pattern: a shared ``decode_responses=True`` client.
    """

    def __init__(
        self,
        *,
        redis: aioredis.Redis,
        clock: Clock,
        max_file_bytes: int,
        max_rows: int,
        keyword_max_length: int,
        preview_ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._max_file_bytes = max_file_bytes
        self._max_rows = max_rows
        self._keyword_max_length = keyword_max_length
        self._preview_ttl_seconds = preview_ttl_seconds
        # csv's default field limit (131072 chars) would turn one large
        # cell into a csv.Error crash; raised above the byte cap, every
        # cell the size gate admitted is parseable and the row check can
        # classify it as TEXT_TOO_LONG instead (engineering §14: parser
        # exceptions become validation outcomes). UTF-8 guarantees
        # #chars <= #bytes, so the byte cap bounds the char count. The
        # limit is process-global, hence raise-only: a small-caps
        # instance must never tighten it for others.
        if max_file_bytes > csv.field_size_limit():
            csv.field_size_limit(max_file_bytes)

    # -- preview ----------------------------------------------------------------

    async def preview_assignments(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        data: bytes,
    ) -> AssignmentImportPreview:
        """Parse and pre-check an upload; mint a confirm token iff
        something is importable (spec §7.1 steps 1-4).

        File-level failures return a preview DTO carrying a single
        file-level error and no token — they are validation outcomes, not
        exceptions (backend-engineering §14).
        """
        task = await db.scalar(select(Task).where(Task.id == task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        await _require_import_access(db, task, actor)

        if len(data) > self._max_file_bytes:
            return self._file_rejected(
                task_id,
                AssignmentImportError(
                    ImportErrorCode.FILE_TOO_LARGE,
                    _FILE_TOO_LARGE_MESSAGE,
                    details={
                        "size": len(data),
                        "limit": self._max_file_bytes,
                    },
                ),
            )

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return self._file_rejected(
                task_id,
                AssignmentImportError(
                    ImportErrorCode.INVALID_ENCODING, _INVALID_ENCODING_MESSAGE
                ),
            )

        rows, file_error = self._parse_csv(text)
        if file_error is not None:
            return self._file_rejected(task_id, file_error)
        assert rows is not None  # _parse_csv returns (rows, None) or (None, error)

        valid: list[AssignmentPreviewRow] = []
        errors: list[AssignmentImportError] = []
        seen: set[tuple[str, str]] = set()
        row_number = 0
        for raw in rows:
            if not any(cell.strip() for cell in raw):
                continue  # blank line: not a spreadsheet row
            row_number += 1
            if row_number > self._max_rows:
                return self._file_rejected(
                    task_id,
                    AssignmentImportError(
                        ImportErrorCode.LIMIT_EXCEEDED,
                        _LIMIT_EXCEEDED_MESSAGE,
                        details={"rows": row_number, "limit": self._max_rows},
                    ),
                )
            if len(raw) != 2:
                errors.append(
                    AssignmentImportError(
                        ImportErrorCode.MALFORMED_CSV,
                        _ROW_COLUMNS_MESSAGE,
                        row_number=row_number,
                    )
                )
                continue
            platform_raw, keyword_raw = raw
            platform_input = platform_raw.strip()
            keyword = keyword_raw.strip()
            if not platform_input:
                errors.append(
                    AssignmentImportError(
                        ImportErrorCode.EMPTY_PLATFORM,
                        _EMPTY_PLATFORM_MESSAGE,
                        row_number=row_number,
                    )
                )
                continue
            platform = platform_input.lower()
            if platform not in SUPPORTED_IMPORT_PLATFORMS:
                errors.append(
                    AssignmentImportError(
                        ImportErrorCode.UNSUPPORTED_PLATFORM,
                        _UNSUPPORTED_PLATFORM_MESSAGE,
                        row_number=row_number,
                        # Echo for display, truncated: the raw value is
                        # unbounded (a giant cell parses — see _parse_csv
                        # — and fails this check), and §14 keeps preview
                        # echoes small.
                        platform=platform_raw[:_ECHO_MAX_LENGTH],
                    )
                )
                continue
            if not keyword:
                errors.append(
                    AssignmentImportError(
                        ImportErrorCode.EMPTY_KEYWORD,
                        _EMPTY_KEYWORD_MESSAGE,
                        row_number=row_number,
                    )
                )
                continue
            if len(keyword) > self._keyword_max_length:
                # The oversize keyword is deliberately not echoed (§14
                # safe preview truncation); the message carries the cap.
                message = (
                    f"{_TEXT_TOO_LONG_MESSAGE}"
                    f"（不超过 {self._keyword_max_length} 字符）"
                )
                errors.append(
                    AssignmentImportError(
                        ImportErrorCode.TEXT_TOO_LONG,
                        message,
                        row_number=row_number,
                    )
                )
                continue
            pair = (platform, keyword)
            if pair in seen:
                errors.append(
                    AssignmentImportError(
                        ImportErrorCode.DUPLICATE_IN_FILE,
                        _DUPLICATE_IN_FILE_MESSAGE,
                        row_number=row_number,
                        platform=platform,
                        keyword=keyword,
                    )
                )
                continue
            seen.add(pair)
            valid.append(
                AssignmentPreviewRow(
                    row_number=row_number, platform=platform, keyword=keyword
                )
            )

        if not valid and not errors:
            return self._file_rejected(
                task_id,
                AssignmentImportError(
                    ImportErrorCode.MALFORMED_CSV, _EMPTY_FILE_MESSAGE
                ),
            )

        if valid:
            existing = await _existing_pairs(
                db, task_id, [(row.platform, row.keyword) for row in valid]
            )
            if existing:
                kept: list[AssignmentPreviewRow] = []
                for row in valid:
                    if (row.platform, row.keyword) in existing:
                        errors.append(
                            AssignmentImportError(
                                ImportErrorCode.DUPLICATE_IN_DB,
                                _DUPLICATE_IN_DB_MESSAGE,
                                row_number=row.row_number,
                                platform=row.platform,
                                keyword=row.keyword,
                            )
                        )
                    else:
                        kept.append(row)
                valid = kept

        errors.sort(key=lambda error: error.row_number or 0)

        preview_token: str | None = None
        expires_at = None
        if valid:
            now = self._clock.now()
            expires_at = now + timedelta(seconds=self._preview_ttl_seconds)
            preview_token = secrets.token_urlsafe(32)
            payload = json.dumps(
                {
                    "task_id": str(task_id),
                    "rows": [
                        {
                            "row_number": row.row_number,
                            "platform": row.platform,
                            "keyword": row.keyword,
                        }
                        for row in valid
                    ],
                    "expires_at": expires_at.timestamp(),
                },
                ensure_ascii=False,
            )
            await self._redis.set(
                _preview_key(preview_token),
                payload,
                ex=self._preview_ttl_seconds + _REDIS_TTL_GRACE_SECONDS,
            )

        logger.info(
            "assignment import previewed task_id=%s rows=%d valid=%d errors=%d",
            task_id,
            row_number,
            len(valid),
            len(errors),
        )
        return AssignmentImportPreview(
            task_id=task_id,
            total_rows=row_number,
            valid=tuple(valid),
            errors=tuple(errors),
            preview_token=preview_token,
            expires_at=expires_at,
        )

    # -- confirm ----------------------------------------------------------------

    async def confirm_assignments(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        preview_token: str,
    ) -> AssignmentImportResult:
        """Insert exactly the previewed rows in one transaction (spec
        §7.1 steps 5-6).

        Authorization runs before the single ``GETDEL`` consume, so a
        denied caller never burns a token; the friendly duplicate
        pre-check runs before the flush; the UNIQUE constraint is the
        race closer, and its IntegrityError becomes the same typed 409.
        """
        task = await db.scalar(select(Task).where(Task.id == task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        await _require_import_access(db, task, actor)

        rows = await self._consume_preview(task_id, preview_token)

        conflicts = await _existing_pairs(
            db, task_id, [(row.platform, row.keyword) for row in rows]
        )
        if conflicts:
            raise DuplicateAssignmentsError(sorted(conflicts))

        db.add_all(
            [
                Assignment(
                    task_id=task_id,
                    platform=row.platform,
                    keyword=row.keyword,
                    availability_status=AssignmentAvailability.AVAILABLE.value,
                )
                for row in rows
            ]
        )
        try:
            await db.flush()
        except IntegrityError as exc:
            if _UNIQUE_CONSTRAINT in str(exc):
                raise DuplicateAssignmentsError([]) from exc
            raise
        await db.commit()

        logger.info(
            "assignment import confirmed task_id=%s inserted=%d",
            task_id,
            len(rows),
        )
        return AssignmentImportResult(task_id=task_id, inserted=len(rows), rows=rows)

    # -- internals ----------------------------------------------------------------

    async def _consume_preview(
        self, task_id: UUID, preview_token: str
    ) -> tuple[AssignmentPreviewRow, ...]:
        """Atomically consume the token and return its immutable rows.

        ``GETDEL`` is the single consume: exactly one concurrent caller
        receives the payload (the OTP verified-token discipline).
        Unknown, already-consumed, expired, task-mismatched, and
        malformed payloads are all the same typed NOT_FOUND.
        """
        raw = await self._redis.getdel(_preview_key(preview_token))
        if raw is None:
            raise InvalidPreviewTokenError
        try:
            loaded: object = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise InvalidPreviewTokenError from None
        if not isinstance(loaded, dict):
            raise InvalidPreviewTokenError
        raw_task_id = loaded.get("task_id")
        raw_rows = loaded.get("rows")
        raw_expires_at = loaded.get("expires_at")
        if not isinstance(raw_task_id, str) or not isinstance(raw_rows, list):
            raise InvalidPreviewTokenError
        try:
            bound_task = UUID(raw_task_id)
        except ValueError:
            raise InvalidPreviewTokenError from None
        if bound_task != task_id:
            raise InvalidPreviewTokenError
        if not isinstance(raw_expires_at, (int, float)):
            raise InvalidPreviewTokenError
        if float(raw_expires_at) <= self._clock.now().timestamp():
            raise InvalidPreviewTokenError
        rows: list[AssignmentPreviewRow] = []
        for item in raw_rows:
            if not isinstance(item, dict):
                raise InvalidPreviewTokenError
            row_number = item.get("row_number")
            platform = item.get("platform")
            keyword = item.get("keyword")
            if (
                not isinstance(row_number, int)
                or not isinstance(platform, str)
                or not isinstance(keyword, str)
            ):
                raise InvalidPreviewTokenError
            rows.append(
                AssignmentPreviewRow(
                    row_number=row_number, platform=platform, keyword=keyword
                )
            )
        if not rows:
            raise InvalidPreviewTokenError
        return tuple(rows)

    def _file_rejected(
        self,
        task_id: UUID,
        error: AssignmentImportError,
    ) -> AssignmentImportPreview:
        logger.info(
            "assignment import rejected task_id=%s code=%s",
            task_id,
            error.code.value,
        )
        return AssignmentImportPreview(
            task_id=task_id,
            total_rows=0,
            valid=(),
            errors=(error,),
            preview_token=None,
            expires_at=None,
        )

    @staticmethod
    def _parse_csv(
        text: str,
    ) -> tuple[list[list[str]] | None, AssignmentImportError | None]:
        """Sniff the dialect and read the file.

        Returns ``(rows, None)`` on success (rows exclude the header) or
        ``(None, error)`` for a file-level rejection. Sniffing is primary
        (it catches ``;``/tab files), but a ragged file defeats the
        sniffer's per-line consistency check — exactly the file that
        needs per-row column errors — so a sniff failure falls back to
        any candidate delimiter whose header line reads as
        ``platform,keyword``; only a file readable under no candidate is
        rejected as undialectable. The XLSX parser (spec §7.1 CSV/XLSX)
        plugs in as a sibling returning the same shape.
        """
        sample = text[:_SNIFF_SAMPLE_BYTES]
        if not sample.strip():
            return None, AssignmentImportError(
                ImportErrorCode.MALFORMED_CSV, _EMPTY_FILE_MESSAGE
            )
        # csv.reader accepts a Dialect instance or its class (Sniffer
        # returns the latter), so both flow into `dialect`.
        dialect: csv.Dialect | type[csv.Dialect] | None
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=_CSV_DELIMITERS)
        except csv.Error:
            dialect = None
        if dialect is None:
            dialect = _header_match_dialect(text)
            if dialect is None:
                return None, AssignmentImportError(
                    ImportErrorCode.MALFORMED_CSV, _BAD_DIALECT_MESSAGE
                )
        reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
        try:
            # strict=True turns structural defects (an unterminated
            # quoted field, ...) into csv.Error here; that is a
            # validation outcome, never an exception past this module
            # (engineering §14). Oversize cells cannot raise anymore:
            # field_size_limit was raised above the byte cap at
            # construction, so they parse and the row check reports
            # TEXT_TOO_LONG.
            rows = list(reader)
        except csv.Error:
            return None, AssignmentImportError(
                ImportErrorCode.MALFORMED_CSV, _BAD_STRUCTURE_MESSAGE
            )
        if not rows:
            return None, AssignmentImportError(
                ImportErrorCode.MALFORMED_CSV, _EMPTY_FILE_MESSAGE
            )
        header = tuple(cell.strip().lower() for cell in rows[0])
        if header != _HEADER:
            return None, AssignmentImportError(
                ImportErrorCode.MALFORMED_CSV, _BAD_HEADER_MESSAGE
            )
        return rows[1:], None
