"""Independent committed PG sessions fence delayed external scan results."""

import asyncio
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.clock import FrozenClock
from app.core.errors import BusinessError
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User, UserSession
from app.modules.innovation.evidence_models import AchievementEvidence
from app.modules.innovation.evidence_schemas import EvidenceIntentCreate
from app.modules.innovation.evidence_service import EvidenceService
from app.modules.innovation.models import (
    AchievementDraft,
    OwnerQualification,
    ProjectDraft,
)
from tests.integration.innovation.test_achievement_evidence import (
    PDF,
    Scanner,
    world,
)
from tests.integration.innovation.test_achievement_evidence import clock as clock
from tests.integration.innovation.test_achievement_evidence import storage as storage

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("change", ["remove", "suspend", "replace_attempt"])
async def test_late_scan_revalidates_committed_state(db_engine, clock, storage, change):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    started, release = Event(), Event()

    class DelayedScanner(Scanner):
        def scan(self, content):
            started.set()
            if not release.wait(10):
                raise TimeoutError("test barrier did not release")

    async with sessions() as seed:
        owner, parent, achievement, _ = await world(seed, clock)
        uid, pid, aid = owner.id, parent.id, achievement.id
        await seed.commit()
    actor = Actor(user_id=uid, role=Role.STUDENT)
    service = EvidenceService(clock=clock, storage=storage, scanner=DelayedScanner())
    args = {"actor": actor, "project_id": pid, "achievement_id": aid}
    job = None
    try:
        async with sessions() as db:
            result, _ = await service.create_intent(
                db,
                **args,
                payload=EvidenceIntentCreate(
                    request_id=uuid4(), content_type="application/pdf", size=len(PDF)
                ),
            )
        eid = result.evidence.id
        storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)

        async def complete():
            async with sessions() as db:
                return await service.complete(db, **args, evidence_id=eid)

        job = asyncio.create_task(complete())
        assert await asyncio.to_thread(started.wait, 5), "scan never started"
        # Scanner is blocked yet the business locks must already be released.
        async with sessions() as other:
            if change == "remove":
                await asyncio.wait_for(
                    service.remove(other, **args, evidence_id=eid), 3
                )
            elif change == "suspend":
                await other.execute(
                    update(User).where(User.id == uid).values(status="SUSPENDED")
                )
                await asyncio.wait_for(other.commit(), 3)
            else:
                retry = EvidenceService(
                    clock=FrozenClock(clock.now() + timedelta(seconds=151)),
                    storage=storage,
                    scanner=Scanner(),
                )
                checked = await asyncio.wait_for(
                    retry.complete(other, **args, evidence_id=eid), 3
                )
                assert checked.state == "READY"
        release.set()
        with pytest.raises(BusinessError) as error:
            await asyncio.wait_for(job, 5)
        assert (
            error.value.status_code
            == {"remove": 404, "suspend": 403, "replace_attempt": 409}[change]
        )
        async with sessions() as check:
            row = await check.get(AchievementEvidence, eid)
            if change == "replace_attempt":
                assert row.state == "READY" and row.version == checked.version
            else:
                assert row.state != "READY" and row.sha256 is None
    finally:
        release.set()
        if job is not None:
            await asyncio.gather(job, return_exceptions=True)
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AchievementEvidence).where(
                    AchievementEvidence.achievement_id == aid
                )
            )
            await cleanup.execute(
                delete(AchievementDraft).where(AchievementDraft.id == aid)
            )
            await cleanup.execute(
                delete(OwnerQualification).where(OwnerQualification.user_id == uid)
            )
            await cleanup.execute(delete(AuditLog).where(AuditLog.actor_user_id == uid))
            await cleanup.execute(delete(ProjectDraft).where(ProjectDraft.id == pid))
            await cleanup.execute(delete(UserSession).where(UserSession.user_id == uid))
            await cleanup.execute(delete(User).where(User.id == uid))
            await cleanup.commit()
