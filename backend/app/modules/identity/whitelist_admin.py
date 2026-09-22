# backend/app/modules/identity/whitelist_admin.py
"""StudentWhitelist bulk administration: preview -> confirm imports and
entry enable/disable (Plan 08 T3; spec §5.1).

Design decisions:

- **Preview is pure classification, zero writes** (the plan's "Bulk
  imports use preview then confirm"). Every non-blank line of the UTF-8
  text gets one decision; the only database work is the single read that
  classifies DB-already-existing rows.
- **Per-row semantics come from Plan 02's own validator, reused not
  rewritten:** :func:`app.modules.identity.validation.validate_student_number`
  is the single authority for "legal student number" (ASCII digits only,
  6-20, full-width forms forbidden, leading zeros kept). The probes here
  only CATEGORIZE a validator failure for the admin-facing row code —
  they never normalize a value into the import: full-width digits are
  reported as ``FULL_WIDTH_DIGITS`` (an NFKC probe says the row WOULD be
  legal after normalization, but Plan 02 forbids normalizing, so the row
  is rejected), never silently converted.
- **The confirm binding is a replay-checked digest, not a server-side
  token.** Preview returns ``confirm_token`` — a SHA-256 digest over the
  sorted importable numbers. Confirm re-runs the same pure classification
  over the payload's row set and compares digests, so a client can never
  confirm a set that was never previewed (row added/removed/swapped ->
  typed 409). No Redis state, no TTL, no single-use burn: the digest is
  an integrity binding like an ETag, not a capability; the DB-exists
  re-check and the UNIQUE constraint below are what keep a replayed
  confirm honest (re-confirming after success deterministically reports
  every number as a conflict — see the all-or-nothing ruling).
- **Conflicts are deterministic and all-or-nothing** (plan review focus
  3). At confirm, any candidate already in the whitelist refuses the
  WHOLE import with a typed 409 whose details list exactly the colliding
  numbers — the assignment-importer precedent
  (``DuplicateAssignmentsError``), not a partial landing. The friendly
  pre-check covers the sequential case; the UNIQUE constraint on
  ``student_whitelist.student_number`` closes the concurrent race, and
  the loser's ``IntegrityError`` path rolls back, re-reads which
  candidates now exist, and raises the SAME typed conflict with the same
  list shape. Two Admins confirming overlapping imports therefore always
  resolve to one full success and one full refusal with a per-number
  duplicate list — never a silent partial overwrite.
- **Every committed import and every real toggle writes its audit row in
  the same transaction** (G12; the ``AuditLogWriter`` flush-only
  discipline): ``WHITELIST_IMPORT_CONFIRMED`` targets the import batch
  (``target_id`` = the confirm digest that names it) with the
  created/enable summary; ``WHITELIST_ENTRY_TOGGLED`` targets each
  actually-flipped entry by its id. A refused confirm writes nothing
  (nothing happened); an idempotent toggle (already in the requested
  state) writes nothing (the staff-invitation replay ruling). The
  "skipped" vocabulary belongs to the PREVIEW answer — a successful
  confirm imports every row it was given, so its audit ``details`` carry
  created/duplicated/enable only.
- **Import-time line trimming happens exactly once, here.** Plan 02's
  validator deliberately does not strip (the login boundary trims); the
  import boundary is this module's decode step: strict UTF-8
  (``utf-8-sig`` to tolerate a BOM, the assignment-importer decode
  precedent), outer whitespace trimmed per line, blank lines skipped
  from decisions but still counted for physical row numbers.
- Service-level role gate: every method requires an ADMIN actor
  (``rbac.is_admin``). Routes mount ``require_admin_actor`` on top
  (defense in depth, the ``StaffService.create_staff_invitation``
  shape); the service stays callable from workers/tests without HTTP.

Error taxonomy: ``BusinessError`` with frozen-registry codes only —
``PERMISSION_DENIED`` 403, ``VALIDATION_ERROR`` 400, and ``CONFLICT``
409 for the two typed conflict shapes (registered Plan 08 T9, closing
the W2 registration gap this module's report noted).
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rbac
from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.identity.models import StudentWhitelist
from app.modules.identity.validation import (
    STUDENT_NUMBER_DEFAULT_MAX_LEN,
    STUDENT_NUMBER_DEFAULT_MIN_LEN,
    validate_student_number,
)

__all__ = [
    "AUDIT_WHITELIST_ENTRY_TOGGLED",
    "AUDIT_WHITELIST_IMPORT_CONFIRMED",
    "InvalidWhitelistConfirmError",
    "WhitelistAdminService",
    "WhitelistImportConflictError",
    "WhitelistImportCounts",
    "WhitelistImportPreview",
    "WhitelistConfirmPayload",
    "WhitelistImportResult",
    "WhitelistRowCode",
    "WhitelistRowDecision",
    "WhitelistToggleResult",
    "classify_row",
    "import_digest",
]

logger = logging.getLogger(__name__)

# Durable audit action names (G12; Plan 08 T3), the audit-stream
# vocabulary for this surface. Defined here, beside the operations that
# emit them (the staff-service precedent); registration in
# interfaces.md is the controller's step.
AUDIT_WHITELIST_IMPORT_CONFIRMED = "WHITELIST_IMPORT_CONFIRMED"
AUDIT_WHITELIST_ENTRY_TOGGLED = "WHITELIST_ENTRY_TOGGLED"

# The polymorphic audit target type for both actions.
_AUDIT_TARGET_TYPE = "student_whitelist"

_PERMISSION_DENIED_MESSAGE = "仅管理员可以管理学生白名单"
_VALIDATION_MESSAGE = "白名单导入数据校验失败"
_CONFIRM_MISMATCH_MESSAGE = "确认载荷与预览摘要不一致，请重新预览后确认"
_IMPORT_CONFLICT_MESSAGE = "部分学号已存在于白名单，未导入任何行"
_TOGGLE_MISSING_MESSAGE = "部分学号不存在于白名单"

# Upload bounds (backend-engineering §14: admin-supplied files are
# untrusted input too). Constructor-injectable; the assignment importer
# takes the same shape of caps from settings, but this wave's config
# ruling adds only the two network-policy fields, so these are module
# defaults until a deployment asks to tune them.
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_ROWS = 5000


class WhitelistRowCode(StrEnum):
    """One row's classification (the preview vocabulary, T9 renders it)."""

    IMPORTABLE = "IMPORTABLE"
    DUPLICATE_IN_FILE = "DUPLICATE_IN_FILE"
    DUPLICATE_IN_DB = "DUPLICATE_IN_DB"
    FULL_WIDTH_DIGITS = "FULL_WIDTH_DIGITS"
    INVALID_CHARACTERS = "INVALID_CHARACTERS"
    INVALID_LENGTH = "INVALID_LENGTH"


