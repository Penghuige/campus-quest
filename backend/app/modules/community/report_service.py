# backend/app/modules/community/report_service.py
"""Comment reports and the staff moderation queue (spec §23; plan 06
task 6; the OPEN -> HANDLED/DISMISSED closure is PR #2 hardening step
10).

Design decisions:

- **Non-destructive by construction (spec §23 举报不自动删除评论 /
  进入治理队列).** ``report_comment`` writes exactly one row — its own
  — and never touches the Comment: no soft-delete trio, no hard-hide
  flag, no revision. Removal is a moderator decision on a separate,
  reason-mandatory, audited path (comment_service's moderate-delete and
  hard hide); the report only ENTERS the queue. The comment keeps
  rendering in the public list until a moderator acts.
- **Closed category set, checked FIRST (spec §23 类别).** SPAM /
  HARASSMENT / PRIVACY / OTHER, membership by exact string through the
  ``ReportCategory`` enum (lowercase is not a member). The check is a
  pure function raised before any database touch — the reaction
  whitelist-first precedent — so an invalid category never reveals
  whether the user or comment exists.
- **Note rules.** Optional free text for the moderator: trimmed, blank
  (or absent) stored as None, capped in code points on the TRIMMED text
  with the boundary inclusive. The 500 default is a service constant,
  not a spec number — notes are moderation annotations, not public
  thread content — and it is constructor-injectable
  (``comment_max_length`` seam) so deployments tune it without code
  changes. No control-character stripping: like moderation reasons,
  notes are staff-facing audit material, and §21.1's public-content
  rules do not reach them.
- **Duplicate = idempotent return (spec §23 同一用户对同一评论同一类别
  SHOULD 防止重复刷举报).** Re-reporting the same (comment, reporter,
  category) returns the EXISTING row — same id, original ``created_at``,
  FIRST-REPORT-WINS note (the second note is discarded, not merged) —
  and never raises: the reporter already did their civic duty, and the
  SHOULD is a dedup rule, not a punishment. The database-side anchor is
  the plain UNIQUE(comment_id, reporter_user_id, category) (models.py
  documents why full-UNIQUE rather than a terminal-status partial
  index): two concurrent first-reports both read absent and both INSERT;
  the loser's flush surfaces ``IntegrityError``, the transaction rolls
  back, and ONE re-read returns the winner's row — any IntegrityError
  whose re-read finds nothing (a foreign constraint, not this race)
  re-raises untouched. A different category, or a different reporter,
  files its own row — the anchor is per triple.
- **Writer and visibility gates: the shared community gates
  (gates.py).** Reporting is a community write: the spec §4.2
  participant family (Student or Teacher role) and ACTIVE status on
  the users row, then the public-surface comment rule (exists -> not a
  tombstone -> PUBLISHED task). The same typed errors as comments,
  votes, and reactions — this service is WHY the gates were extracted
  (it would have been the third private copy).
- **Moderation listing: owner / MODERATE_COMMUNITY / Admin
  (spec §4.2, §23).** ``list_task_reports`` reads the queue for ONE
  task. Admin is admitted BY DESIGN, unlike comment_service's
  moderate-DELETE (which reserves Admin's community power to the audited
  hard hide): reading the queue hides nothing and duplicates nothing —
  the power separation that matters is removal vs review, and review is
  the operator's. The standing check is judged on the Actor's
  server-resolved role (the moderation precedent; ``Actor.role`` is
  never client-supplied), owner match and collaborator capability on
  the rows. Existence answers before standing (the moderate-delete
  order): an unknown task is the shared ``TaskNotFoundError`` (404) for
  anyone. Deliberately NOT gated on PUBLISHED — governance reaches
  paused/closed history exactly like every moderation path.
- **Closure: the audited moderation endpoints (PR #2 hardening step
  10; spec §23's queue is the ruling's anchor, the interfaces.md
  report-closure ruling is its ownership).** ``dismiss_report`` takes
  an OPEN report to DISMISSED with a MANDATORY reason — a governance
  decision needs its context; blank-after-trim is the typed rejection
  and there is NO length cap (the ``_require_moderation_reason``
  precedent: moderator input is audit material, not public thread
  content). ``handle_report`` takes it to HANDLED with an OPTIONAL
  note (the filing-note normalization and cap). "Handled" registers
  the CONCLUSION only: the moderator acts on the reported comment
  through the existing separately-audited paths (comment_service's
  moderate-delete, the Admin hard hide) or otherwise, and a closure
  never touches the Comment row — §23 不自动删除评论 reaches closure
  too, and no new report rule is invented (G13: no auto-punishment,
  no notification, no reporter feedback). Standing is the queue
  listing's exactly — ``require_task_moderation_site`` with
  ``admit_admin=True`` (owner / MODERATE_COMMUNITY / Admin) — with
  its order: task existence answers first (unknown task -> the
  shared 404), then standing, then the report read joined to the
  task's comments, so an unknown report id AND a report on ANOTHER
  task's comment are the same typed 404. ``handled_by`` is the
  acting moderator; ``handled_at`` is the database ``now()`` — the
  same transaction clock PostgreSQL stamps on the audit row's
  ``created_at`` (no injected Clock needed). A replay onto the SAME
  terminal state is the idempotent return of the existing row:
  nothing is rewritten and NO second audit row lands (the
  redemption-replay ruling — audit records decisions, not requests);
  a replay onto the OTHER terminal state is the typed 409
  (VALIDATION_ERROR, the ``RedemptionNotReviewableError``
  precedent).
- **Closure serialization (PR #2 hardening pass 5c): the closure
  gate's report read takes ``FOR UPDATE OF comment_reports``** — the
  task-scoping JOIN stays lock-free — so two moderators racing
  DISMISSED vs HANDLED from the same OPEN row serialize on the row
  itself: the second transaction's locked read wakes on the WINNER's
  committed terminal state (READ COMMITTED re-evaluates the locked
  row) and answers the replay rulings above — same-state idempotent
  return, other-state typed 409 — so exactly one transition and
  exactly one decision audit row ever land. The state machine is
  judged on the LOCKED row's database values (``populate_existing``:
  a session whose identity map still holds a pre-race OPEN snapshot
  from an earlier listing cannot act on the stale copy). FOR UPDATE
  was chosen over the CAS/RETURNING alternative because it keeps the
  module's §5 shape — the gate read becomes a locking read, and
  everything downstream (one flush, one audit row, one commit) is
  unchanged — matching the repo's lock discipline
  (upload_service's intent-row FOR UPDATE state machine); the
  conditional-UPDATE claim primitive belongs to cleanup/claim, not
  here. Both closures append exactly one durable
  ``REPORT_DISMISSED`` / ``REPORT_HANDLED`` row into ``audit_logs``
  in the SAME transaction through the flush-only ``AuditLogWriter``
  (G12): actor, the ``comment_report`` target, the dismissal reason
  (the writer's ``reason`` field, the redemption-reject/reveal
  precedent) or the handling note (``details["note"]``, the
  redemption-approve precedent), with the task/comment/reporter
  context riding ``details``. No DomainEvent stream of their own:
  §25 wires no notification to report closure and G13 invents none.
- **Reporter identity: moderator-only (spec §23 被举报用户不可看到举报者
  身份).** The queue view (``CommentReportView``) carries
  ``reporter_user_id`` + ``reporter_nickname`` — moderators act on
  reports and must see who filed them — and is the ONLY shape in the
  module where reporter identity exists at all; students are a typed
  PERMISSION_DENIED away from it. ``report_comment`` returns the ORM
  row to the REPORTER themself: the caller's own identity is not a
  leak, and the row carries no comment-author identity to leak either.
  The public comment surface (``CommentPublic``) has no report fields
  by construction. **Self-report fold (task 6 review F2):** when
  ``reporter_user_id`` equals the comment's ``user_id``, BOTH reporter
  fields render None — the anonymous author reporting their own comment
  must not have their nickname surface beside it. The comment context
  itself is the task-8 Teacher-safe record (serializers):
  匿名用户 + the pseudonymous ``moderation_key`` for anonymous
  comments, the nickname for named ones, never the raw author id.
- **Pagination.** Newest first (``created_at`` DESC, id tie-break), one
  limit/offset page plus the rendered total — the query_service
  pattern; ``limit``/``offset`` arrive already bounded (the route owns
  the caps, task 9). Reports on since-deleted or hard-hidden comments
  STAY listed with the removal flagged in the comment context: the
  queue reviews history, and a moderator must still dismiss or act on
  a report whose target was removed meanwhile.
- **No clock and no event seam on FILING.** ``created_at`` is a
  database ``now()`` server default and the spec wires no audit
  stream to FILING a report (the vote/reaction ruling). The closure
  methods above own the moderation transitions and their audit rows
  — written directly in the closing transaction through the shared
  ``AuditLogWriter`` (see the closure decision above); the timestamp
  both share is the database's, so no Clock is injected anywhere in
  this service.

Transaction shape per backend-engineering §5: the gate reads, the
duplicate read, the insert, and exactly one commit per file; the
closure's report read is a LOCKING read (``FOR UPDATE OF
comment_reports`` — see the closure-serialization decision above) so
the terminal-state arbitration happens on the row the transaction
holds until its commit; ``list_task_reports`` is read-only.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, Uuid, column, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.community.enums import ReportCategory, ReportStatus
from app.modules.community.gates import (
    require_community_writer,
    require_task_moderation_site,
    require_visible_comment,
)
from app.modules.community.models import Comment, CommentReport, CommentRevision
from app.modules.community.schemas import CommentReportView
from app.modules.community.serializers import serialize_moderation_comment
from app.modules.identity.events import Actor

__all__ = [
    "DEFAULT_REPORT_NOTE_MAX_LENGTH",
    "InvalidReportCategoryError",
    "REPORT_DISMISSED",
    "REPORT_HANDLED",
    "ReportAlreadyClosedError",
    "ReportClosureDeniedError",
    "ReportDismissReasonRequiredError",
    "ReportNotFoundError",
    "ReportNoteTooLongError",
    "ReportService",
    "ReportViewDeniedError",
    "normalize_dismiss_reason",
    "normalize_report_note",
]

# Audit action names (the REDEMPTION_APPROVE / COMMUNITY_IDENTITY_REVEAL
# family): the durable audit_logs rows' actions for the two report
# closures (G12; PR #2 hardening step 10). These are AUDIT identifiers —
# no DomainEvent stream exists for report closure (see the module
# docstring).
REPORT_DISMISSED = "REPORT_DISMISSED"
REPORT_HANDLED = "REPORT_HANDLED"

# The audit target vocabulary for the closures: the report row the
# moderator decided on (target_id = the report's UUID as text).
_AUDIT_TARGET_TYPE = "comment_report"

# The moderation-annotation cap (see module docstring): not a spec
# number, constructor-injectable, defaulting to a bounded note size.
DEFAULT_REPORT_NOTE_MAX_LENGTH = 500

# Identity seam (the T2 precedent): a typed Core-level light users
# table, NOT the identity ORM model — nickname for the reporter display
# on the moderation surface.
_USERS = table(
    "users",
    column("id", Uuid),
    column("nickname", String),
)

# --- messages (§29 envelope text) ---------------------------------------------------

_INVALID_CATEGORY_MESSAGE = "举报类别无效"
_NOTE_INVALID_MESSAGE = "举报备注必须是文本"
_NOTE_TOO_LONG_MESSAGE = "举报备注超过长度上限"
_VIEW_DENIED_MESSAGE = "只有任务所有者、拥有社区治理权限的协作者或管理员可以查看举报"
_CLOSURE_DENIED_MESSAGE = "只有任务所有者、拥有社区治理权限的协作者或管理员可以处理举报"
_DISMISS_REASON_REQUIRED_MESSAGE = "必须填写驳回举报的理由"
_REPORT_NOT_FOUND_MESSAGE = "举报不存在"
_REPORT_ALREADY_CLOSED_MESSAGE = "举报已按另一结论处理，不能再次变更"


# --- typed exceptions (router-mapped) ------------------------------------------------


class InvalidReportCategoryError(BusinessError):
    """``category`` outside the closed spec §23 set — including the
    lowercase spellings and non-string input (the
    ``InvalidVoteValueError`` / ``UnknownEmojiError`` family)."""

    def __init__(self, category: object) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _INVALID_CATEGORY_MESSAGE,
            status_code=400,
            details={"category": str(category)},
        )


class ReportNoteTooLongError(BusinessError):
    """The trimmed note exceeds the cap (see module docstring: a service
    constant, not a spec number)."""

    def __init__(self, max_length: int) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _NOTE_TOO_LONG_MESSAGE,
            status_code=400,
            details={"max_length": max_length},
        )


class ReportViewDeniedError(BusinessError):
    """The actor is not the task's owner Teacher, not a collaborator
    holding MODERATE_COMMUNITY, and not Admin (spec §4.2/§23) — the
    typed wall between reporter identity and the reported user."""

    def __init__(self, task_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _VIEW_DENIED_MESSAGE,
            status_code=403,
            details={"task_id": str(task_id)},
        )


class ReportClosureDeniedError(BusinessError):
    """The actor is not the task's owner Teacher, not a collaborator
    holding MODERATE_COMMUNITY, and not Admin on a CLOSURE path — the
    same standing as ``ReportViewDeniedError`` (the queue listing),
    its write-side twin: review and decision are one governance
    surface (spec §4.2/§23; the interfaces.md closure ruling)."""

    def __init__(self, task_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _CLOSURE_DENIED_MESSAGE,
            status_code=403,
            details={"task_id": str(task_id)},
        )


class ReportDismissReasonRequiredError(BusinessError):
    """``dismiss_report`` arrived without a usable reason — None, a
    non-string, or blank after trimming. A dismissal is a governance
    decision and carries its context (the
    ``ModerationReasonRequiredError`` / reveal-reason precedent)."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _DISMISS_REASON_REQUIRED_MESSAGE,
            status_code=400,
        )


