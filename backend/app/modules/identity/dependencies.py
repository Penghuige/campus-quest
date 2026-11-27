# backend/app/modules/identity/dependencies.py
"""FastAPI request guards: authentication, account status, RBAC, and staff 2FA.

Invariants (spec §4, §5.6-§5.8, §33.4):
- Roles and account status are always read from the live ``users`` row (never
  trusted from JWT claims) alongside live ``user_sessions`` verification so
  revocation and demotion take effect immediately.
- Guard evaluation order is capability (``PERMISSION_DENIED``) -> account state
  (``ACCOUNT_NOT_ACTIVE``) -> staff 2FA (``TotpSetupRequiredError``).
- Ordinary community participation admits ACTIVE Student and Teacher only;
  Admin operates on dedicated governance surfaces.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rbac
from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import AccessTokenCodec, AccessTokenError
from app.db.session import get_db_session
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.repository import UserRepository
from app.modules.identity.staff_service import TotpSetupRequiredError

__all__ = [
    "Actor",
    "Role",
    "TotpSetupRequiredError",
    "get_access_session_id",
    "get_access_token_codec",
    "get_actor",
    "get_business_clock",
    "require_active_actor",
    "require_active_community_actor",
    "require_active_staff_actor",
    "require_active_student_actor",
    "require_active_student_or_staff_management_actor",
    "require_admin_actor",
    "require_active_except_pending_student",
    "require_browsing_student_actor",
    "require_approved_community_actor",
    "require_approved_student_actor",
    "require_staff_management_actor",
    "resolve_access_token",
]

_AUTHENTICATION_REQUIRED_MESSAGE = "未登录或登录状态已失效"
_ACCOUNT_NOT_ACTIVE_MESSAGE = "账号当前状态不允许执行该操作"
_MANAGEMENT_PERMISSION_MESSAGE = "仅教师或管理员可访问管理功能"
_STUDENT_ACTION_PERMISSION_MESSAGE = "仅学生账号可执行该操作"
_COMMUNITY_PARTICIPATION_PERMISSION_MESSAGE = "仅学生或教师账号可参与社区互动"
_ADMIN_ACTION_PERMISSION_MESSAGE = "只有管理员可以执行该操作"
_TOTP_SETUP_REQUIRED_MESSAGE = "必须先完成 TOTP 两步验证才能使用管理功能"
_APPROVAL_PENDING_MESSAGE = "账号待管理员审核，期间可浏览但不能执行该操作"

# auto_error=False: a missing or malformed Authorization header is OUR
# AUTHENTICATION_REQUIRED business error (401, §29 envelope), not
# Starlette's framework-shaped 403.
_bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def get_access_token_codec() -> AccessTokenCodec:
    """The process-wide access-token codec, built from settings.

    Cached like ``get_settings`` so request guards decode with exactly
    the secret and TTL the composition root minted tokens with.
    """
    settings = get_settings()
    return AccessTokenCodec(
        secret=settings.token_secret,
        ttl_minutes=settings.access_token_ttl_minutes,
    )


def get_business_clock() -> Clock:
    """Production business time for request guards (tests override this
    with a ``FrozenClock`` to make liveness comparisons deterministic)."""
    return SystemClock()


def _authentication_required() -> BusinessError:
    return BusinessError(
        ErrorCode.AUTHENTICATION_REQUIRED,
        _AUTHENTICATION_REQUIRED_MESSAGE,
        status_code=401,
    )


async def _resolve_user(
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
    codec: AccessTokenCodec,
    clock: Clock,
) -> User:
    """Authenticate the Bearer token; return the fresh user row.

    A missing header, an unverifiable or expired signature, and an
    unknown or dead session all fail as the same AUTHENTICATION_REQUIRED
    401 — the branch reason never leaves the server (the enumeration
    discipline of ``SessionService``; backend-engineering §15: the token
    itself never reaches an error message or log line).
    """
    if credentials is None:
        raise _authentication_required()
    return await resolve_access_token(db, credentials.credentials, codec, clock)


async def resolve_access_token(
    db: AsyncSession, token: str, codec: AccessTokenCodec, clock: Clock
) -> User:
    """Authenticate one raw access-token string; return the fresh user row.

    The Bearer-header guards delegate here; routes that carry the token
    in the request body (the email-code totp leg's ``pending_token``)
    call it directly with the same uniform-failure semantics.
    """
    try:
        claims = codec.decode(token)
    except AccessTokenError as exc:
        raise _authentication_required() from exc
    try:
        user_id = UUID(claims.sub)
        session_id = UUID(claims.sid)
    except ValueError as exc:
        # Unreachable in practice: the codec UUID-validates both claims.
        raise _authentication_required() from exc
    user = await UserRepository().find_with_live_session(
        db, user_id=user_id, session_id=session_id, now=clock.now()
    )
    if user is None:
        # Deleted user, killed session, borrowed claim pair, or expired
        # session row — one uniform answer.
        raise _authentication_required()
    return user


async def get_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """Resolve the authenticated ``Actor`` from the Bearer access token.

    Authentication only — status and 2FA gates are the other two guards
    below, so an authenticated-but-suspended or pending-setup account can
    still reach the endpoints that legitimately serve it (refresh, TOTP
    setup).
    """
    user = await _resolve_user(credentials, db, codec, clock)
    return Actor(user_id=user.id, role=Role(user.role))


async def require_active_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The guard for state-changing student operations (spec §5.7).

    Authentication (``get_actor``'s checks) plus the current row's
    ``status == ACTIVE``: SUSPENDED/BANNED/PENDING_PHONE accounts get
    ``ACCOUNT_NOT_ACTIVE`` (403) — a stable business code, never a
    generic 500 — resolved per request from the database, not from
    anything the client carries.
    """
    user = await _resolve_user(credentials, db, codec, clock)
    if user.status not in (
        UserStatus.ACTIVE,
        # PENDING_APPROVAL browses (V1.1 spec 2026-10-16 §2: login and
        # the reading surfaces are open; the acting surfaces use the
        # approved guards).
        UserStatus.PENDING_APPROVAL,
    ):
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    return Actor(user_id=user.id, role=Role(user.role))


async def require_active_student_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The guard for the Student claim lifecycle (spec §4.1: 领取/放弃/
    claim history are Student capabilities — Teacher/Admin never enter it).

    ``require_active_actor``'s checks plus ``role == STUDENT``, resolved
    per request from the user row (the stale-role defense: a staff
    account is a staff account no matter what any token snapshot says).
    A non-STUDENT role answers ``PERMISSION_DENIED`` (403) — a permission
    outcome, deliberately NOT ``ACCOUNT_NOT_ACTIVE``, and deliberately
    NOT a 2FA error: an invited Teacher holding a normal ACTIVE session
    before TOTP confirmation is still the wrong role for these routes,
    so the student guard never consults ``totp_credentials``. Check
    order: role, then status, mirroring the staff guard's
    capability-then-state precedence. The claim/abandon services re-check
    the same invariant under the user-row lock; this guard is the
    transport boundary, not the only line of defense.
    """
    user = await _resolve_user(credentials, db, codec, clock)
    if not rbac.is_student(user.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _STUDENT_ACTION_PERMISSION_MESSAGE,
            status_code=403,
        )
    if user.status != UserStatus.ACTIVE:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    return Actor(user_id=user.id, role=Role(user.role))


async def get_access_session_id(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
) -> UUID:
    """The current request's session id (the access token's ``sid`` claim).

    Companion to ``require_active_actor`` for the one use case that needs
    it: `change_password` keeps the authorizing session alive while every
    other device is signed out. Pure decode — no database round trip —
    because it is only ever mounted alongside an authentication guard that
    already proved the session live against the row; on its own it says
    nothing about liveness.
    """
    if credentials is None:
        raise _authentication_required()
    try:
        claims = codec.decode(credentials.credentials)
    except AccessTokenError as exc:
        raise _authentication_required() from exc
    return UUID(claims.sid)


async def require_active_staff_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The guard for staff-scoped but NON-management endpoints (TOTP setup).

    Staff role plus ``status == ACTIVE`` — the management guard's first two
    checks, WITHOUT the confirmed-TOTP gate, because these are the very
    endpoints that establish it (a TOTP requirement here would deadlock
    onboarding). A SUSPENDED staff member holding a still-live access token
    (revocation lags suspension) gets ``ACCOUNT_NOT_ACTIVE`` — no privilege
    was reachable through the management guard anyway, but the §5.7 state
    gate belongs on every staff action. Check order mirrors the management
    guard: role, then status.
    """
    user = await _resolve_user(credentials, db, codec, clock)
    if not rbac.is_staff(user.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _MANAGEMENT_PERMISSION_MESSAGE,
            status_code=403,
        )
    if user.status != UserStatus.ACTIVE:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    return Actor(user_id=user.id, role=Role(user.role))


