"""Talent files belong to ordinary students and require separate sharing."""

import importlib.util
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import get_db_session
from app.integrations.evidence_scanner import (
    EvidenceCheckUnavailableError,
    EvidenceThreatError,
)
from app.main import create_app
from app.modules.identity.dependencies import get_business_clock
from tests.fakes.integrations import FakeObjectStorage
from tests.integration.innovation.talent_support import BASE, command, saved
from tests.integration.innovation.test_achievement_evidence import PDF, Scanner

pytestmark = pytest.mark.integration
FILES = BASE + "/files"


class AdvancingClock:
    def __init__(self):
        self.current = datetime.now(UTC)

    def now(self):
        return self.current

    def advance(self, duration):
        self.current += duration


@pytest.fixture
def clock():
    return AdvancingClock()


@pytest.fixture
def storage(clock):
    return FakeObjectStorage(clock=clock)


@pytest.fixture
def scanner():
    return Scanner()


@pytest.fixture
async def client(db_session, clock, storage, scanner):
    app = create_app()

    async def db():
        yield db_session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_business_clock] = lambda: clock
    if importlib.util.find_spec("app.modules.innovation.talent_file_router"):
        from app.modules.innovation.talent_file_router import get_talent_file_service
        from app.modules.innovation.talent_file_service import TalentFileService

        app.dependency_overrides[get_talent_file_service] = lambda: TalentFileService(
            clock=clock, storage=storage, scanner=scanner
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as result:
        yield result


def intent(**changes):
    return {
        "request_id": str(uuid4()),
        "name": "模拟作品.pdf",
        "content_type": "application/pdf",
        "size": len(PDF),
        **changes,
    }


async def upload(client, headers, storage, *, payload=None, content=PDF):
    response = await client.post(FILES, headers=headers, json=payload or intent())
    assert response.status_code == 201, response.text
    key = storage.upload_urls[-1].object_key
    storage.put_object(object_key=key, content=content)
    return response.json()


async def current(client, headers):
    response = await client.get(BASE, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["profile"]


async def test_unqualified_student_upload_is_private_until_selected(
    client, db_session, clock, storage
):
    _, headers, row, _ = await saved(client, db_session, clock)
    created = await client.post(FILES, headers=headers, json=intent())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["file"]["state"] == "PENDING"
    assert body["client_headers"]["If-None-Match"] == "*"
    assert body["pinned_content_length"] == len(PDF)
    assert body["file"]["id"] not in row["public_file_ids"]
    public = await client.post(BASE + "/publish", headers=headers, json=command(row))
    assert public.status_code == 200
    row = public.json()["current"]
    draft = await client.put(
        BASE,
        headers=headers,
        json={
            **command(row),
            "fields": {
                **row["fields"],
                "file_ids": [body["file"]["id"]],
            },
        },
    )
    assert draft.status_code == 200
    denied = await client.post(
        BASE + "/publish", headers=headers, json=command(draft.json()["current"])
    )
    assert denied.status_code == 409
    storage.put_object(
        object_key=storage.upload_urls[-1].object_key,
        content=PDF,
    )
    checked = await client.post(
        FILES + f"/{body['file']['id']}/complete", headers=headers
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["state"] == "READY"
    row = await current(client, headers)
    assert row["public_file_ids"] == []
    result = await client.post(BASE + "/publish", headers=headers, json=command(row))
    assert result.status_code == 200
    assert result.json()["current"]["public_file_ids"] == [body["file"]["id"]]


async def test_creation_replays_original_upload_contract_and_rejects_changed_payload(
    client, db_session, clock, storage
):
    _, headers, _, _ = await saved(client, db_session, clock)
    payload = intent()
    first = await client.post(FILES, headers=headers, json=payload)
    assert first.status_code == 201
    clock.advance(timedelta(minutes=16))
    replay = await client.post(FILES, headers=headers, json=payload)
    assert replay.status_code == 200
    assert first.json()["upload_url"] == replay.json()["upload_url"]
    assert first.json()["expires_at"] == replay.json()["expires_at"]
    assert len(storage.upload_urls) == 1
    changed = await client.post(
        FILES, headers=headers, json={**payload, "name": "其他作品.pdf"}
    )
    assert changed.status_code == 409


async def test_uploaded_bytes_can_be_checked_after_credential_expiry(
    client, db_session, clock, storage
):
    _, headers, _, _ = await saved(client, db_session, clock)
    created = await upload(client, headers, storage)
    clock.advance(timedelta(minutes=16))
    response = await client.post(
        FILES + f"/{created['file']['id']}/complete", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "READY"


async def ready(client, headers, storage, **changes):
    uploaded = await upload(client, headers, storage, payload=intent(**changes))
    file_id = uploaded["file"]["id"]
    response = await client.post(FILES + f"/{file_id}/complete", headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "READY"
    return response.json()


async def select_and_publish(client, headers, file_ids):
    row = await current(client, headers)
    draft = await client.put(
        BASE,
        headers=headers,
        json={**command(row), "fields": {**row["fields"], "file_ids": file_ids}},
    )
    assert draft.status_code == 200, draft.text
    public = await client.post(
        BASE + "/publish", headers=headers, json=command(draft.json()["current"])
    )
    assert public.status_code == 200, public.text
    return public.json()["current"]


async def test_file_list_and_content_are_private_and_attachment_only(
    client, db_session, clock, storage
):
    _, headers, _, _ = await saved(client, db_session, clock)
    file = await ready(client, headers, storage, name="C:\\fake\\作品\r\n.pdf")
    listed = await client.get(FILES, headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["name"] == "作品.pdf"
    assert "upload_url" not in listed.text and "object_key" not in listed.text
    download = await client.get(FILES + f"/{file['id']}/content", headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == PDF
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"


async def test_public_file_requires_revocation_and_cannot_be_published_again(
    client, db_session, clock, storage
):
    _, headers, _, _ = await saved(client, db_session, clock)
    file = await ready(client, headers, storage)
    row = await select_and_publish(client, headers, [file["id"]])
    path = FILES + f"/{file['id']}"
    refused = await client.post(path + "/remove", headers=headers, json=command(row))
    assert refused.status_code == 409
    revoke_payload = command(row)
    response = await client.post(path + "/revoke", headers=headers, json=revoke_payload)
    assert response.status_code == 200, response.text
    assert response.json()["current"]["visibility"] == "VISIBLE"
    assert response.json()["current"]["public_file_ids"] == []
    assert response.json()["current"]["fields"]["file_ids"] == []
    replay = await client.post(path + "/revoke", headers=headers, json=revoke_payload)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert (await client.get(path + "/content", headers=headers)).content == PDF
    row = await current(client, headers)
    draft = await client.put(
        BASE,
        headers=headers,
        json={**command(row), "fields": {**row["fields"], "file_ids": [file["id"]]}},
    )
    assert draft.status_code == 200
    denied = await client.post(
        BASE + "/publish", headers=headers, json=command(draft.json()["current"])
    )
    assert denied.status_code == 409


async def test_remove_invalidates_old_upload_record_and_owner_content(
    client, db_session, clock, storage
):
    _, headers, row, _ = await saved(client, db_session, clock)
    payload = intent()
    uploaded = await upload(client, headers, storage, payload=payload)
    path = FILES + f"/{uploaded['file']['id']}"
    removed = await client.post(path + "/remove", headers=headers, json=command(row))
    assert removed.status_code == 200, removed.text
    assert (await client.post(path + "/complete", headers=headers)).status_code == 404
    assert (await client.get(path + "/content", headers=headers)).status_code == 404
    assert (await client.post(FILES, headers=headers, json=payload)).status_code == 409


@pytest.mark.parametrize(
    "failure,state,code",
    [
        (EvidenceCheckUnavailableError("unavailable"), "PENDING", "CHECK_UNAVAILABLE"),
        (EvidenceThreatError("threat"), "REJECTED", "UNSAFE_CONTENT"),
    ],
)
async def test_scanner_failures_never_authorize_content(
    client, db_session, clock, storage, scanner, failure, state, code
):
    _, headers, _, _ = await saved(client, db_session, clock)
    uploaded = await upload(client, headers, storage)
    scanner.failure = failure
    path = FILES + f"/{uploaded['file']['id']}"
    checked = await client.post(path + "/complete", headers=headers)
    assert checked.status_code == 200
    assert checked.json()["state"] == state and checked.json()["failure_code"] == code
    assert (await client.get(path + "/content", headers=headers)).status_code == 409


@pytest.mark.parametrize("content", [b"", b"MZ" + b"x" * (len(PDF) - 2)])
async def test_invalid_bytes_cannot_become_ready(
    client, db_session, clock, storage, content
):
    _, headers, _, _ = await saved(client, db_session, clock)
    uploaded = await upload(client, headers, storage, content=content)
    checked = await client.post(
        FILES + f"/{uploaded['file']['id']}/complete", headers=headers
    )
    assert checked.status_code == 200 and checked.json()["state"] == "REJECTED"
    assert checked.json()["failure_code"] == "INVALID_CONTENT"


async def test_missing_upload_and_unavailable_scanner_can_retry_without_false_success(
    client, db_session, clock, storage, scanner
):
    _, headers, _, _ = await saved(client, db_session, clock)
    created = await client.post(FILES, headers=headers, json=intent())
    path = FILES + f"/{created.json()['file']['id']}"
    missing = await client.post(path + "/complete", headers=headers)
    assert missing.json()["state"] == "PENDING"
    assert missing.json()["failure_code"] == "MISSING_OBJECT"
    storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
    scanner.failure = EvidenceCheckUnavailableError("unavailable")
    unavailable = await client.post(path + "/complete", headers=headers)
    assert unavailable.json()["state"] == "PENDING"
    scanner.failure = None
    checked = await client.post(path + "/complete", headers=headers)
    assert checked.status_code == 200 and checked.json()["state"] == "READY"
    assert checked.json()["failure_code"] is None


async def test_foreign_student_cannot_read_check_or_remove_private_file(
    client, db_session, clock, storage
):
    _, headers, _, _ = await saved(client, db_session, clock)
    file = await ready(client, headers, storage)
    _, other, row, _ = await saved(client, db_session, clock)
    path = FILES + f"/{file['id']}"
    for suffix in ["/content", "/complete", "/remove", "/revoke"]:
        if suffix == "/content":
            response = await client.get(path + suffix, headers=other)
        elif suffix == "/complete":
            response = await client.post(path + suffix, headers=other)
        else:
            response = await client.post(
                path + suffix, headers=other, json=command(row)
            )
        assert response.status_code == 404
        assert response.headers["cache-control"] == "private, no-store"


async def test_file_audit_failure_rolls_back_intent_and_never_returns_content(
    client, db_session, clock, storage, scanner
):
    from uuid import UUID

    from sqlalchemy import func, select

    from app.modules.audit.context import AuditContext
    from app.modules.audit.service import AuditLogWriter
    from app.modules.identity.enums import Role
    from app.modules.identity.events import Actor
    from app.modules.innovation.talent_file_schemas import TalentFileIntentCreate
    from app.modules.innovation.talent_file_service import TalentFileService
    from app.modules.innovation.talent_models import TalentActionReceipt, TalentFile

    user, headers, row, _ = await saved(client, db_session, clock)
    file = await ready(client, headers, storage)

    class UnavailableAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    service = TalentFileService(
        clock=clock, storage=storage, scanner=scanner, audit=UnavailableAudit()
    )
    actor = Actor(user.id, Role.STUDENT)
    payload = TalentFileIntentCreate(**intent())
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await service.create_intent(
            db_session, actor=actor, payload=payload, context=AuditContext(None, None)
        )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TalentFile)
            .where(TalentFile.creation_request_id == payload.request_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TalentActionReceipt)
            .where(TalentActionReceipt.request_id == payload.request_id)
        )
        == 0
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await service.read_owned_content(
            db_session,
            actor=actor,
            file_id=UUID(file["id"]),
            context=AuditContext(None, None),
        )


async def test_five_public_files_can_all_be_replaced_before_closing_old_version(
    client, db_session, clock, storage
):
    _, headers, _, _ = await saved(client, db_session, clock)
    old = [await ready(client, headers, storage) for _ in range(5)]
    row = await select_and_publish(client, headers, [f["id"] for f in old])
    new = [await ready(client, headers, storage) for _ in range(5)]
    assert (await current(client, headers))["public_file_ids"] == row["public_file_ids"]
    refused = await client.post(FILES, headers=headers, json=intent())
    assert refused.status_code == 409
    next_row = await select_and_publish(client, headers, [f["id"] for f in new])
    assert next_row["public_file_ids"] == [f["id"] for f in new]
    assert next_row["public_version"] == row["public_version"] + 1


async def test_file_metadata_is_immutable_in_postgres(
    client, db_session, clock, storage
):
    _, headers, _, _ = await saved(client, db_session, clock)
    file = await ready(client, headers, storage)
    with pytest.raises(DBAPIError, match="talent file metadata is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE ie_talent_files SET object_key='different/key' WHERE id=:id"
                ),
                {"id": file["id"]},
            )


@pytest.mark.parametrize("column", ["removed_at", "revoked_at"])
async def test_file_tombstone_cannot_be_cleared_in_postgres(
    client, db_session, clock, storage, column
):
    _, headers, _, _ = await saved(client, db_session, clock)
    uploaded = await upload(client, headers, storage)
    file_id = uploaded["file"]["id"]
    await db_session.execute(
        text(f"UPDATE ie_talent_files SET {column}=:now WHERE id=:id"),
        {"now": clock.now(), "id": file_id},
    )
    with pytest.raises(DBAPIError, match="talent file tombstone is permanent"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(f"UPDATE ie_talent_files SET {column}=NULL WHERE id=:id"),
                {"id": file_id},
            )