class ReportNotFoundError(BusinessError):
    """``report_id`` matches no report on the requested task's comments
    — an unknown report id and a report belonging to ANOTHER task are
    the same answer (the task-scoped 404; existence rules of the
    moderation listing hold on the write side too)."""

    def __init__(self, task_id: UUID, report_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _REPORT_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"task_id": str(task_id), "report_id": str(report_id)},
        )


class ReportAlreadyClosedError(BusinessError):
    """The report already holds the OTHER terminal status — the queue
    leaves each report exactly once (enums.py's status-set decision),
    so a HANDLED report cannot be dismissed nor a DISMISSED one
    handled. The same-state replay never reaches here (idempotent
    return). VALIDATION_ERROR with HTTP 409, the
    ``RedemptionNotReviewableError`` precedent for a terminal-state
    conflict."""

    def __init__(self, report_id: UUID, status: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _REPORT_ALREADY_CLOSED_MESSAGE,
            status_code=409,
            details={"report_id": str(report_id), "status": status},
        )


# --- input normalization --------------------------------------------------------------


def _require_category(category: object) -> str:
    """Exact-string membership in the closed ``ReportCategory`` set;
    anything else is the typed rejection. Pure — the check runs before
    any database touch."""
    if not isinstance(category, str):
        raise InvalidReportCategoryError(category)
    try:
        return ReportCategory(category).value
    except ValueError:
        raise InvalidReportCategoryError(category) from None