async def require_approved_student_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The Student ACTING surface under the approval regime (V1.1 spec
    2026-10-16 §2): every require_active_student_actor check plus the
    PENDING_APPROVAL freeze — a pending student may log in and browse,
    but claim/submit/redeem/engage answers 403 APPROVAL_PENDING until an
    admin approves. Staff never carries the state, so nothing changes
    for them by construction."""

    async def _pending_gate(user: User) -> None:
        if (
            user.role == Role.STUDENT.value
            and user.status == UserStatus.PENDING_APPROVAL.value
        ):
            raise BusinessError(
                ErrorCode.APPROVAL_PENDING,
                _APPROVAL_PENDING_MESSAGE,
                status_code=403,
            )

    user = await _resolve_user(credentials, db, codec, clock)
    await _pending_gate(user)
    if not rbac.is_student(user.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _STUDENT_ACTION_PERMISSION_MESSAGE,
            status_code=403,
        )
    if user.status != UserStatus.ACTIVE.value:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    return Actor(user_id=user.id, role=Role(user.role))


async def require_approved_community_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The community WRITE surface under the approval regime: the
    require_active_community_actor checks plus the PENDING_APPROVAL
    freeze for the Student arm — a pending student reads the square but
    cannot comment/vote/react/rate/report; the Teacher arm is untouched
    (staff never carries the state)."""

    user = await _resolve_user(credentials, db, codec, clock)
    if (
        user.role == Role.STUDENT.value
        and user.status == UserStatus.PENDING_APPROVAL.value
    ):
        raise BusinessError(
            ErrorCode.APPROVAL_PENDING,
            _APPROVAL_PENDING_MESSAGE,
            status_code=403,
        )
    if not rbac.has_any_role(user.role, Role.STUDENT, Role.TEACHER):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _COMMUNITY_PARTICIPATION_PERMISSION_MESSAGE,
            status_code=403,
        )
    if user.status != UserStatus.ACTIVE.value:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    return Actor(user_id=user.id, role=Role(user.role))


