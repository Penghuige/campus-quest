# backend/tests/unit/integrations/test_fakes.py
"""Deterministic fake behavior for the integration adapter ports.

Covers the recording contract from the task brief (exact-delivery
assertions via list equality), the object-storage fake's key/metadata
semantics (including the retention ``delete_object`` contract from
spec §27), and raise-on-demand failure programming used by later worker
tests to simulate provider outages.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.clock import FrozenClock
from app.integrations.email import SentEmail
from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.integrations.object_storage import ObjectHead
from app.integrations.rate_limit import RateLimitExceededError
from app.integrations.sms import SentSms
from tests.fakes.integrations import (
    FakeEmailSender,
    FakeObjectStorage,
    FakeRateLimiter,
    FakeSmsSender,
    RateLimitCheck,
)

CLAIM_ID = UUID("12345678-1234-5678-1234-567812345678")
FROZEN_NOW = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
TTL = timedelta(minutes=5)


def test_fake_sms_records_exact_delivery():
    sms = FakeSmsSender()
    sms.send(to="+8613800000000", template="deadline_4h", variables={"task": "T"})
    assert sms.messages == [
        SentSms(to="+8613800000000", template="deadline_4h", variables={"task": "T"})
    ]


def test_fake_sms_records_each_send_separately() -> None:
    sms = FakeSmsSender()
    sms.send(to="+8613800000000", template="deadline_4h", variables={"task": "A"})
    sms.send(to="+8613900000000", template="deadline_24h", variables={"task": "B"})
    assert [m.to for m in sms.messages] == ["+8613800000000", "+8613900000000"]
    assert [m.template for m in sms.messages] == ["deadline_4h", "deadline_24h"]


def test_fake_sms_snapshots_variables_against_later_mutation() -> None:
    sms = FakeSmsSender()
    variables: dict[str, str] = {"task": "T"}
    sms.send(to="+8613800000000", template="deadline_4h", variables=variables)
    variables["task"] = "MUTATED"
    assert sms.messages == [
        SentSms(to="+8613800000000", template="deadline_4h", variables={"task": "T"})
    ]


def test_fake_email_records_exact_delivery() -> None:
    email = FakeEmailSender()
    email.send(
        to="student@campus.example.edu",
        template="revision_required",
        variables={"claim": "c1"},
    )
    assert email.messages == [
        SentEmail(
            to="student@campus.example.edu",
            template="revision_required",
            variables={"claim": "c1"},
        )
    ]


def test_upload_url_uses_server_generated_key_under_claim_prefix() -> None:
    storage = FakeObjectStorage()
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    assert url.object_key.startswith(f"submissions/{CLAIM_ID}/")
    # The key tail is server-generated random material, not caller input.
    UUID(url.object_key.rsplit("/", 1)[1])
    # Two intents for the same claim never collide on keys.
    second = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    assert second.object_key != url.object_key
    assert storage.upload_urls == [url, second]


def test_upload_url_expiry_comes_from_injected_clock() -> None:
    storage = FakeObjectStorage(clock=FrozenClock(FROZEN_NOW))
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    assert url.expires_at == FROZEN_NOW + TTL


def test_head_object_returns_pinned_metadata_after_simulated_upload() -> None:
    storage = FakeObjectStorage()
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    storage.put_object(object_key=url.object_key, size=2048)
    assert storage.head_object(object_key=url.object_key) == ObjectHead(
        object_key=url.object_key, size=2048, content_type="text/csv"
    )


def test_head_object_returns_none_for_missing_object() -> None:
    storage = FakeObjectStorage()
    assert storage.head_object(object_key="submissions/does/not-exist") is None


def test_head_object_returns_none_after_upload_never_completed() -> None:
    storage = FakeObjectStorage()
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    # Intent issued but the client never PUT the object.
    assert storage.head_object(object_key=url.object_key) is None


def test_download_url_is_short_lived_and_recorded() -> None:
    storage = FakeObjectStorage(clock=FrozenClock(FROZEN_NOW))
    url = storage.create_download_url(
        object_key=f"submissions/{CLAIM_ID}/abc", expires_in=TTL
    )
    assert url.object_key in url.url
    assert url.expires_at == FROZEN_NOW + TTL
    assert storage.download_urls == [url]


def test_download_url_signs_without_existence_check() -> None:
    # Providers sign GETs without checking existence; the caller must
    # verify business authorization before generating the URL.
    storage = FakeObjectStorage()
    missing = f"submissions/{CLAIM_ID}/missing"
    url = storage.create_download_url(object_key=missing, expires_in=TTL)
    assert storage.head_object(object_key=missing) is None
    assert missing in url.url


def test_fake_sms_programmed_failure_raises_and_records_nothing() -> None:
    sms = FakeSmsSender()
    sms.fail_with(UnknownOutcomeError("provider timeout"), times=3)
    for _ in range(3):
        with pytest.raises(UnknownOutcomeError):
            sms.send(to="+8613800000000", template="deadline_4h", variables={})
    assert sms.messages == []
    assert sms.failures == []
    # Programmed failures are exhausted; delivery proceeds.
    sms.send(to="+8613800000000", template="deadline_4h", variables={})
    assert len(sms.messages) == 1


def test_fake_email_programmed_permanent_rejection() -> None:
    email = FakeEmailSender()
    email.fail_with(PermanentProviderError("hard bounce"))
    with pytest.raises(PermanentProviderError):
        email.send(
            to="bad@campus.example.edu", template="revision_required", variables={}
        )
    assert email.messages == []


@pytest.mark.parametrize(
    "error",
    [
        TemporaryProviderError("throttled"),
        PermanentProviderError("rejected"),
        UnknownOutcomeError("timeout"),
    ],
)
def test_fake_failure_taxonomy_passes_through_unchanged(
    error: Exception,
) -> None:
    sms = FakeSmsSender()
    sms.fail_with(error)
    with pytest.raises(type(error)) as raised:
        sms.send(to="+8613800000000", template="deadline_4h", variables={})
    assert raised.value is error


def test_fake_object_storage_programmed_failure_blocks_every_operation() -> None:
    storage = FakeObjectStorage()
    storage.fail_with(TemporaryProviderError("s3 unavailable"), times=3)
    with pytest.raises(TemporaryProviderError):
        storage.create_upload_url(
            claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
        )
    with pytest.raises(TemporaryProviderError):
        storage.head_object(object_key=f"submissions/{CLAIM_ID}/abc")
    with pytest.raises(TemporaryProviderError):
        storage.create_download_url(
            object_key=f"submissions/{CLAIM_ID}/abc", expires_in=TTL
        )
    assert storage.upload_urls == []
    assert storage.download_urls == []
    assert storage.objects == {}


def test_fake_object_storage_recovers_after_programmed_outage() -> None:
    storage = FakeObjectStorage()
    storage.fail_with(TemporaryProviderError("s3 unavailable"))
    with pytest.raises(TemporaryProviderError):
        storage.create_upload_url(
            claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
        )
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    storage.put_object(object_key=url.object_key, size=10)
    assert storage.head_object(object_key=url.object_key) is not None


def test_delete_object_removes_stored_object_and_records_key() -> None:
    storage = FakeObjectStorage()
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    storage.put_object(object_key=url.object_key, size=2048)

    storage.delete_object(object_key=url.object_key)

    assert storage.head_object(object_key=url.object_key) is None
    assert storage.deleted_keys == [url.object_key]


def test_delete_object_missing_raises_file_not_found_error() -> None:
    # §27 contract: a missing object is a typed signal the retention
    # cleanup worker branches on, not a provider failure.
    storage = FakeObjectStorage()
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    with pytest.raises(FileNotFoundError):
        storage.delete_object(object_key=url.object_key)
    assert storage.deleted_keys == []


def test_delete_object_twice_raises_on_second_call() -> None:
    storage = FakeObjectStorage()
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    storage.put_object(object_key=url.object_key, size=10)
    storage.delete_object(object_key=url.object_key)

    with pytest.raises(FileNotFoundError):
        storage.delete_object(object_key=url.object_key)

    assert storage.deleted_keys == [url.object_key]


def test_delete_object_programmed_failure_deletes_nothing() -> None:
    storage = FakeObjectStorage()
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    storage.put_object(object_key=url.object_key, size=10)
    storage.fail_with(PermanentProviderError("access denied"))

    with pytest.raises(PermanentProviderError):
        storage.delete_object(object_key=url.object_key)

    assert storage.head_object(object_key=url.object_key) is not None
    assert storage.deleted_keys == []


def test_fake_rate_limiter_records_exact_checks() -> None:
    limiter = FakeRateLimiter()

    asyncio.run(
        limiter.check(
            bucket="auth:login", identifier="alice", limit=10, window_seconds=300
        )
    )

    assert limiter.checks == [
        RateLimitCheck(
            bucket="auth:login", identifier="alice", limit=10, window_seconds=300
        )
    ]
    assert limiter.checks_for("auth:login")[0].identifier == "alice"
    assert limiter.checks_for("auth:otp-send") == []


def test_fake_rate_limiter_programmed_bucket_raises() -> None:
    limiter = FakeRateLimiter()
    limiter.fail_on("auth:otp-send")

    with pytest.raises(RateLimitExceededError) as exc_info:
        asyncio.run(
            limiter.check(
                bucket="auth:otp-send",
                identifier="+8613700137001",
                limit=5,
                window_seconds=3600,
            )
        )
    assert exc_info.value.bucket == "auth:otp-send"

    # Other buckets stay allowed; the failing call was still recorded.
    asyncio.run(
        limiter.check(
            bucket="auth:login", identifier="alice", limit=10, window_seconds=300
        )
    )
    assert len(limiter.checks) == 2
