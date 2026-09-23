# backend/tests/e2e/browser_world.py
"""The browser-suite world builder (plan 10 task 2, step 2/3).

The Playwright specs drive REAL pages against the REAL orchestrated
servers (playwright.config.ts webServer: uvicorn + next dev), so their
world — accounts, published tasks with pre-claimed submittable claims,
an open task to claim through the UI, a stocked reward, spendable
points, projected boards — is seeded through the SAME e2e factories the
pytest modules use, then cleaned the same way. This script is the
process bridge: Node shells out to it (global-setup/teardown and the
per-action calls), Python owns the database.

Subcommands (JSON on stdout; non-zero exit with a message on stderr):

    seed                     build the world, print its contract
    validate <submission_id> run the REAL validation job entry
    rank <user_id> [iso]     run the REAL ranking-projection job entry
    otp-code <phone>         recover the current OTP code (otp_probe)
    mint <user_id>           a fresh access token (the 15-min TTL makes
                             seed-time tokens unreliable for long runs)
    clean <world_file>       teardown: S3 objects, rows, boards, broker

Env: the integration defaults (db_guard) apply, so the plain
``uv run python tests/e2e/browser_world.py ...`` from backend/ targets
the local test stack; real env vars win — same contract as pytest.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

_E2E_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _E2E_DIR.parent.parent
_INTEGRATION_DIR = _E2E_DIR.parent / "integration"
for _path in (_BACKEND_DIR, _INTEGRATION_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from db_guard import apply_integration_env_defaults  # noqa: E402

apply_integration_env_defaults()

import redis.asyncio as aioredis  # noqa: E402
from sqlalchemy import delete, func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.modules.identity.models import (  # noqa: E402
    TotpCredential as TotpCredentialRow,
)
from tests.e2e.factories import (  # noqa: E402
    DEFAULT_PASSWORD,
    clean_world,
    seed_admin_confirmed_totp,
    seed_claim,
    seed_points_balance,
    seed_reward_item,
    seed_student,
    seed_task_with_assignments,
    seed_teacher_confirmed_totp,
    snapshot_honor_ids,
)

_BROKER_QUEUE_KEY = "celery"
_REWARD_POINTS = 100

_GOOD_CSV = (
    b"url,title\n"
    b"https://example.com/note/1,\xe7\xac\xac\xe4\xb8\x80\xe6\x9d\xa1\n"
    b"https://example.com/note/2,\xe7\xac\xac\xe4\xba\x8c\xe6\x9d\xa1\n"
)

#: Header mismatch (missing the required url column): a structural
#: failure the machine gate reports with findings — drives the legacy
#: failed-validation spec's report/retry assertions.
_BAD_CSV = b"platform,date\nxiaohongshu,2026-09-21\n"

#: The §12.4 report shape the validation worker persists for
#: `_GOOD_CSV` (the 13-key `report_to_json` contract the review-queue
#: DTO validates against) — the seeded review target carries it so the
#: queue row's validation badge reads like a real machine verdict.
_GOOD_CSV_REPORT: dict[str, Any] = {
    "parser_version": "csv-2",
    "file_type": "CSV",
    "row_count": 2,
    "detected_columns": ["url", "title"],
    "missing_required_columns": [],
    "extra_columns": [],
    "type_error_counts": {},
    "null_ratios": {"url": 0.0, "title": 0.0},
    "duplicate_counts": {},
    "warnings": [],
    "errors": [],
    "duration_ms": 3.0,
    "preview_rows": [
        ["https://example.com/note/1", "第一条"],
        ["https://example.com/note/2", "第二条"],
    ],
}


def _factory() -> async_sessionmaker[AsyncSession]:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _dispose(factory: async_sessionmaker[AsyncSession]) -> None:
    await factory.kw["bind"].dispose()


def _registered_number(run: str) -> str:
    """The run-unique student number the auth spec registers through the
    REAL flow (6-20 ASCII digits; prefix "3" keeps it clear of the
    factories' "2025..." namespace and of the whitelist sweep)."""
    return f"3{int(run, 16) % 10**15}"


def _answerable_staff_row(username: str, nickname: str, role: Any) -> tuple[Any, str]:
    """One ACTIVE staff account (NOT yet session-bound) plus the genuine
    base32 TOTP secret its CONFIRMED credential will store.

    The PR #6 browser staff contract (final review P1): the teacher and
    admin suites log in through the REAL staff form — password plus a
    TOTP code the spec computes RFC 6238-style from this secret — so the
    seeded credential must be answerable, unlike the factories'
    stand-in. The caller encrypts the secret with the backend's own
    ``encrypt_totp_secret`` in the same transaction that adds the rows.
    """
    from app.core.security import hash_password
    from app.modules.identity.enums import UserStatus as SeedStatus
    from app.modules.identity.models import User as UserRow
    from app.modules.identity.totp import generate_totp_secret

    user = UserRow(
        username=username,
        password_hash=hash_password(DEFAULT_PASSWORD),
        nickname=nickname,
        phone_e164=None,
        role=role,
        status=SeedStatus.ACTIVE,
    )
    return user, generate_totp_secret()


async def _mint(user_id: UUID) -> str:
    from app.core.clock import SystemClock
    from app.modules.identity.dependencies import get_access_token_codec
    from app.modules.identity.models import User
    from app.modules.identity.session_service import SessionService

    factory = _factory()
    try:
        async with factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                raise SystemExit(f"unknown user {user_id}")
            sessions = SessionService(
                clock=SystemClock(), access_codec=get_access_token_codec()
            )
            _, tokens = await sessions.issue_session(
                db, user=user, now=SystemClock().now()
            )
            await db.commit()
            return tokens.access_token
    finally:
        await _dispose(factory)


async def _seed() -> dict[str, Any]:
    from app.modules.community.models import Comment
    from app.modules.tasks.models import Task
    from app.workers.jobs.project_ranking_update import run_ranking_projection

    run = uuid.uuid4().hex[:12]
    factory = _factory()
    broker = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    tmpdir = tempfile.mkdtemp(prefix=f"cq-e2e-{run}-")
    try:
        student = await seed_student(factory, run=run)
        teacher = await seed_teacher_confirmed_totp(factory, run=run)
        admin = await seed_admin_confirmed_totp(factory, run=run)
        # Plan 10 task 9: a SECOND student authors the anonymous comment
        # under privacy test, with a known email so the DOM negative
        # search covers every identity fact. The PREFIXED run marker
        # keeps the factories' hex-folded student number/phone distinct
        # from the primary student's (a suffix would fold identically).
        author = await seed_student(factory, run=f"d{run}")
        author_email = f"e2e-author-{run}@school.edu"

        # Three independent tasks: A/B each carry one PRE-CLAIMED claim
        # (the deep-link specs consume them), C stays fully open for the
        # UI-claim flow. Separate tasks because the same-task rule
        # refuses a second non-terminal claim for one student.
        task_a = await seed_task_with_assignments(
            factory, teacher_id=teacher.user_id, run=f"{run}a", assignment_count=2
        )
        task_b = await seed_task_with_assignments(
            factory, teacher_id=teacher.user_id, run=f"{run}b", assignment_count=2
        )
        task_c = await seed_task_with_assignments(
            factory, teacher_id=teacher.user_id, run=f"{run}c", assignment_count=2
        )
        claim_a = await seed_claim(factory, task=task_a, student_id=student.user_id)
        claim_b = await seed_claim(factory, task=task_b, student_id=student.user_id)
        item = await seed_reward_item(factory, run=run, point_cost=50)

        # Plan 10 task 9 additions, all world-building (the FLOWS stay
        # in the specs): the author's email, one pre-existing anonymous
        # comment on task A as the reveal dialog's target, and a
        # DEDICATED reveal admin with an EMAIL-SHAPED username plus a
        # REAL answerable TOTP credential. The staff login form demands
        # an email-format identifier client-side, and its backend
        # resolves staff logins BY USERNAME (staff_service:
        # find_by_username(identifier.lower())) — so an email-shaped
        # username is the contract both sides accept. The factories'
        # stand-in secret passes the management guard but cannot ANSWER
        # a login prompt; this credential is genuine base32 the spec
        # computes RFC 6238 codes from.
        #
        # PR #6 final review P1 (task 3): the same shape now also seeds
        # the answerable BROWSER STAFF the teacher/admin suites log in
        # as — teacher A (CQ_E2E_STAFF, also exported as CQ_E2E_TEACHER
        # for admin.spec's privilege negative), an unrelated teacher B
        # (CQ_E2E_STAFF2), and the operations admin (CQ_E2E_ADMIN).
        from cryptography.fernet import Fernet

        from app.modules.identity.enums import Role as SeedRole
        from app.modules.identity.models import User as UserRow
        from app.modules.identity.totp import encrypt_totp_secret

        reveal_admin, reveal_admin_secret = _answerable_staff_row(
            f"e2e-admin-{run}@school.edu",
            f"端到端揭示管理员{run[:4]}",
            SeedRole.ADMIN,
        )
        browser_teacher, browser_teacher_secret = _answerable_staff_row(
            f"e2e-teacher-{run}@school.edu",
            f"端到端浏览器教师{run[:4]}",
            SeedRole.TEACHER,
        )
        browser_teacher2, browser_teacher2_secret = _answerable_staff_row(
            f"e2e-teacher2-{run}@school.edu",
            f"端到端浏览器教师乙{run[:4]}",
            SeedRole.TEACHER,
        )
        browser_admin, browser_admin_secret = _answerable_staff_row(
            f"e2e-ops-admin-{run}@school.edu",
            f"端到端运营管理员{run[:4]}",
            SeedRole.ADMIN,
        )
        answerable_staff = (
            (reveal_admin, reveal_admin_secret),
            (browser_teacher, browser_teacher_secret),
            (browser_teacher2, browser_teacher2_secret),
            (browser_admin, browser_admin_secret),
        )
        async with factory() as db:
            author_row = await db.get(UserRow, author.user_id)
            assert author_row is not None
            author_row.email_normalized = author_email
            author_phone = author_row.phone_e164
            author_nickname = author_row.nickname
            for user_row, _ in answerable_staff:
                db.add(user_row)
            await db.flush()
            for user_row, secret in answerable_staff:
                db.add(
                    TotpCredentialRow(
                        user_id=user_row.id,
                        secret_encrypted=encrypt_totp_secret(
                            Fernet(get_settings().totp_encryption_key), secret
                        ),
                        confirmed_at=dt.datetime.now(dt.UTC),
                    )
                )
            db.add(
                Comment(
                    task_id=task_a.task_id,
                    user_id=author.user_id,
                    content=f"匿名治理目标{run[:6]}：大家记得提前预约座位",
                    is_anonymous=True,
                )
            )
            await db.commit()

        # Spendable points + the boards the ranking page reads: the
        # world closes one ledger entry and runs the REAL projection
        # job at its own effective instant (the earning FLOW itself is
        # the task-2 chain under test, not world-building). The job
        # entry shells an internal asyncio.run, so it runs on a worker
        # thread — the same to_thread discipline the pytest e2e uses.
        effective_at = await seed_points_balance(
            factory,
            student_id=student.user_id,
            amount=_REWARD_POINTS,
            source_id=claim_a.claim_id,
        )
        await asyncio.to_thread(
            run_ranking_projection,
            str(student.user_id),
            effective_at.isoformat(),
            request_id=f"e2e-browser-seed-{run}",
        )

        # --- PR #6 final review P1 (task 3): the operational state the
        # teacher/admin specs assert against, derived from the specs'
        # own assertions ------------------------------------------------
        #
        # teacher.spec's review queue needs a VALIDATED-but-undecided
        # submission on a task the BROWSER teacher owns (the queue is
        # owner-scoped); admin.spec needs a REQUESTED redemption with
        # its ACTIVE freeze (the reject/approve -> fulfill chain) and
        # an ACTIVE student row to suspend-and-restore. The
        # claimant/redeemer is a DEDICATED run-prefixed student: the
        # primary student already holds claims A and B and the
        # actionable-claim quota (3) must stay open for
        # task-claim.spec's UI claim.
        from app.core.security import hash_password
        from app.modules.identity.enums import UserStatus as SeedStatus
        from app.modules.points.models import PointReservation, RewardRedemption
        from app.modules.submissions.models import Submission as SubmissionRow
        from app.modules.system.models import SystemSetting
        from app.modules.tasks.enums import (
            ClaimStatus,
            RewardLockStatus,
        )
        from app.modules.tasks.models import AssignmentClaim

        task_r = await seed_task_with_assignments(
            factory, teacher_id=browser_teacher.id, run=f"{run}r", assignment_count=1
        )
        # Hex-digit prefix "e" (the author's "d" discipline): the
        # factories fold the marker's leading chars as HEX into the
        # student number/phone, so the prefix must be a hex digit.
        redeemer = await seed_student(factory, run=f"e{run}")
        claim_r = await seed_claim(factory, task=task_r, student_id=redeemer.user_id)
        await seed_points_balance(
            factory,
            student_id=redeemer.user_id,
            amount=item.point_cost,
            source_id=claim_r.claim_id,
        )
        async with factory() as db:
            # The snapshot the settings spec mutates and teardown
            # restores (CURRENT_ACADEMIC_TERM row-or-seed priority, the
            # SystemAcademicTermProvider rule) — also the seeded
            # redemption's term_key source.
            term_row = await db.get(SystemSetting, "CURRENT_ACADEMIC_TERM")
            term_before = term_row.value if term_row is not None else None
            term_key = (
                term_before
                if term_before is not None
                else get_settings().current_academic_term
            )

            # The post-validation shape the review queue reads (§12.4):
            # the machine verdict locked the on-time 100% tier
            # PROVISIONALLY and moved the claim to UNDER_REVIEW, the
            # submission to VALIDATED/PENDING_REVIEW with the persisted
            # report — seed_claim's CLAIMED/NONE rows adjusted to that
            # state.
            claim_row = await db.get(AssignmentClaim, claim_r.claim_id)
            assert claim_row is not None
            now = dt.datetime.now(dt.UTC)
            claim_row.status = ClaimStatus.UNDER_REVIEW
            claim_row.reward_lock_status = RewardLockStatus.PROVISIONAL
            claim_row.reward_tier_locked = 100
            claim_row.locked_reward_points = claim_row.base_reward_points_snapshot
            claim_row.reward_locked_at = now
            submission_r = SubmissionRow(
                claim_id=claim_row.id,
                version=1,
                object_key=f"submissions/{claim_row.id}/{uuid.uuid4()}",
                original_filename="e2e-review-queue.csv",
                declared_type="CSV",
                detected_type="CSV",
                file_size=len(_GOOD_CSV),
                submitted_at=now,
                validation_status="VALIDATED",
                review_status="PENDING_REVIEW",
                validation_report=_GOOD_CSV_REPORT,
                retention_until=now + dt.timedelta(days=180),
            )
            db.add(submission_r)
            await db.flush()
            claim_row.latest_submission_id = submission_r.id

            # The redemption request exactly as `request_redemption`
            # leaves it (REQUESTED + ACTIVE freeze, term/price
            # snapshotted) — the admin queue's first row.
            redemption = RewardRedemption(
                user_id=redeemer.user_id,
                reward_item_id=item.reward_item_id,
                status="REQUESTED",
                term_key=term_key,
                points=item.point_cost,
            )
            db.add(redemption)
            await db.flush()
            db.add(
                PointReservation(
                    user_id=redeemer.user_id,
                    redemption_id=redemption.id,
                    points=item.point_cost,
                    status="ACTIVE",
                )
            )

            # The admin users page's suspend target, located by its
            # run-unique handle (the spec's row locator).
            suspended_student = UserRow(
                username=f"e2e-suspended-student-{run}",
                password_hash=hash_password(DEFAULT_PASSWORD),
                nickname=f"停用目标同学{run[:4]}",
                phone_e164=None,
                role=SeedRole.STUDENT,
                status=SeedStatus.ACTIVE,
            )
            db.add(suspended_student)
            await db.commit()

        async with factory() as db:
            task_c_row = await db.get(Task, task_c.task_id)
            assert task_c_row is not None
            task_c_title = task_c_row.title
        honors_before = await snapshot_honor_ids(factory)

        good_csv = str(Path(tmpdir) / "good.csv")
        bad_csv = str(Path(tmpdir) / "bad.csv")
        Path(good_csv).write_bytes(_GOOD_CSV)
        Path(bad_csv).write_bytes(_BAD_CSV)

        world = {
            "run": run,
            "student": {
                "id": str(student.user_id),
                "username": student.username,
                "password": student.password,
            },
            "author": {
                "id": str(author.user_id),
                "username": author.username,
                "password": author.password,
            },
            "teacher_id": str(teacher.user_id),
            "admin_id": str(admin.user_id),
            "reveal_admin_id": str(reveal_admin.id),
            # PR #6 task 3: the answerable browser staff (their TOTP
            # secrets never persist in the world FILE — only the env
            # contract below carries them, and teardown needs just ids).
            "browser_teacher_id": str(browser_teacher.id),
            "browser_teacher2_id": str(browser_teacher2.id),
            "browser_admin_id": str(browser_admin.id),
            "redeemer_id": str(redeemer.user_id),
            "suspended_student_id": str(suspended_student.id),
            "review_task_id": str(task_r.task_id),
            "pending_redemption_id": str(redemption.id),
            "term_before": term_before,
            "task_ids": [str(t.task_id) for t in (task_a, task_b, task_c, task_r)],
            "task_open": {
                "task_id": str(task_c.task_id),
                "title": task_c_title,
            },
            "claim_a": str(claim_a.claim_id),
            "claim_b": str(claim_b.claim_id),
            "reward_item_id": str(item.reward_item_id),
            "reward_cost": item.point_cost,
            "honors_before": sorted(str(id_) for id_ in honors_before),
            "good_csv": good_csv,
            "bad_csv": bad_csv,
            "broker_depth_before": await broker.llen(_BROKER_QUEUE_KEY),
        }
        world_path = Path(tmpdir) / "world.json"
        world_path.write_text(json.dumps(world), encoding="utf-8")

        # The Node-side contract: seeded creds + deep-link paths +
        # ids for lazy token mints. Full URLs are the setup module's
        # job (it knows CQ_E2E_BASE_URL).
        world["world_file"] = str(world_path)
        world["env"] = {
            "CQ_E2E_RUN": run,
            "CQ_E2E_STUDENT": f"{student.username}:{student.password}",
            "CQ_E2E_CLAIM_PATH": f"/claims/{claim_a.claim_id}",
            "CQ_E2E_CLAIM_PATH_2": f"/claims/{claim_b.claim_id}",
            "CQ_E2E_TASK_OPEN_PATH": f"/tasks/{task_c.task_id}",
            "CQ_E2E_GOOD_CSV": good_csv,
            "CQ_E2E_BAD_CSV": bad_csv,
            "CQ_E2E_STUDENT_ID": str(student.user_id),
            "CQ_E2E_TEACHER_ID": str(teacher.user_id),
            "CQ_E2E_ADMIN_ID": str(admin.user_id),
            "CQ_E2E_REWARD_ITEM_ID": str(item.reward_item_id),
            "CQ_E2E_REGISTER_NUMBER": _registered_number(run),
            # Plan 10 task 9 (community.spec): the task detail URL, the
            # anonymous-comment author and their DOM-negative-search
            # secrets, the non-completer (the author never claimed the
            # open task), and the reveal flow's admin staff-login
            # contract (identifier + the genuine TOTP secret).
            "CQ_E2E_TASK_URL": f"/tasks/{task_c.task_id}",
            "CQ_E2E_AUTHOR_STUDENT": f"{author.username}:{author.password}",
            "CQ_E2E_AUTHOR_SECRETS": ",".join(
                (
                    author_nickname,
                    author.username,
                    author_phone or "",
                    author_email,
                    str(author.user_id),
                )
            ),
            "CQ_E2E_NON_COMPLETER_STUDENT": f"{author.username}:{author.password}",
            "CQ_E2E_MODERATION_TASK_PATH": f"/teacher/tasks/{task_a.task_id}",
            "CQ_E2E_REVEAL_ADMIN": (f"{reveal_admin.username}:correct-horse-battery"),
            "CQ_E2E_REVEAL_ADMIN_TOTP_SECRET": reveal_admin_secret,
            # PR #6 task 3: the browser staff's staff-login contracts
            # (identifier:password + the genuine base32 secret). teacher
            # A doubles as CQ_E2E_TEACHER (admin.spec's privilege
            # negative) and the ops admin as CQ_E2E_ADMIN.
            "CQ_E2E_STAFF": f"{browser_teacher.username}:{DEFAULT_PASSWORD}",
            "CQ_E2E_STAFF_TOTP_SECRET": browser_teacher_secret,
            "CQ_E2E_STAFF2": f"{browser_teacher2.username}:{DEFAULT_PASSWORD}",
            "CQ_E2E_STAFF2_TOTP_SECRET": browser_teacher2_secret,
            "CQ_E2E_TEACHER": f"{browser_teacher.username}:{DEFAULT_PASSWORD}",
            "CQ_E2E_TEACHER_TOTP_SECRET": browser_teacher_secret,
            "CQ_E2E_ADMIN": f"{browser_admin.username}:{DEFAULT_PASSWORD}",
            "CQ_E2E_ADMIN_TOTP_SECRET": browser_admin_secret,
        }
        return world
    finally:
        await broker.aclose()
        await _dispose(factory)


async def _clean(world_path: str) -> dict[str, Any]:
    from app.integrations.object_storage_s3 import S3ObjectStorage
    from app.modules.audit.models import AuditLog
    from app.modules.identity.models import StudentWhitelist, User
    from app.modules.rankings.periods import business_day, business_month
    from app.modules.rankings.redis_projection import ALL_TIME_KEY
    from app.modules.submissions.models import UploadIntent
    from app.modules.system.models import SystemSetting
    from app.modules.tasks.models import AssignmentClaim, Task

    world = json.loads(Path(world_path).read_text(encoding="utf-8"))
    run = world["run"]
    factory = _factory()
    broker = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    removed: dict[str, int] = {"objects": 0, "drift_users": 0, "board_members": 0}
    try:
        task_ids = [uuid.UUID(t) for t in world["task_ids"]]
        user_ids = [
            uuid.UUID(world["teacher_id"]),
            uuid.UUID(world["admin_id"]),
            uuid.UUID(world["student"]["id"]),
        ]
        # World-contract fields that landed with later tasks: absent in
        # older world files, so teardown stays version-tolerant (a
        # missing optional member simply contributes no rows).
        for optional_member in (
            "reveal_admin_id",
            "browser_teacher_id",
            "browser_teacher2_id",
            "browser_admin_id",
            "redeemer_id",
            "suspended_student_id",
        ):
            if optional_member in world:
                user_ids.append(uuid.UUID(world[optional_member]))
        if "author" in world:
            user_ids.append(uuid.UUID(world["author"]["id"]))
        # Tasks the teacher suite created through the REAL UI (the
        # create -> import -> publish flow) exist only as rows — sweep
        # them by owner (the browser staff teachers) so clean_world's
        # FK-ordered deletion covers them with everything else.
        ui_task_owners = [
            uuid.UUID(world[key])
            for key in ("browser_teacher_id", "browser_teacher2_id")
            if key in world
        ]
        if ui_task_owners:
            async with factory() as db:
                task_ids.extend(
                    (
                        await db.execute(
                            select(Task.id).where(
                                Task.owner_teacher_id.in_(ui_task_owners),
                                Task.id.not_in(task_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        # Rows the SPECS created through the real surfaces: the
        # registered student (auth spec, run-derived number), the
        # invitation-accepted staff account (staff-auth spec,
        # run-prefixed email as username), the whitelist entries (the
        # auth spec's register number plus the admin import spec's
        # 9-digit stamp entries), and the audit rows the run's staff
        # actors appended through the real APIs (audit_logs carry no
        # FKs — nothing else removes them, and the whitelist-admin
        # suite asserts on a clean audit table).
        register_prefix = _registered_number(run)
        async with factory() as db:
            drift = (
                (
                    await db.execute(
                        select(User.id).where(
                            (User.username.like(f"{register_prefix}%"))
                            | (User.username.like(f"%-{run}@%"))
                        )
                    )
                )
                .scalars()
                .all()
            )
            removed["drift_users"] = len(drift)
            user_ids.extend(row for row in drift if row not in user_ids)
            await db.execute(
                delete(StudentWhitelist).where(
                    (StudentWhitelist.student_number.like(f"{register_prefix}%"))
                    | (
                        # the whitelist-import spec's confirmed row is a
                        # 9-digit Date.now stamp — run-unique, never a
                        # factory number (11+ digits) or the register
                        # prefix (16 digits)
                        StudentWhitelist.student_number.regexp_match("^[0-9]{9}$")
                    )
                )
            )
            await db.commit()

        # PR #6 final review P2 (task 4): ONE source list for the audit
        # cleanup — every account this run seeded or the specs created
        # (the drift sweep included) may appear as an AuditLog actor:
        # the reveal admin (COMMUNITY_IDENTITY_REVEAL — the row class
        # the previous hand-trimmed [teacher, admin] list left behind),
        # the browser staff, the whitelist/settings admins, and the
        # invited staff's own STAFF_INVITATION_ACCEPTED row. The
        # user-targeted family (USER_SUSPENDED/_REACTIVATED, points
        # adjustments) names this run's users in target_id, so the
        # delete covers both dimensions and the zero-assertion at the
        # end of the clean pins them empty.
        staff_actor_ids = list(user_ids)
        actor_target_ids = [str(user_id) for user_id in user_ids]
        async with factory() as db:
            removed["audit_rows"] = (
                await db.execute(
                    delete(AuditLog).where(
                        or_(
                            AuditLog.actor_user_id.in_(staff_actor_ids),
                            AuditLog.target_id.in_(actor_target_ids),
                        )
                    )
                )
            ).rowcount
            await db.commit()

        # The settings spec's term edit is GLOBAL state: restore the
        # pre-run row exactly (value, or the row's meaningful absence —
        # "never configured here; use the deployment seed").
        if "term_before" in world:
            term_before = world["term_before"]
            async with factory() as db:
                term_row = await db.get(SystemSetting, "CURRENT_ACADEMIC_TERM")
                if term_before is None:
                    if term_row is not None:
                        await db.delete(term_row)
                elif term_row is not None:
                    term_row.value = term_before
                await db.commit()

        # S3 objects this run PUT (guarded: absent objects raise, §27).
        storage = S3ObjectStorage(get_settings())
        async with factory() as db:
            keys = (
                (
                    await db.execute(
                        select(UploadIntent.object_key).where(
                            UploadIntent.claim_id.in_(
                                select(AssignmentClaim.id).where(
                                    AssignmentClaim.task_id.in_(task_ids)
                                )
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        for key in keys:
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(storage.delete_object, object_key=key)
                removed["objects"] += 1

        honors_before = {uuid.UUID(t) for t in world["honors_before"]}
        await clean_world(
            factory,
            user_ids=user_ids,
            task_ids=task_ids,
            reward_item_ids=[uuid.UUID(world["reward_item_id"])],
            honor_ids_before=honors_before,
        )

        # Teardown regression (PR #6 task 4): with every removal done,
        # not one AuditLog row may still name THIS run as actor or as a
        # user-target — the reveal flow's rows are the family that used
        # to survive a green-looking teardown. A non-zero count fails
        # the clean (and so the gate) instead of rotting the shared
        # stack for the next suite.
        async with factory() as db:
            audit_survivors = int(
                await db.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        or_(
                            AuditLog.actor_user_id.in_(staff_actor_ids),
                            AuditLog.target_id.in_(actor_target_ids),
                        )
                    )
                )
            )
        if audit_survivors:
            raise SystemExit(
                f"browser_world clean: {audit_survivors} audit row(s) still name "
                "this run's actors or user-targets — teardown is incomplete"
            )

        # Boards: remove only THIS student's member (never another
        # suite's entries); emptied ZSETs vanish on their own.
        member = world["student"]["id"]
        tz = ZoneInfo(get_settings().business_timezone)
        now = dt.datetime.now(dt.UTC)
        board_keys = [
            f"ranking:daily:{business_day(now, tz).isoformat()}",
            f"ranking:monthly:{business_month(now, tz):%Y-%m}",
            ALL_TIME_KEY,
        ]
        for key in board_keys:
            removed["board_members"] += await broker.zrem(key, member)

        # Broker: restore the pre-seed backlog this run's publishes
        # appended onto (DEL when the queue was empty at seed).
        depth_before = int(world.get("broker_depth_before", 0))
        if depth_before > 0:
            await broker.ltrim(_BROKER_QUEUE_KEY, 0, depth_before - 1)
        else:
            await broker.delete(_BROKER_QUEUE_KEY)
        return removed
    finally:
        await broker.aclose()
        await _dispose(factory)


async def _main(argv: list[str]) -> Any:
    if not argv:
        raise SystemExit(__doc__)
    action = argv[0]
    if action == "seed":
        return await _seed()
    if action == "validate":
        from app.workers.jobs.validate_submission import run_submission_validation

        # The job entries shell their own asyncio.run: give them a
        # worker thread (no running loop there), the pytest convention.
        return await asyncio.to_thread(
            run_submission_validation,
            argv[1],
            request_id=f"e2e-browser-{uuid.uuid4().hex[:8]}",
        )
    if action == "rank":
        from app.workers.jobs.project_ranking_update import run_ranking_projection

        effective_at = argv[2] if len(argv) > 2 else None
        if effective_at is None:
            effective_at = dt.datetime.now(dt.UTC).isoformat()
        return await asyncio.to_thread(
            run_ranking_projection,
            argv[1],
            effective_at,
            request_id=f"e2e-browser-{uuid.uuid4().hex[:8]}",
        )
    if action == "otp-code":
        from tests.e2e.otp_probe import recover_otp_code

        broker = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            challenge_id, code = await recover_otp_code(broker, argv[1])
            return {"challenge_id": str(challenge_id), "code": code}
        finally:
            await broker.aclose()
    if action == "mint":
        return {"token": await _mint(uuid.UUID(argv[1]))}
    if action == "clean":
        return await _clean(argv[1])
    raise SystemExit(f"unknown action {action!r}")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main(sys.argv[1:]))))