async def require_browsing_student_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The student READING surfaces that stay role-gated (rankings,
    growth, notifications — spec §4.1): the student-role check of
    require_active_student_actor with PENDING_APPROVAL admitted (spec
    2026-10-16 §2 — a pending student browses the boards and the bell)."""

    user = await _resolve_user(credentials, db, codec, clock)
    if not rbac.is_student(user.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _STUDENT_ACTION_PERMISSION_MESSAGE,
            status_code=403,
        )
    if user.status not in (
        UserStatus.ACTIVE,
        UserStatus.PENDING_APPROVAL,
    ):
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    return Actor(user_id=user.id, role=Role(user.role))


async def require_active_except_pending_student(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """Any-ACTIVE-role gate with the PENDING_APPROVAL freeze: browsing
    semantics for every role (staff never carries the state), used where
    a surface serves all roles but a pending student must not write
    (avatar: the ADMIN arm the community variant would drop)."""

    user = await _resolve_user(credentials, db, codec, clock)
    if (
        user.role == Role.STUDENT.value
        and user.status == UserStatus.PENDING_APPROVAL.value
    ):
        raise BusinessError(
            ErrorCode.APPROVAL_PENDING,
            _APPROVAL_PENDING_MESSAGE,
            status_code=403,
        )
    if user.status != UserStatus.ACTIVE.value:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    return Actor(user_id=user.id, role=Role(user.role))


async def require_staff_management_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The guard for ALL management endpoints (spec §5.8 step 3, §33.4).

    Requires a staff role (TEACHER/ADMIN via the core predicates — the
    role predicates are pure functions, and identity OWNS wiring them to
    real actors), an ACTIVE account, and a CONFIRMED TOTP credential
    (``totp_credentials.confirmed_at IS NOT NULL``). Pending staff —
    setup not started or unconfirmed — gets ``TotpSetupRequiredError``,
    the distinct setup-forcing error, which the router maps to its
    documented §29 code. Routers mount this dependency; they never
    re-derive these rules (backend-engineering §3, §16).
    """
    user = await _resolve_user(credentials, db, codec, clock)
    if not rbac.is_staff(user.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _MANAGEMENT_PERMISSION_MESSAGE,
            status_code=403,
        )
    if user.status != UserStatus.ACTIVE:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    credential = await db.get(TotpCredential, user.id)
    if credential is None or credential.confirmed_at is None:
        raise TotpSetupRequiredError(_TOTP_SETUP_REQUIRED_MESSAGE)
    return Actor(user_id=user.id, role=Role(user.role))


