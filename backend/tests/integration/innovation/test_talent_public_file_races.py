"""Distinct identities and committed PG changes order final byte authorization."""

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.core.errors import BusinessError
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User, UserSession
from app.modules.innovation.public_talent_service import PublicTalentService
from app.modules.innovation.talent_file_service import TalentFileService
from app.modules.innovation.talent_schemas import (
    TalentFileCommand,
    TalentSaveCommand,
    TalentVersionCommand,
)
from tests.fakes.integrations import FakeObjectStorage
from tests.integration.innovation.talent_support import account
from tests.integration.innovation.test_talent_file_races import CONTEXT, create_file
from tests.integration.innovation.test_talent_files import Scanner
from tests.integration.innovation.test_talent_files import clock as clock
from tests.integration.innovation.test_talent_races import committed as committed
from tests.integration.innovation.test_talent_races import saved

pytestmark = pytest.mark.integration


@pytest.fixture
async def viewer(committed, clock):
    sessions, _, _ = committed
    async with sessions() as db:
        user, _ = await account(db, clock)
        viewer_id = user.id
        await db.commit()
    try:
        yield Actor(viewer_id, Role.STUDENT)
    finally:
        async with sessions() as db:
            await db.execute(
                delete(UserSession).where(UserSession.user_id == viewer_id)
            )
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_user_id == viewer_id)
            )
            await db.execute(delete(User).where(User.id == viewer_id))
            await db.commit()


class PausingStorage(FakeObjectStorage):
    pause = False

    def __init__(self, clock):
        super().__init__(clock=clock)
        self.entered, self.release = threading.Event(), threading.Event()

    def read_bounded_object(self, **kwargs):
        result = super().read_bounded_object(**kwargs)
        if self.pause:
            self.entered.set()
            assert self.release.wait(10)
        return result


async def setup_public(committed, clock):
    sessions, talents, actor = committed
    row = await saved(sessions, talents, actor)
    storage = PausingStorage(clock)
    files = TalentFileService(clock=clock, storage=storage, scanner=Scanner())
    file = await create_file(sessions, files, actor)
    async with sessions() as db:
        await files.complete(db, actor=actor, file_id=file.id, context=CONTEXT)
        row = (
            await talents.save(
                db,
                actor=actor,
                context=CONTEXT,
                payload=TalentSaveCommand(
                    request_id=uuid4(),
                    version=row.version,
                    fields=row.fields.model_copy(update={"file_ids": [file.id]}),
                ),
            )
        ).current
        row = (
            await talents.publish(
                db,
                actor=actor,
                context=CONTEXT,
                payload=TalentVersionCommand(request_id=uuid4(), version=row.version),
            )
        ).current
    return row, file, files, storage


async def read_race(committed, clock, viewer, action):
    sessions, talents, owner = committed
    row, file, files, storage = await setup_public(committed, clock)
    public = PublicTalentService(clock=clock, storage=storage)
    storage.pause = True

    async def read():
        async with sessions() as db:
            with pytest.raises(BusinessError) as error:
                await public.read_file_content(
                    db,
                    actor=viewer,
                    profile_id=row.id,
                    file_id=file.id,
                    expected_public_version=row.public_version,
                    context=CONTEXT,
                )
            assert error.value.status_code == (404 if action == "hide" else 409)

    task = asyncio.create_task(read())
    try:
        assert await asyncio.to_thread(storage.entered.wait, 5)
        async with asyncio.timeout(5):
            async with sessions() as db:
                payload = TalentVersionCommand(request_id=uuid4(), version=row.version)
                if action == "hide":
                    await talents.hide(
                        db, actor=owner, payload=payload, context=CONTEXT
                    )
                elif action == "publish":
                    await talents.publish(
                        db, actor=owner, payload=payload, context=CONTEXT
                    )
                else:
                    await files.revoke(
                        db,
                        actor=owner,
                        file_id=file.id,
                        context=CONTEXT,
                        payload=TalentFileCommand(**payload.model_dump()),
                    )
        storage.release.set()
        await asyncio.wait_for(task, 5)
    finally:
        storage.release.set()
        await asyncio.gather(task, return_exceptions=True)
    async with sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "ie.talent.file-read",
                    AuditLog.actor_user_id == viewer.user_id,
                )
            )
            == 0
        )


async def test_close_during_public_file_read_denies_final_output(
    committed, clock, viewer
):
    await read_race(committed, clock, viewer, "hide")


async def test_revoke_during_public_file_read_denies_final_output(
    committed, clock, viewer
):
    await read_race(committed, clock, viewer, "revoke")


async def test_revision_change_requires_refresh_before_contact_or_download(
    committed, clock, viewer
):
    await read_race(committed, clock, viewer, "publish")


async def test_public_file_audit_failure_never_returns_loaded_bytes(
    committed, clock, viewer
):
    from app.modules.audit.service import AuditLogWriter

    sessions, _, _ = committed
    row, file, _, storage = await setup_public(committed, clock)

    class UnavailableAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("controlled unavailable audit")

    service = PublicTalentService(
        clock=clock, storage=storage, audit=UnavailableAudit()
    )
    async with sessions() as db:
        with pytest.raises(RuntimeError, match="controlled unavailable audit"):
            await service.read_file_content(
                db,
                actor=viewer,
                profile_id=row.id,
                file_id=file.id,
                expected_public_version=row.public_version,
                context=CONTEXT,
            )
