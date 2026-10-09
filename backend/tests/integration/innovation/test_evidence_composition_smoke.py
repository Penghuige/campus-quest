"""Production provider: real PG session, MinIO signing/read and ClamAV."""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest

from app.core.config import get_settings
from app.integrations.object_storage_s3 import S3ObjectStorage
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.innovation.evidence_router import get_evidence_service
from app.modules.innovation.evidence_schemas import EvidenceIntentCreate
from app.modules.innovation.models import OwnerProfile
from app.modules.innovation.review_operations_router import (
    get_review_operations_service,
)
from app.modules.innovation.review_schemas import (
    ReviewVersionCommand,
    SavedRevisionCommand,
)
from app.modules.innovation.review_service import AchievementReviewService
from tests.integration.innovation.test_achievement_evidence import PDF, world
from tests.integration.innovation.test_achievement_evidence import clock as clock
from tests.integration.innovation.test_achievement_review_operations import account

pytestmark = pytest.mark.integration

if (
    os.environ.get("CQ_S3_SMOKE") != "1"
    or os.environ.get("CQ_EVIDENCE_SCAN_SMOKE") != "1"
):
    pytest.skip(
        "real evidence composition requires both S3 and scanner smoke flags",
        allow_module_level=True,
    )


async def test_real_provider_checks_uploaded_bytes_and_write_once(db_session, clock):
    owner, parent, achievement, _ = await world(db_session, clock)
    parent.summary, parent.direction, parent.stage, parent.team_status = (
        "真实存储组合烟测",
        "教育",
        "原型",
        "两人团队",
    )
    achievement.description = "可运行原型及真实证明"
    db_session.add(
        OwnerProfile(
            user_id=owner.id,
            name="烟测本人",
            student_no="000005",
            major="专业",
            grade="2026",
        )
    )
    await db_session.flush()
    actor = Actor(user_id=owner.id, role=Role.STUDENT)
    service = get_evidence_service(clock)
    args = {"actor": actor, "project_id": parent.id, "achievement_id": achievement.id}
    signed, _ = await service.create_intent(
        db_session,
        **args,
        payload=EvidenceIntentCreate(
            request_id=uuid4(), content_type="application/pdf", size=len(PDF)
        ),
    )
    # Do not print signed URLs on assertion failure; they are credentials.
    from urllib.parse import unquote, urlsplit

    key = unquote(urlsplit(signed.upload_url).path).split("/", 2)[2]
    real_storage = S3ObjectStorage(get_settings(), clock=clock)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            first = await client.put(
                signed.upload_url, headers=signed.client_headers, content=PDF
            )
            assert first.status_code == 200
            repeated = await client.put(
                signed.upload_url, headers=signed.client_headers, content=PDF
            )
            assert repeated.status_code == 412
        checked = await service.complete(
            db_session, **args, evidence_id=signed.evidence.id
        )
        assert checked.state == "READY", checked.failure_code
        content = await service.read_content(db_session, **args, evidence_id=checked.id)
        assert content.content == PDF
        workflow_service = AchievementReviewService(clock=clock)
        workflow = await workflow_service.workflow(db_session, **args)
        submitted = await workflow_service.submit(
            db_session,
            **args,
            payload=SavedRevisionCommand(
                request_id=uuid4(),
                workflow_version=workflow.version,
                project_version=parent.version,
                achievement_version=achievement.version,
                evidence_ids=[checked.id],
            ),
        )
        assert submitted.review_case is not None
        operator, _ = await account(db_session, clock, operator=True)
        review_service = get_review_operations_service(clock)
        review_args = {
            "actor": Actor(user_id=operator.id, role=Role.STUDENT),
            "case_id": submitted.review_case.id,
        }
        claimed = await review_service.claim(
            db_session,
            **review_args,
            payload=ReviewVersionCommand(version=submitted.review_case.version),
        )
        detail = await review_service.detail(db_session, **review_args)
        assert (
            detail.case.id == claimed.id
            and detail.owner_profile["student_no"] == "000005"
        )
        operator_content = await review_service.read_content(
            db_session, **review_args, evidence_id=checked.id
        )
        assert operator_content.content == PDF
        with pytest.raises(ValueError, match="size limit"):
            await asyncio.to_thread(
                real_storage.read_bounded_object, object_key=key, max_bytes=len(PDF) - 1
            )
    finally:
        await asyncio.to_thread(real_storage.delete_object, object_key=key)


@pytest.mark.parametrize("wrong", ["type", "size"])
async def test_evidence_signature_pins_type_and_size(clock, wrong):
    from datetime import timedelta

    storage = S3ObjectStorage(get_settings(), clock=clock)
    signed = storage.create_evidence_upload_url(
        achievement_id=uuid4(),
        content_type="application/pdf",
        content_length=len(PDF),
        expires_in=timedelta(minutes=5),
    )
    headers = dict(signed.client_headers)
    body = PDF
    if wrong == "type":
        headers["Content-Type"] = "image/png"
    else:
        body += b"x"
    async with httpx.AsyncClient(timeout=15) as client:
        result = await client.put(signed.url, headers=headers, content=body)
        assert result.status_code == 403
    assert (
        await asyncio.to_thread(storage.head_object, object_key=signed.object_key)
        is None
    )
