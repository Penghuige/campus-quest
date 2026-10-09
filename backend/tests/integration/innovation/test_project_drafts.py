"""Owner-only drafts through real sessions and PostgreSQL, never SQLite."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.errors import BusinessError
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_access_token_codec, get_business_clock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User, UserSession
from app.modules.identity.routing_common import REFRESH_COOKIE_NAME
from app.modules.identity.session_service import SessionService
from app.modules.innovation.models import ProjectDraft
from app.modules.innovation.schemas import ProjectDraftCreate, ProjectDraftUpdate
from app.modules.innovation.service import ProjectDraftService

pytestmark = pytest.mark.integration
PATH = "/api/v1/ie/me/project-drafts"


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def api_app(db_session: AsyncSession, api_clock: FrozenClock) -> FastAPI:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _user(db: AsyncSession, role: Role = Role.STUDENT) -> User:
    user = User(
        username=f"ie-{uuid4().hex}",
        password_hash="unused-test-password-hash",
        nickname="项目同学",
        role=role.value,
        status=UserStatus.ACTIVE.value,
    )
    db.add(user)
    await db.flush()
    return user


async def _headers(db: AsyncSession, clock: FrozenClock, user: User) -> dict:
    service = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await service.issue_session(db, user=user, now=clock.now())
    return {"Authorization": f"Bearer {tokens.access_token}"}


def _payload(**overrides: object) -> dict:
    return {"request_id": str(uuid4()), "title": "  校园调研  ", **overrides}


def _edit(body: dict, **overrides: object) -> dict:
    return {
        key: overrides.get(key, body[key])
        for key in ("title", "summary", "direction", "stage", "team_status", "version")
    }


async def test_owner_round_trip(client, db_session, api_clock) -> None:
    owner = await _user(db_session)
    headers = await _headers(db_session, api_clock, owner)
    response = await client.post(PATH, json=_payload(), headers=headers)
    assert response.status_code == 201
    draft = response.json()
    assert set(draft) == {
        "id",
        "title",
        "summary",
        "direction",
        "stage",
        "team_status",
        "version",
        "created_at",
        "updated_at",
    }
    assert draft["title"] == "校园调研"
    assert draft["version"] == 1
    assert response.headers["cache-control"] == "private, no-store"
    assert (await client.get(f"{PATH}/{draft['id']}", headers=headers)).json() == draft
    listed = await client.get(PATH, headers=headers, params={"limit": 1, "offset": 0})
    assert listed.json() == {"items": [draft], "total": 1, "limit": 1, "offset": 0}
    assert (
        await client.get(PATH, headers=headers, params={"limit": 0})
    ).status_code == 422
    edited = await client.patch(
        f"{PATH}/{draft['id']}",
        headers=headers,
        json=_edit(draft, summary=" 初步访问十位同学 "),
    )
    assert edited.status_code == 200
    assert edited.json()["summary"] == "初步访问十位同学"
    assert edited.json()["version"] == 2
    stored = await db_session.get(ProjectDraft, UUID(draft["id"]))
    assert stored is not None and stored.summary == "初步访问十位同学"
    forbidden = await client.post(
        PATH, json=_payload(owner_user_id=str(owner.id)), headers=headers
    )
    assert forbidden.status_code == 422


async def test_other_student_cannot_read_or_write(
    client, db_session, api_clock
) -> None:
    owner, outsider = await _user(db_session), await _user(db_session)
    own_headers = await _headers(db_session, api_clock, owner)
    other_headers = await _headers(db_session, api_clock, outsider)
    draft = (await client.post(PATH, json=_payload(), headers=own_headers)).json()
    assert (await client.get(PATH, headers=other_headers)).json()["items"] == []
    for draft_id in (draft["id"], str(uuid4())):
        read = await client.get(f"{PATH}/{draft_id}", headers=other_headers)
        write = await client.patch(
            f"{PATH}/{draft_id}", json=_edit(draft), headers=other_headers
        )
        assert read.status_code == write.status_code == 404
        assert (
            read.json()["error"]["code"] == write.json()["error"]["code"] == "NOT_FOUND"
        )
    assert (
        await client.get(f"{PATH}/{draft['id']}", headers=own_headers)
    ).json() == draft


async def test_authentication_and_account_gates(client, db_session, api_clock) -> None:
    cookie_user = await _user(db_session)
    sessions = SessionService(clock=api_clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(
        db_session, user=cookie_user, now=api_clock.now()
    )
    draft = (
        await client.post(
            PATH,
            json=_payload(),
            headers={"Authorization": f"Bearer {tokens.access_token}"},
        )
    ).json()
    for cookies in ({}, {REFRESH_COOKIE_NAME: tokens.refresh_token}):
        client.cookies.update(cookies)
        for method, path, body in (
            ("GET", PATH, None),
            ("POST", PATH, _payload()),
            ("GET", f"{PATH}/{draft['id']}", None),
            ("PATCH", f"{PATH}/{draft['id']}", _edit(draft)),
        ):
            assert (await client.request(method, path, json=body)).status_code == 401
    client.cookies.clear()
    for role in (Role.TEACHER, Role.ADMIN, Role.STUDENT):
        user = await _user(db_session, role)
        headers = await _headers(db_session, api_clock, user)
        if role == Role.STUDENT:
            user.status = UserStatus.SUSPENDED.value
            await db_session.flush()
        for method, path, body in (
            ("GET", PATH, None),
            ("POST", PATH, _payload()),
            ("GET", f"{PATH}/{draft['id']}", None),
            ("PATCH", f"{PATH}/{draft['id']}", _edit(draft)),
        ):
            response = await client.request(method, path, headers=headers, json=body)
            assert response.status_code == 403
            expected = (
                "ACCOUNT_NOT_ACTIVE" if role == Role.STUDENT else "PERMISSION_DENIED"
            )
            assert response.json()["error"]["code"] == expected


async def test_create_retry_preserves_later_edit(client, db_session, api_clock) -> None:
    user = await _user(db_session)
    headers = await _headers(db_session, api_clock, user)
    payload = _payload()
    draft = (await client.post(PATH, json=payload, headers=headers)).json()
    edited = (
        await client.patch(
            f"{PATH}/{draft['id']}",
            json=_edit(draft, title="修改之后"),
            headers=headers,
        )
    ).json()
    retried = await client.post(
        PATH, json={**payload, "title": "校园调研"}, headers=headers
    )
    assert retried.status_code == 200 and retried.json() == edited
    mismatch = await client.post(
        PATH, json={**payload, "title": "不同原始内容"}, headers=headers
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "PROJECT_DRAFT_REQUEST_CONFLICT"
    stale = await client.patch(
        f"{PATH}/{draft['id']}", json=_edit(draft), headers=headers
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PROJECT_DRAFT_VERSION_CONFLICT"


async def test_independent_sessions_enforce_idempotency_and_version(
    db_engine: AsyncEngine,
    api_clock: FrozenClock,
) -> None:
    """Committed fixture; own cleanup only, and no rollback-harness session."""
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as seed:
        owner = await _user(seed)
        owner_id = owner.id
        await seed.commit()
    actor = Actor(user_id=owner_id, role=Role.STUDENT)
    service = ProjectDraftService(clock=api_clock)
    payload = ProjectDraftCreate.model_validate(_payload())
    try:

        async def create():
            async with sessions() as db:
                return await service.create(db, actor=actor, payload=payload)

        created = await asyncio.wait_for(asyncio.gather(create(), create()), timeout=15)
        assert sorted(result[1] for result in created) == [False, True]
        draft = created[0][0]
        assert created[1][0].id == draft.id
        # A different connection sees committed state, including injected time.
        async with sessions() as reader:
            row = await reader.get(ProjectDraft, draft.id)
            assert row is not None and row.created_at == api_clock.now()
            assert (
                await reader.scalar(
                    select(func.count())
                    .select_from(ProjectDraft)
                    .where(
                        ProjectDraft.owner_user_id == owner_id,
                    )
                )
                == 1
            )

        async def update(title: str):
            async with sessions() as db:
                try:
                    return await service.update(
                        db,
                        actor=actor,
                        draft_id=draft.id,
                        payload=ProjectDraftUpdate.model_validate(
                            _edit(draft.model_dump(), title=title)
                        ),
                    )
                except BusinessError as error:
                    return error

        outcomes = await asyncio.wait_for(
            asyncio.gather(update("一"), update("二")), timeout=15
        )
        successes = [
            result for result in outcomes if not isinstance(result, BusinessError)
        ]
        failures = [result for result in outcomes if isinstance(result, BusinessError)]
        assert len(successes) == len(failures) == 1
        assert failures[0].code == "PROJECT_DRAFT_VERSION_CONFLICT"
        async with sessions() as reader:
            row = await reader.get(ProjectDraft, draft.id)
            assert (
                row is not None and row.version == 2 and row.title == successes[0].title
            )
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(ProjectDraft).where(ProjectDraft.owner_user_id == owner_id)
            )
            await cleanup.execute(
                delete(UserSession).where(UserSession.user_id == owner_id)
            )
            await cleanup.execute(delete(User).where(User.id == owner_id))
            await cleanup.commit()


@pytest.mark.parametrize(
    "bad", [{"title": " "}, {"summary": "长" * 2001}, {"version": 0}]
)
async def test_database_constraints(db_session, api_clock, bad) -> None:
    owner = await _user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ProjectDraft(
                    owner_user_id=owner.id,
                    creation_request_id=uuid4(),
                    creation_payload_fingerprint="0" * 64,
                    title="项目",
                    summary="",
                    direction="",
                    stage="",
                    team_status="",
                    version=1,
                    created_at=api_clock.now(),
                    updated_at=api_clock.now(),
                )
            )
            row = next(obj for obj in db_session.new if isinstance(obj, ProjectDraft))
            for key, value in bad.items():
                setattr(row, key, value)
            await db_session.flush()
