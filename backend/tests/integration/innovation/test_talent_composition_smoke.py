"""Real production talent composition: private signing, Clamd and byte proxy."""

import asyncio
import os
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.innovation.public_talent_router import get_public_talent_service
from app.modules.innovation.talent_file_router import get_talent_file_service
from app.modules.innovation.talent_file_schemas import TalentFileIntentCreate
from app.modules.innovation.talent_models import TalentTag
from app.modules.innovation.talent_schemas import (
    TalentSaveCommand,
    TalentVersionCommand,
)
from tests.integration.innovation.talent_support import clock as clock
from tests.integration.innovation.talent_support import fields
from tests.integration.innovation.test_achievement_evidence import PDF
from tests.integration.innovation.test_achievement_review_operations import account

pytestmark = pytest.mark.integration
if (
    os.environ.get("CQ_S3_SMOKE") != "1"
    or os.environ.get("CQ_EVIDENCE_SCAN_SMOKE") != "1"
):
    pytest.skip(
        "real talent composition requires S3 and scanner smoke flags",
        allow_module_level=True,
    )


async def test_real_talent_provider_checks_private_file_and_serves_same_bytes(
    db_session, clock
):
    user, _ = await account(db_session, clock)
    actor = Actor(user.id, Role.STUDENT)
    service = get_talent_file_service(clock)
    context = AuditContext(None, None)
    tag = await db_session.scalar(select(TalentTag.id).where(TalentTag.enabled))
    await service.talents.save(
        db_session,
        actor=actor,
        context=context,
        payload=TalentSaveCommand(request_id=uuid4(), version=0, fields=fields(tag)),
    )
    signed, _ = await service.create_intent(
        db_session,
        actor=actor,
        context=context,
        payload=TalentFileIntentCreate(
            request_id=uuid4(),
            name="模拟作品.pdf",
            content_type="application/pdf",
            size=len(PDF),
        ),
    )
    key = unquote(urlsplit(signed.upload_url).path).split("/", 2)[2]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            assert (
                await client.put(
                    signed.upload_url, headers=signed.client_headers, content=PDF
                )
            ).status_code == 200
            assert (
                await client.put(
                    signed.upload_url, headers=signed.client_headers, content=PDF
                )
            ).status_code == 412
        checked = await service.complete(
            db_session, actor=actor, file_id=signed.file.id, context=context
        )
        assert checked.state == "READY", checked.failure_code
        content = await service.read_owned_content(
            db_session, actor=actor, file_id=checked.id, context=context
        )
        assert content.content == PDF
        profile = (
            await service.talents.get_owned(db_session, actor=actor, context=context)
        ).profile
        assert profile is not None
        updated = await service.talents.save(
            db_session,
            actor=actor,
            context=context,
            payload=TalentSaveCommand(
                request_id=uuid4(),
                version=profile.version,
                fields=profile.fields.model_copy(update={"file_ids": [checked.id]}),
            ),
        )
        public_row = (
            await service.talents.publish(
                db_session,
                actor=actor,
                context=context,
                payload=TalentVersionCommand(
                    request_id=uuid4(), version=updated.current.version
                ),
            )
        ).current
        viewer, _ = await account(db_session, clock)
        public = get_public_talent_service(clock)
        args = {
            "actor": Actor(viewer.id, Role.STUDENT),
            "profile_id": public_row.id,
            "file_id": checked.id,
            "expected_public_version": public_row.public_version,
            "context": context,
        }
        assert (await public.read_file_content(db_session, **args)).content == PDF
        await service.talents.hide(
            db_session,
            actor=actor,
            context=context,
            payload=TalentVersionCommand(
                request_id=uuid4(), version=public_row.version
            ),
        )
        with pytest.raises(BusinessError) as closed:
            await public.read_file_content(db_session, **args)
        assert closed.value.status_code == 404
    finally:
        await asyncio.to_thread(service.storage.delete_object, object_key=key)