def normalize_report_note(note: str | None, max_length: int) -> str | None:
    """Trim the note; blank (or absent) is None — a blank note is no
    note. Over-cap is the typed rejection, measured on the TRIMMED text
    in code points, boundary inclusive (the comment-content precedent).
    Pure on purpose: unit-testable without a database."""
    if note is None:
        return None
    if not isinstance(note, str):
        # The comment-content precedent: a non-string body is the bare
        # VALIDATION_ERROR, not one of the typed note rulings.
        raise BusinessError(ErrorCode.VALIDATION_ERROR, _NOTE_INVALID_MESSAGE, 400)
    normalized = note.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ReportNoteTooLongError(max_length)
    return normalized


def normalize_dismiss_reason(reason: str) -> str:
    """Trim the dismissal reason; missing, non-string, or
    blank-after-trim is the typed rejection. No length cap (the
    ``_require_moderation_reason`` precedent: moderator input is audit
    material, not public thread content). Pure on purpose: the check
    runs before any database touch (the category-first precedent), so
    a reasonless dismissal never reveals whether the task, the report,
    or the actor's standing exists."""
    if not isinstance(reason, str) or not reason.strip():
        raise ReportDismissReasonRequiredError()
    return reason.strip()


# --- the service ----------------------------------------------------------------------