class InvalidWhitelistConfirmError(BusinessError):
    """The confirm payload does not replay its preview digest (typed 409).

    The row set was never the previewed set: rows added, removed,
    swapped, or malformed — the client must re-preview.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(
            ErrorCode.CONFLICT,
            _CONFIRM_MISMATCH_MESSAGE,
            status_code=409,
            details={"field": "confirm_payload", "reason": reason},
        )


class WhitelistImportConflictError(BusinessError):
    """Some candidates already exist; the whole import was refused (409).

    ``details["student_numbers"]`` is the deterministic, sorted conflict
    list — the same shape from the friendly pre-check and from the
    UNIQUE-constraint race path, so the loser of two concurrent
    overlapping confirms learns exactly which numbers collided and that
    nothing of theirs was written.
    """

    def __init__(self, student_numbers: Iterable[str]) -> None:
        super().__init__(
            ErrorCode.CONFLICT,
            _IMPORT_CONFLICT_MESSAGE,
            status_code=409,
            details={"student_numbers": sorted(student_numbers)},
        )


@dataclass(frozen=True, slots=True)
class WhitelistRowDecision:
    """One non-blank line's verdict: physical row number, code, and the
    canonical ASCII student number when the line is ASCII-digit-only
    (else ``None`` — a full-width or garbage row has no canonical form)."""

    row_number: int
    code: WhitelistRowCode
    student_number: str | None = None


@dataclass(frozen=True, slots=True)
class WhitelistImportCounts:
    """Summary counts over the decisions (the preview's header line)."""

    total_rows: int
    importable: int
    duplicate_in_file: int
    duplicate_in_db: int
    full_width_digits: int
    invalid_characters: int
    invalid_length: int


@dataclass(frozen=True, slots=True)
class WhitelistImportPreview:
    """Preview's full answer: per-row decisions, counts, and the digest
    the confirm payload must carry back."""

    total_rows: int
    decisions: tuple[WhitelistRowDecision, ...]
    counts: WhitelistImportCounts
    importable: tuple[str, ...]
    confirm_token: str


@dataclass(frozen=True, slots=True)
class WhitelistConfirmPayload:
    """What confirm accepts: the previewed importable set plus the digest
    preview computed over it, and the enabled state the new rows get."""

    confirm_token: str
    enable: bool
    student_numbers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WhitelistImportResult:
    """The committed import's summary (all-or-nothing: created == all)."""

    created: int
    enable: bool


@dataclass(frozen=True, slots=True)
class WhitelistToggleResult:
    """The toggle outcome per requested number, split by what happened."""

    toggled: tuple[str, ...]
    unchanged: tuple[str, ...]


def _ascii_digits_in_length(value: str) -> bool:
    """Length probe with the shared Plan 02 bounds (no pattern rewrite)."""
    return (
        STUDENT_NUMBER_DEFAULT_MIN_LEN <= len(value) <= (STUDENT_NUMBER_DEFAULT_MAX_LEN)
    )


def classify_row(value: str) -> WhitelistRowCode:
    """Classify one trimmed line against Plan 02 semantics.

    ``validate_student_number`` is the authority; the fallback probes
    only sort the failure into an admin-actionable code (see the module
    docstring: NFKC is a DETECTION probe, never an import normalization).
    """
    try:
        validate_student_number(value)
    except ValueError:
        if value.isascii() and value.isdigit():
            # Pure ASCII digits: only the length can be wrong.
            return WhitelistRowCode.INVALID_LENGTH
        normalized = unicodedata.normalize("NFKC", value)
        if normalized.isascii() and normalized.isdigit():
            # Full-width (and lookalike) digit forms: rejected per Plan
            # 02 even though normalization would fix them; the code the
            # admin gets depends on whether it WOULD have been legal.
            if _ascii_digits_in_length(normalized):
                return WhitelistRowCode.FULL_WIDTH_DIGITS
            return WhitelistRowCode.INVALID_LENGTH
        return WhitelistRowCode.INVALID_CHARACTERS
    return WhitelistRowCode.IMPORTABLE


def import_digest(student_numbers: Iterable[str]) -> str:
    """The replay-check digest over a row SET: SHA-256 of the sorted
    numbers joined by newlines. Order-independent on purpose — the
    whitelist is a set and insert order carries no meaning — while any
    added, removed, or swapped member changes it."""
    encoded = "\n".join(sorted(student_numbers))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class WhitelistAdminService:
    """Preview/confirm whitelist imports and toggle entries (Plan 08 T3).

    ``audit`` defaults to a fresh ``AuditLogWriter`` (stateless,
    flush-only) — the default means the default writer, never "no
    auditing" (the ``StaffService`` wiring ruling).
    """

    def __init__(
        self,
        *,
        clock: Clock,
        audit: AuditLogWriter | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self._clock = clock
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()
        self._max_file_bytes = max_file_bytes
        self._max_rows = max_rows

    # -- preview ---------------------------------------------------------------

    async def preview_whitelist_import(
        self, db: AsyncSession, actor: Actor, data: bytes
    ) -> WhitelistImportPreview:
        """Classify every non-blank line of a UTF-8 whitelist text; zero writes.

        Raises ``VALIDATION_ERROR`` 400 for file-level bounds (too large,
        too many rows) — a bounded rejection, not a classification.
        """
        self._require_admin(actor)
        rows: list[tuple[int, str]] = []
        for row_number, line in enumerate(self._decode_lines(data), start=1):
            value = line.strip()
            if value:
                rows.append((row_number, value))

        codes: list[WhitelistRowCode] = []
        numbers: list[str | None] = []
        seen: set[str] = set()
        candidates: list[str] = []
        for _row_number, value in rows:
            code = classify_row(value)
            number: str | None = None
            if code is WhitelistRowCode.IMPORTABLE:
                number = value
                if value in seen:
                    code = WhitelistRowCode.DUPLICATE_IN_FILE
                else:
                    seen.add(value)
                    candidates.append(value)
            elif value.isascii() and value.isdigit():
                # ASCII digits of the wrong length still carry their
                # canonical value on the decision.
                number = value
            codes.append(code)
            numbers.append(number)

        existing = await self._existing_numbers(db, candidates)
        kept = {number for number in candidates if number not in existing}
        decisions = [
            WhitelistRowDecision(
                row_number=row_number,
                code=(
                    WhitelistRowCode.DUPLICATE_IN_DB
                    if code is WhitelistRowCode.IMPORTABLE
                    and number is not None
                    and number not in kept
                    else code
                ),
                student_number=number,
            )
            for (row_number, _value), code, number in zip(
                rows, codes, numbers, strict=True
            )
        ]

        counts = self._count(decisions)
        importable = sorted(kept)
        token = import_digest(importable)
        logger.info(
            "whitelist import previewed actor_id=%s rows=%d importable=%d",
            actor.user_id,
            counts.total_rows,
            counts.importable,
        )
        return WhitelistImportPreview(
            total_rows=counts.total_rows,
            decisions=tuple(decisions),
            counts=counts,
            importable=tuple(importable),
            confirm_token=token,
        )

    # -- confirm ---------------------------------------------------------------

    async def confirm_whitelist_import(
        self,
        db: AsyncSession,
        actor: Actor,
        payload: WhitelistConfirmPayload,
        *,
        audit_context: AuditContext | None = None,
    ) -> WhitelistImportResult:
        """Insert exactly the previewed set, all-or-nothing (see the
        module docstring's conflict ruling).

        Replay check -> friendly DB pre-check -> one flush -> one commit;
        the UNIQUE constraint closes the pre-check race, and its
        ``IntegrityError`` becomes the same typed conflict list.
        """
        self._require_admin(actor)
        numbers = self._replay_classify(payload)

        existing = await self._existing_numbers(db, numbers)
        if existing:
            raise WhitelistImportConflictError(existing)

        now = self._clock.now()
        db.add_all(
            StudentWhitelist(
                student_number=number,
                enabled=payload.enable,
                disabled_at=None if payload.enable else now,
            )
            for number in numbers
        )
        try:
            await db.flush()
        except IntegrityError as exc:
            # Lost the UNIQUE race against a concurrent confirm: roll
            # back (nothing of ours landed), re-read which candidates
            # the winner committed, and refuse with the deterministic
            # per-number conflict list.
            await db.rollback()
            existing_now = await self._existing_numbers(db, numbers)
            raise WhitelistImportConflictError(existing_now) from exc

        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_WHITELIST_IMPORT_CONFIRMED,
            target_type=_AUDIT_TARGET_TYPE,
            # The batch's digest names the import (a business key, the
            # ledger source_* precedent); its rows are enumerable by it.
            target_id=payload.confirm_token,
            details={
                "created": len(numbers),
                "duplicated": 0,
                "enable": payload.enable,
            },
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info(
            "whitelist import confirmed actor_id=%s created=%d enable=%s",
            actor.user_id,
            len(numbers),
            payload.enable,
        )
        return WhitelistImportResult(created=len(numbers), enable=payload.enable)

    # -- enable/disable --------------------------------------------------------

    async def set_entries_enabled(
        self,
        db: AsyncSession,
        actor: Actor,
        student_numbers: Sequence[str],
        *,
        enabled: bool,
        reason: str | None = None,
        audit_context: AuditContext | None = None,
    ) -> WhitelistToggleResult:
        """Flip entries to ``enabled`` (single number or batch — one call).

        Entries already in the requested state are reported ``unchanged``
        and write nothing (idempotent replays audit nothing); unknown
        numbers refuse the whole call with the missing list. Each real
        flip appends its ``WHITELIST_ENTRY_TOGGLED`` audit row inside
        this transaction.
        """
        self._require_admin(actor)
        if not student_numbers:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "student_numbers", "reason": "must not be empty"},
            )
        requested = list(dict.fromkeys(student_numbers))
        entries = (
            await db.scalars(
                select(StudentWhitelist)
                .where(StudentWhitelist.student_number.in_(requested))
                .with_for_update()
            )
        ).all()
        by_number = {entry.student_number: entry for entry in entries}
        missing = [number for number in requested if number not in by_number]
        if missing:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _TOGGLE_MISSING_MESSAGE,
                status_code=400,
                details={"missing": missing},
            )

        now = self._clock.now()
        toggled: list[str] = []
        unchanged: list[str] = []
        for number in requested:
            entry = by_number[number]
            if entry.enabled == enabled:
                unchanged.append(number)
                continue
            entry.enabled = enabled
            entry.disabled_at = None if enabled else now
            toggled.append(number)
            await self._audit.append(
                db,
                actor=actor,
                action=AUDIT_WHITELIST_ENTRY_TOGGLED,
                target_type=_AUDIT_TARGET_TYPE,
                target_id=str(entry.id),
                reason=reason,
                before_snapshot={"enabled": not enabled},
                after_snapshot={"enabled": enabled},
                ip_address=audit_context.ip_address if audit_context else None,
                request_id=audit_context.request_id if audit_context else None,
            )
        await db.commit()
        logger.info(
            "whitelist entries toggled actor_id=%s enabled=%s toggled=%d unchanged=%d",
            actor.user_id,
            enabled,
            len(toggled),
            len(unchanged),
        )
        return WhitelistToggleResult(toggled=tuple(toggled), unchanged=tuple(unchanged))

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _require_admin(actor: Actor) -> None:
        if not rbac.is_admin(actor.role):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _PERMISSION_DENIED_MESSAGE,
                status_code=403,
            )

    def _decode_lines(self, data: bytes) -> list[str]:
        """Bounded strict-UTF-8 decode to physical lines; outer trim and
        blank-line skipping live in the classifier loop (module docstring)."""
        if len(data) > self._max_file_bytes:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={
                    "field": "file",
                    "reason": f"must be at most {self._max_file_bytes} bytes",
                },
            )
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "file", "reason": "must be valid UTF-8 text"},
            ) from exc
        lines = text.splitlines()
        if len(lines) > self._max_rows:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={
                    "field": "file",
                    "reason": f"must have at most {self._max_rows} rows",
                },
            )
        return lines

    def _replay_classify(self, payload: WhitelistConfirmPayload) -> list[str]:
        """Re-run the pure classification over the payload rows and bind
        it to the preview digest (the module docstring's replay check).

        DB membership is deliberately NOT part of the digest: a DB change
        between preview and confirm surfaces as the deterministic
        conflict list, not as a stale-token mismatch.
        """
        if not payload.student_numbers:
            raise InvalidWhitelistConfirmError("empty_student_numbers")
        seen: set[str] = set()
        for number in payload.student_numbers:
            try:
                validate_student_number(number)
            except ValueError as exc:
                raise InvalidWhitelistConfirmError("row_failed_validation") from exc
            if number in seen:
                raise InvalidWhitelistConfirmError("duplicate_in_payload")
            seen.add(number)
        if import_digest(seen) != payload.confirm_token:
            raise InvalidWhitelistConfirmError("digest_mismatch")
        return list(payload.student_numbers)

    @staticmethod
    async def _existing_numbers(
        db: AsyncSession, candidates: Sequence[str]
    ) -> set[str]:
        """Which candidates already occupy whitelist rows (one query)."""
        if not candidates:
            return set()
        rows = await db.scalars(
            select(StudentWhitelist.student_number).where(
                StudentWhitelist.student_number.in_(list(candidates))
            )
        )
        return set(rows.all())

    @staticmethod
    def _count(decisions: Sequence[WhitelistRowDecision]) -> WhitelistImportCounts:
        codes = [decision.code for decision in decisions]
        return WhitelistImportCounts(
            total_rows=len(codes),
            importable=codes.count(WhitelistRowCode.IMPORTABLE),
            duplicate_in_file=codes.count(WhitelistRowCode.DUPLICATE_IN_FILE),
            duplicate_in_db=codes.count(WhitelistRowCode.DUPLICATE_IN_DB),
            full_width_digits=codes.count(WhitelistRowCode.FULL_WIDTH_DIGITS),
            invalid_characters=codes.count(WhitelistRowCode.INVALID_CHARACTERS),
            invalid_length=codes.count(WhitelistRowCode.INVALID_LENGTH),
        )
