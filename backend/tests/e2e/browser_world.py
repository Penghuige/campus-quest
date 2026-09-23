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
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.modules.identity.models import (  # noqa: E402
    TotpCredential as TotpCredentialRow,
)
from tests.e2e.factories import (  # noqa: E402
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
        from cryptography.fernet import Fernet

        from app.core.security import hash_password
        from app.modules.identity.enums import Role as SeedRole
        from app.modules.identity.enums import UserStatus as SeedStatus
        from app.modules.identity.models import User as UserRow
        from app.modules.identity.totp import (
            encrypt_totp_secret,
            generate_totp_secret,
        )

        admin_totp_secret = generate_totp_secret()
        reveal_admin = UserRow(
            username=f"e2e-admin-{run}@school.edu",
            password_hash=hash_password("correct-horse-battery"),
            nickname=f"端到端揭示管理员{run[:4]}",
            phone_e164=None,
            role=SeedRole.ADMIN,
            status=SeedStatus.ACTIVE,
        )
        async with factory() as db:
            author_row = await db.get(UserRow, author.user_id)
            assert author_row is not None
            author_row.email_normalized = author_email
            author_phone = author_row.phone_e164
            author_nickname = author_row.nickname
            db.add(reveal_admin)
            await db.flush()
            db.add(
                TotpCredentialRow(
                    user_id=reveal_admin.id,
                    secret_encrypted=encrypt_totp_secret(
                        Fernet(get_settings().totp_encryption_key), admin_totp_secret
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
            "task_ids": [str(t.task_id) for t in (task_a, task_b, task_c)],
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
            "CQ_E2E_REVEAL_ADMIN_TOTP_SECRET": admin_totp_secret,
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
    from app.modules.tasks.models import AssignmentClaim

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
        # World-contract fields that landed with plan 10 task 9: absent
        # in older world files, so teardown stays version-tolerant (a
        # missing optional member simply contributes no rows).
        for optional_member in ("reveal_admin_id",):
            if optional_member in world:
                user_ids.append(uuid.UUID(world[optional_member]))
        if "author" in world:
            user_ids.append(uuid.UUID(world["author"]["id"]))
        # Rows the SPECS created through the real surfaces: the
        # registered student (auth spec, run-derived number), the
        # invitation-accepted staff account (staff-auth spec,
        # run-prefixed email as username), the whitelist entry, and the
        # audit rows the run's staff actors appended through the real
        # APIs (audit_logs carry no FKs — nothing else removes them, and
        # the whitelist-admin suite asserts on a clean audit table).
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
                    StudentWhitelist.student_number.like(f"{register_prefix}%")
                )
            )
            await db.commit()

        staff_actor_ids = [uuid.UUID(world["teacher_id"]), uuid.UUID(world["admin_id"])]
        async with factory() as db:
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_user_id.in_(staff_actor_ids))
            )
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
