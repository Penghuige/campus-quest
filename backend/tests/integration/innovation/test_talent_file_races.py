"""Independent PG sessions and external I/O barriers fence late file work."""

import asyncio
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.identity.models import User
from app.modules.innovation.talent_file_schemas import TalentFileIntentCreate
from app.modules.innovation.talent_file_service import TalentFileService
from app.modules.innovation.talent_models import TalentFile
from app.modules.innovation.talent_schemas import TalentFileCommand
from tests.fakes.integrations import FakeObjectStorage
from tests.integration.innovation.test_talent_files import PDF, Scanner
from tests.integration.innovation.test_talent_files import clock as clock
from tests.integration.innovation.test_talent_races import committed as committed
from tests.integration.innovation.test_talent_races import saved

pytestmark = pytest.mark.integration
CONTEXT = AuditContext(None, None)


async def test_concurrent_upload_intents_cannot_exceed_ten_records(committed, clock):
    sessions, talents, actor = committed
    profile = await saved(sessions, talents, actor)
    service = TalentFileService(
        clock=clock, storage=FakeObjectStorage(clock=clock), scanner=Scanner()
    )
    for _ in range(9):
        await create_file(sessions, service, actor)
    barrier = asyncio.Barrier(2)

    async def create():
        await barrier.wait()
        try:
            await create_file(sessions, service, actor)
            return 201
        except BusinessError as error:
            return error.status_code

    assert sorted(await asyncio.gather(create(), create())) == [201, 409]
    async with sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(TalentFile)
                .where(TalentFile.profile_id == profile.id)
            )
            == 10
        )


async def create_file(sessions, service, actor):
    async with sessions() as db:
        result, _ = await service.create_intent(
            db,
            actor=actor,
            context=CONTEXT,
            payload=TalentFileIntentCreate(
                request_id=uuid4(),
                name="模拟作品.pdf",
                content_type="application/pdf",
                size=len(PDF),
            ),
        )
    service.storage.put_object(
        object_key=service.storage.upload_urls[-1].object_key, content=PDF
    )
    return result.file


class FirstScanBarrier:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.lock = threading.Lock()

    def scan(self, content):
        with self.lock:
            self.calls += 1
            first = self.calls == 1
        if first:
            self.entered.set()
            assert self.release.wait(10), "test did not release scanner barrier"


async def test_late_scan_cannot_resurrect_removed_file(committed, clock):
    sessions, talents, actor = committed
    row = await saved(sessions, talents, actor)
    scanner = FirstScanBarrier()
    service = TalentFileService(
        clock=clock, storage=FakeObjectStorage(clock=clock), scanner=scanner
    )
    file = await create_file(sessions, service, actor)

    async def check():
        async with sessions() as db:
            with pytest.raises(BusinessError) as error:
                await service.complete(
                    db, actor=actor, file_id=file.id, context=CONTEXT
                )
            assert error.value.status_code == 404

    task = asyncio.create_task(check())
    try:
        assert await asyncio.to_thread(scanner.entered.wait, 5)
        async with asyncio.timeout(5):
            async with sessions() as db:
                await service.remove(
                    db,
                    actor=actor,
                    file_id=file.id,
                    context=CONTEXT,
                    payload=TalentFileCommand(request_id=uuid4(), version=row.version),
                )
        scanner.release.set()
        await asyncio.wait_for(task, 5)
    finally:
        scanner.release.set()
        await asyncio.gather(task, return_exceptions=True)
    async with sessions() as db:
        stored = await db.get(TalentFile, file.id)
        assert stored.removed_at is not None
        assert stored.state != "READY" and stored.sha256 is None


async def test_scan_lease_fences_previous_attempt(committed, clock):
    sessions, talents, actor = committed
    await saved(sessions, talents, actor)
    scanner = FirstScanBarrier()
    service = TalentFileService(
        clock=clock, storage=FakeObjectStorage(clock=clock), scanner=scanner
    )
    file = await create_file(sessions, service, actor)

    async def first():
        async with sessions() as db:
            with pytest.raises(BusinessError) as error:
                await service.complete(
                    db, actor=actor, file_id=file.id, context=CONTEXT
                )
            assert error.value.status_code == 409

    task = asyncio.create_task(first())
    try:
        assert await asyncio.to_thread(scanner.entered.wait, 5)
        clock.advance(timedelta(seconds=151))
        async with sessions() as db:
            second = await service.complete(
                db, actor=actor, file_id=file.id, context=CONTEXT
            )
            assert second.state == "READY"
        scanner.release.set()
        await asyncio.wait_for(task, 5)
    finally:
        scanner.release.set()
        await asyncio.gather(task, return_exceptions=True)
    async with sessions() as db:
        stored = await db.get(TalentFile, file.id)
        assert stored.state == "READY" and stored.version == second.version


async def test_owned_read_rechecks_account_after_storage_io(committed, clock):
    sessions, talents, actor = committed
    await saved(sessions, talents, actor)

    class PausingStorage(FakeObjectStorage):
        pause = False

        def __init__(self):
            super().__init__(clock=clock)
            self.entered, self.release = threading.Event(), threading.Event()

        def read_bounded_object(self, **kwargs):
            result = super().read_bounded_object(**kwargs)
            if self.pause:
                self.entered.set()
                assert self.release.wait(10)
            return result

    storage = PausingStorage()
    service = TalentFileService(clock=clock, storage=storage, scanner=Scanner())
    file = await create_file(sessions, service, actor)
    async with sessions() as db:
        await service.complete(db, actor=actor, file_id=file.id, context=CONTEXT)
    storage.pause = True

    async def read():
        async with sessions() as db:
            with pytest.raises(BusinessError) as error:
                await service.read_owned_content(
                    db, actor=actor, file_id=file.id, context=CONTEXT
                )
            assert error.value.status_code == 403

    task = asyncio.create_task(read())
    try:
        assert await asyncio.to_thread(storage.entered.wait, 5)
        async with asyncio.timeout(5):
            async with sessions() as db:
                await db.execute(
                    update(User)
                    .where(User.id == actor.user_id)
                    .values(status="SUSPENDED")
                )
                await db.commit()
        storage.release.set()
        await asyncio.wait_for(task, 5)
    finally:
        storage.release.set()
        await asyncio.gather(task, return_exceptions=True)
