# backend/app/modules/community/report_service.py
"""Comment reports and the staff moderation queue (spec §23; plan 06
task 6).

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
  (gates.py).** Reporting is a community write: Student role and ACTIVE
  status on the users row, then the public-surface comment rule (exists
  -> not a tombstone -> PUBLISHED task). The same typed errors as
  comments, votes, and reactions — this service is WHY the gates were
  extracted (it would have been the third private copy).
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
- **No clock and no event seam.** ``created_at`` is a database ``now()``
  server default and the spec wires no audit stream to FILING a report
  (the vote/reaction ruling); task 8's handling flow owns the
  moderation transitions and their audit events.

Transaction shape per backend-engineering §5: the gate reads, the
duplicate read, the insert, and exactly one commit per file;
``list_task_reports`` is read-only.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, Uuid, column, func, select, table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.enums import ReportCategory
from app.modules.community.gates import (
    require_student_writer,
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
    "ReportNoteTooLongError",
    "ReportService",
    "ReportViewDeniedError",
    "normalize_report_note",
]

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


# --- the service ----------------------------------------------------------------------


class ReportService:
    """Comment reporting and the per-task moderation queue (spec §23):
    ``report_comment`` files, ``list_task_reports`` reviews.

    ``note_max_length`` is injectable for tests and deployments; the
    default is the documented service constant.
    ``moderation_key_secret`` is the settings-derived HMAC material
    behind the queue's pseudonymous moderation keys (see
    ``serializers.derive_moderation_key``); None resolves
    Settings.token_secret — the composition-root default. No clock, no
    events (see the module docstring — the task-8 reveal owns its own
    audit stream in ModerationService).
    """

    def __init__(
        self,
        *,
        note_max_length: int = DEFAULT_REPORT_NOTE_MAX_LENGTH,
        moderation_key_secret: str | None = None,
    ) -> None:
        self._note_max_length = note_max_length
        self._moderation_key_secret = (
            moderation_key_secret
            if moderation_key_secret is not None
            else get_settings().token_secret
        )

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
        await require_student_writer(db, user_id)
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