class ReportService:
    """Comment reporting and the per-task moderation queue (spec §23):
    ``report_comment`` files, ``list_task_reports`` reviews, and
    ``dismiss_report``/``handle_report`` close (PR #2 hardening step
    10 — the audited OPEN -> DISMISSED/HANDLED transitions).

    ``note_max_length`` is injectable for tests and deployments; the
    default is the documented service constant.
    ``moderation_key_secret`` is the settings-derived HMAC material
    behind the queue's pseudonymous moderation keys (see
    ``serializers.derive_moderation_key``); None resolves
    Settings.token_secret — the composition-root default. ``audit``
    is the durable audit_logs seam for the closures — stateless and
    flush-only, defaulting to a fresh ``AuditLogWriter`` so no wiring
    slip can silently drop the G12 trace (the ModerationService
    pattern). Filing stays clock- and event-free (see the module
    docstring); the closures need no Clock either — the database
    ``now()`` is the closure timestamp.
    """

    def __init__(
        self,
        *,
        note_max_length: int = DEFAULT_REPORT_NOTE_MAX_LENGTH,
        moderation_key_secret: str | None = None,
        audit: AuditLogWriter | None = None,
    ) -> None:
        self._note_max_length = note_max_length
        self._moderation_key_secret = (
            moderation_key_secret
            if moderation_key_secret is not None
            else get_settings().token_secret
        )
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()

    async def report_comment(
        self,
        db: AsyncSession,
        user_id: UUID,
        comment_id: UUID,
        category: str,
        note: str | None = None,
    ) -> CommentReport:
        """File one report into the moderation queue (spec §23) and
        return the row — the caller's OWN report (see the module
        docstring for why returning the ORM row is the privacy-safe
        choice here). Duplicate (comment, reporter, category) is the
        idempotent return of the existing row; the reported comment is
        never deleted, hidden, or edited."""
        category_value = _require_category(category)
        stored_note = normalize_report_note(note, self._note_max_length)
        await require_community_writer(db, user_id)
        await require_visible_comment(db, comment_id)

        existing = await self._find(db, comment_id, user_id, category_value)
        if existing is not None:
            return existing

        report = CommentReport(
            comment_id=comment_id,
            reporter_user_id=user_id,
            category=category_value,
            note=stored_note,
        )
        try:
            db.add(report)
            await db.flush()
            await db.commit()
        except IntegrityError:
            # The both-create race lost to the UNIQUE triple: the winner
            # committed the identical report, so this caller's filing is
            # already satisfied — return it. A re-read that finds nothing
            # means the violation was some other constraint: re-raise.
            await db.rollback()
            winner = await self._find(db, comment_id, user_id, category_value)
            if winner is None:
                raise
            return winner
        await db.refresh(report)  # load the now()/OPEN server defaults
        return report

    async def list_task_reports(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[CommentReportView], int]:
        """One offset page of the reports on ONE task's comments, newest
        first, with the rendered total — the staff moderation surface
        (owner / MODERATE_COMMUNITY / Admin; see the module docstring
        for the Admin admission ruling and the reporter-identity
        contract). ``limit``/``offset`` arrive already bounded (the
        route owns the caps, task 9)."""
        await self._require_report_viewer(db, actor, task_id)

        total = int(
            await db.scalar(
                select(func.count())
                .select_from(CommentReport)
                .join(Comment, Comment.id == CommentReport.comment_id)
                .where(Comment.task_id == task_id)
            )
            or 0
        )
        if total == 0 or offset >= total:
            return [], total

        edited_flag = (
            select(CommentRevision.comment_id)
            .where(CommentRevision.comment_id == Comment.id)
            .exists()
        )
        # The author's users row joins under an alias: the nickname is
        # the Teacher-safe display for named comments, while the key
        # derives from the (task, author) pair server-side.
        author_users = _USERS.alias("moderation_author_users")
        rows = (
            await db.execute(
                select(
                    CommentReport,
                    Comment,
                    _USERS.c.nickname,
                    author_users.c.nickname,
                    edited_flag,
                )
                .select_from(CommentReport)
                .join(Comment, Comment.id == CommentReport.comment_id)
                .join(_USERS, _USERS.c.id == CommentReport.reporter_user_id)
                .join(author_users, author_users.c.id == Comment.user_id)
                .where(Comment.task_id == task_id)
                .order_by(CommentReport.created_at.desc(), CommentReport.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return [
            CommentReportView(
                id=report.id,
                comment=serialize_moderation_comment(
                    comment,
                    author_nickname=str(author_nickname),
                    key_secret=self._moderation_key_secret,
                    edited=edited,
                ),
                category=report.category,
                note=report.note,
                status=report.status,
                # Self-report fold (task 6 review F2): the reporter IS
                # the comment's author -> no reporter identity renders;
                # their nickname beside an anonymous comment would
                # deanonymize it. Otherwise the reporter fields are the
                # moderators' material (row unpacks are Any; the column
                # is NOT NULL VARCHAR).
                reporter_user_id=(
                    None
                    if report.reporter_user_id == comment.user_id
                    else report.reporter_user_id
                ),
                reporter_nickname=(
                    None
                    if report.reporter_user_id == comment.user_id
                    else str(nickname)
                ),
                created_at=report.created_at,
                handled_by=report.handled_by,
                handled_at=report.handled_at,
            )
            for report, comment, nickname, author_nickname, edited in rows
        ], total

    async def dismiss_report(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        report_id: UUID,
        reason: str,
        *,
        audit_context: AuditContext | None = None,
    ) -> CommentReport:
        """Take one OPEN report of this task's comments to DISMISSED
        (PR #2 hardening step 10): the moderator judged that no action
        is warranted, and the mandatory reason records why. Returns the
        closed row (or the existing row unchanged on a same-state
        replay). Never touches the comment — dismissal removes the
        QUEUE entry, not the comment."""
        normalized_reason = normalize_dismiss_reason(reason)
        report = await self._require_closable(db, actor, task_id, report_id)
        if report.status == ReportStatus.DISMISSED.value:
            return report  # same-state replay: nothing to do, nothing to audit
        await self._close(
            db,
            actor,
            task_id,
            report,
            ReportStatus.DISMISSED,
            audit_reason=normalized_reason,
            audit_note=None,
            audit_context=audit_context,
        )
        return report

    async def handle_report(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        report_id: UUID,
        note: str | None = None,
        *,
        audit_context: AuditContext | None = None,
    ) -> CommentReport:
        """Take one OPEN report of this task's comments to HANDLED
        (PR #2 hardening step 10): the moderator has acted on the
        reported comment — through the existing audited paths
        (moderate-delete, hard hide) or otherwise — and this call
        registers the conclusion on the QUEUE row; the comment itself
        is never touched here. The note is optional governor context
        (the filing-note normalization and cap). Returns the closed
        row (or the existing row unchanged on a same-state replay)."""
        stored_note = normalize_report_note(note, self._note_max_length)
        report = await self._require_closable(db, actor, task_id, report_id)
        if report.status == ReportStatus.HANDLED.value:
            return report  # same-state replay: nothing to do, nothing to audit
        await self._close(
            db,
            actor,
            task_id,
            report,
            ReportStatus.HANDLED,
            audit_reason=None,
            audit_note=stored_note,
            audit_context=audit_context,
        )
        return report

    # -- internals ----------------------------------------------------------------

    @staticmethod
    async def _find(
        db: AsyncSession, comment_id: UUID, reporter_user_id: UUID, category: str
    ) -> CommentReport | None:
        """The report for one (comment, reporter, category) triple, or
        None — the idempotency read and the post-race re-read."""
        found = await db.scalar(
            select(CommentReport).where(
                CommentReport.comment_id == comment_id,
                CommentReport.reporter_user_id == reporter_user_id,
                CommentReport.category == category,
            )
        )
        return found if found is None or isinstance(found, CommentReport) else None

    @staticmethod
    async def _require_report_viewer(
        db: AsyncSession, actor: Actor, task_id: UUID
    ) -> None:
        """Spec §4.2/§23: the task's owner Teacher, a collaborator
        holding MODERATE_COMMUNITY, or Admin (the read-surface policy:
        ``admit_admin=True`` — review hides nothing, unlike
        comment_service's moderate-DELETE). Existence answers first
        (unknown task -> the shared 404); standing is judged on the
        Actor's server-resolved role; NOT gated on task visibility —
        governance reaches paused/closed history too. The predicate
        lives in ``gates.require_task_moderation_site`` since the final
        review (fix I1)."""
        await require_task_moderation_site(
            db,
            actor,
            task_id,
            admit_admin=True,
            error_factory=ReportViewDeniedError,
        )

    @staticmethod
    async def _require_closable(
        db: AsyncSession, actor: Actor, task_id: UUID, report_id: UUID
    ) -> CommentReport:
        """The closure gate (see the module docstring): the queue
        listing's standing and check order — task existence answers
        first (the shared 404), then the owner/MODERATE_COMMUNITY/Admin
        standing (the write twin of ``ReportViewDeniedError``), then
        the report read JOINED to this task's comments so an unknown
        id and another task's report are the same typed 404. NOT
        gated on PUBLISHED or on the comment's liveness — the queue
        reviews history, and a report on a since-deleted comment must
        still be dismissable (the listing ruling). Returns the row
        for the state machine; terminal-state arbitration is the
        caller's.

        The read is a LOCKING read — ``FOR UPDATE`` scoped to the
        comment_reports row only (the task-scoping JOIN stays
        lock-free, so no lock edge against the comment surfaces
        exists to deadlock on) and the ``populate_existing`` execution
        option so the returned ORM object carries the LOCKED row's
        committed values, never a stale identity-map snapshot: the
        closure race (DISMISSED vs HANDLED from one OPEN row) is
        serialized here, with the loser waking on the winner's
        committed terminal state (see the module docstring's
        closure-serialization decision)."""
        await require_task_moderation_site(
            db,
            actor,
            task_id,
            admit_admin=True,
            error_factory=ReportClosureDeniedError,
        )
        found = await db.scalar(
            select(CommentReport)
            .join(Comment, Comment.id == CommentReport.comment_id)
            .where(CommentReport.id == report_id, Comment.task_id == task_id)
            .with_for_update(of=CommentReport)
            .execution_options(populate_existing=True)
        )
        if found is None or not isinstance(found, CommentReport):
            raise ReportNotFoundError(task_id, report_id)
        return found

    async def _close(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        report: CommentReport,
        target: ReportStatus,
        *,
        audit_reason: str | None,
        audit_note: str | None,
        audit_context: AuditContext | None = None,
    ) -> None:
        """One OPEN -> terminal transition and its audit row, one
        transaction (backend-engineering §5): stamp the trio, append
        the flush-only audit row, commit, refresh. The guard judges
        the row ``_require_closable`` returned LOCKED (its values are
        the database's, not a snapshot's), and the lock holds through
        this commit — so between the guard and the commit no other
        closure can touch the row: the both-closed race is impossible,
        the loser answered at the locked read. ``handled_at`` is
        the database ``now()`` expression — the same transaction clock
        PostgreSQL stamps on the audit row's ``created_at`` (``now()``
        is the transaction timestamp, so the two are identical). The
        same-state replay never reaches here (the idempotent returns
        in the public methods), so exactly one audit row exists per
        DECISION, none per request (the redemption ruling). The §30
        snapshot pair (0016) carries the status migration."""
        if report.status != ReportStatus.OPEN.value:
            raise ReportAlreadyClosedError(report.id, report.status)

        report.status = target.value
        report.handled_by = actor.user_id
        report.handled_at = func.now()  # the database's transaction clock
        await db.flush()

        details: dict[str, str] = {
            "task_id": str(task_id),
            "comment_id": str(report.comment_id),
            "reporter_user_id": str(report.reporter_user_id),
        }
        if audit_note is not None:
            details["note"] = audit_note
        await self._audit.append(
            db,
            actor=actor,
            action=(
                REPORT_DISMISSED if target is ReportStatus.DISMISSED else REPORT_HANDLED
            ),
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(report.id),
            reason=audit_reason,
            before_snapshot={"status": ReportStatus.OPEN.value},
            after_snapshot={"status": target.value},
            details=details,
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        await db.refresh(report)  # load the now() stamp and updated_at
