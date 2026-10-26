"""Browser cleanup must remove only its talent world and restore history guards."""

import pytest
from sqlalchemy import func, select, text

from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.innovation.talent_models import TalentProfile, TalentRevision
from app.modules.innovation.talent_schemas import TalentVersionCommand
from tests.e2e.factories import clean_world
from tests.integration.innovation.talent_support import account
from tests.integration.innovation.talent_support import clock as clock
from tests.integration.innovation.test_talent_races import committed as committed
from tests.integration.innovation.test_talent_races import saved

pytestmark = pytest.mark.integration


async def test_cleanup_preserves_other_world_and_restores_history_guards(
    committed, clock
):
    sessions, service, actor = committed
    async with sessions() as db:
        other, _ = await account(db, clock)
        other_actor = Actor(other.id, Role.STUDENT)
        await db.commit()
    try:
        for owner in (actor, other_actor):
            row = await saved(sessions, service, owner)
            async with sessions() as db:
                from uuid import uuid4

                from app.modules.audit.context import AuditContext

                await service.publish(
                    db,
                    actor=owner,
                    payload=TalentVersionCommand(
                        version=row.version, request_id=uuid4()
                    ),
                    context=AuditContext(None, None),
                )
                await db.commit()
        await clean_world(sessions, user_ids=[actor.user_id])
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(TalentProfile)
                    .where(TalentProfile.owner_user_id == actor.user_id)
                )
                == 0
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(TalentRevision)
                    .join(TalentProfile, TalentRevision.profile_id == TalentProfile.id)
                    .where(TalentProfile.owner_user_id == other_actor.user_id)
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_trigger WHERE "
                        "tgrelid = 'ie_talent_revisions'::regclass "
                        "AND NOT tgisinternal AND tgenabled = 'D'"
                    )
                )
                == 0
            )
    finally:
        # The existing fixture handles the first owner's failed-RED cleanup.
        async with sessions() as db:
            from sqlalchemy import delete, update

            from app.modules.identity.models import User, UserSession
            from app.modules.innovation.talent_models import TalentActionReceipt

            profiles = select(TalentProfile.id).where(
                TalentProfile.owner_user_id == other_actor.user_id
            )
            await db.execute(
                update(TalentProfile)
                .where(TalentProfile.owner_user_id == other_actor.user_id)
                .values(visibility="HIDDEN", public_revision_id=None)
            )
            for model in (TalentRevision, TalentActionReceipt):
                table = model.__tablename__
                await db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
                await db.execute(delete(model).where(model.profile_id.in_(profiles)))
                await db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
            await db.execute(
                delete(TalentProfile).where(
                    TalentProfile.owner_user_id == other_actor.user_id
                )
            )
            await db.execute(
                delete(UserSession).where(UserSession.user_id == other_actor.user_id)
            )
            await db.execute(delete(User).where(User.id == other_actor.user_id))
            await db.commit()