async def require_admin_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The guard for Admin-only surfaces (PR #2 hardening ruling).

    ``require_staff_management_actor``'s structure with the role narrowed
    to ADMIN: same capability-then-state-then-second-factor check order,
    same typed errors. A TEACHER — even ACTIVE with a confirmed TOTP
    credential — answers ``PERMISSION_DENIED`` (403): the guarded
    surfaces are the ones the hardening ruling pulled out of the staff
    family (the notifications failure query), where any-active-staff
    power was judged too broad. The points redemption review decisions
    now ride scoped delegation instead (Plan 08 T4's
    ``require_reward_review_actor``); this guard keeps the remaining
    Admin-only face. An ACTIVE Admin without a confirmed TOTP
    credential still gets ``TotpSetupRequiredError``: the Admin role does
    not waive the §5.8 step-3 management 2FA gate these surfaces inherit.
    """
    user = await _resolve_user(credentials, db, codec, clock)
    if not rbac.is_admin(user.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _ADMIN_ACTION_PERMISSION_MESSAGE,
            status_code=403,
        )
    if user.status != UserStatus.ACTIVE:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    credential = await db.get(TotpCredential, user.id)
    if credential is None or credential.confirmed_at is None:
        raise TotpSetupRequiredError(_TOTP_SETUP_REQUIRED_MESSAGE)
    return Actor(user_id=user.id, role=Role(user.role))


async def require_active_community_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The guard for ordinary community participation (spec §4.1/§4.2).

    Spec §4.2 opens with "除普通社区能力外，可：" — Teacher holds the
    ordinary community capabilities (comments, replies, anonymity, votes,
    reactions, reports) listed for Student in §4.1, so the participant
    family is STUDENT + TEACHER, both ACTIVE. Admin is deliberately NOT
    a participant (the PR #2 hardening ruling): Admin's community powers
    are the governance surfaces (moderation queue, hard hide, identity
    reveal), which keep their own guards; keeping the actor out of the
    ordinary write paths preserves the participant/moderator separation
    the anonymity design leans on.

    No TOTP requirement — community participation is not management
    (§33.4's 2FA gate guards the management surfaces only), so an invited
    Teacher with a normal ACTIVE session participates like any Student.
    Role is read from the user row (the stale-role defense shared with
    every guard here); check order mirrors the student guard: role
    (``PERMISSION_DENIED``), then status (``ACCOUNT_NOT_ACTIVE``).
    """
    user = await _resolve_user(credentials, db, codec, clock)
    if not rbac.has_any_role(user.role, Role.STUDENT, Role.TEACHER):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _COMMUNITY_PARTICIPATION_PERMISSION_MESSAGE,
            status_code=403,
        )
    if user.status != UserStatus.ACTIVE:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    return Actor(user_id=user.id, role=Role(user.role))


async def require_active_student_or_staff_management_actor(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> Actor:
    """The guard for the one surface both populations legitimately
    shares: the submission download (spec §33.3, PR #4 hardening 5.2).

    The route serves the owning STUDENT and the reviewing staff (task
    owner teacher / ``REVIEW_SUBMISSIONS`` collaborator / Admin) through
    one path, so the arm is selected by role: the student arm is exactly
    ``require_active_student_actor``'s state gate — no TOTP, a Student
    never establishes one — while the staff arm adds the management
    gate's CONFIRMED-TOTP check (§33.4), so a pending-setup staff
    account cannot pull a presigned GET before the second factor is
    proven. The role enum is closed (STUDENT is the only non-staff
    member), so no ``PERMISSION_DENIED`` branch exists: every role has
    exactly one arm, and check order still follows the family — role
    selects the arm, then status (``ACCOUNT_NOT_ACTIVE``), then the
    staff-only 2FA gate (``TotpSetupRequiredError`` → 403
    ``TOTP_SETUP_REQUIRED``, rendered by the app-wide identity handler).

    Resource-level standing — whose submission, which task — is NOT this
    guard's question: the submissions query service judges it per
    submission after the port-boundary gate (backend-engineering §16).
    """
    user = await _resolve_user(credentials, db, codec, clock)
    staff = rbac.is_staff(user.role)
    if user.status != UserStatus.ACTIVE:
        raise BusinessError(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
        )
    if staff:
        credential = await db.get(TotpCredential, user.id)
        if credential is None or credential.confirmed_at is None:
            raise TotpSetupRequiredError(_TOTP_SETUP_REQUIRED_MESSAGE)
    return Actor(user_id=user.id, role=Role(user.role))
