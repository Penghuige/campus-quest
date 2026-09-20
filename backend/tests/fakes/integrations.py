# backend/tests/fakes/integrations.py
"""Deterministic in-memory fakes for the integration adapter ports.

TEST-ONLY code: no provider SDK imports, no network. The fakes record
every accepted call for exact-delivery assertions and support
raise-on-demand failure programming so worker tests can simulate provider
outages with the same taxonomy real adapters raise
(docs/quality/backend-engineering.md §13).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.core.clock import Clock, SystemClock
from app.integrations.email import SentEmail
from app.integrations.object_storage import DownloadUrl, ObjectHead, UploadUrl
from app.integrations.rate_limit import RateLimitExceededError
from app.integrations.sms import SentSms

_FAKE_HOST = "https://fake-object-storage.test"


@dataclass(frozen=True)
class RateLimitCheck:
    """One recorded `RateLimiter.check` call, exactly as invoked."""

    bucket: str
    identifier: str
    limit: int
    window_seconds: int


class FakeRateLimiter:
    """In-memory `RateLimiter` recording every check.

    Deterministic and permission-free: every call is appended to ``checks``
    for exact-identifier assertions, and buckets named in ``failing_buckets``
    (programmed via `fail_on`) raise `RateLimitExceededError` so tests drive
    the 429 path without a real Redis window.
    """

    def __init__(self) -> None:
        self.checks: list[RateLimitCheck] = []
        self.failing_buckets: set[str] = set()

    def fail_on(self, bucket: str) -> None:
        """Program every future check of ``bucket`` to exceed the limit."""
        self.failing_buckets.add(bucket)

    async def check(
        self, *, bucket: str, identifier: str, limit: int, window_seconds: int
    ) -> None:
        self.checks.append(
            RateLimitCheck(
                bucket=bucket,
                identifier=identifier,
                limit=limit,
                window_seconds=window_seconds,
            )
        )
        if bucket in self.failing_buckets:
            raise RateLimitExceededError(bucket)

    def checks_for(self, bucket: str) -> list[RateLimitCheck]:
        return [check for check in self.checks if check.bucket == bucket]


class _FailureProgrammable:
    """Shared raise-on-demand behavior for outage simulation.

    `failures` holds programmed errors FIFO; each port call consumes one
    before doing its normal work, so a failed call is never recorded as
    delivered. Programmed errors are raised as-is, preserving the
    temporary/permanent/unknown taxonomy.
    """

    failures: list[Exception]

    def fail_with(self, error: Exception, *, times: int = 1) -> None:
        """Program the next `times` port calls to raise `error`."""
        if times < 1:
            raise ValueError("times must be >= 1")
        self.failures.extend([error] * times)

    def _raise_if_programmed(self) -> None:
        if self.failures:
            raise self.failures.pop(0)


class FakeSmsSender(_FailureProgrammable):
    """In-memory `SmsSender` recording exact deliveries."""

    def __init__(self) -> None:
        self.messages: list[SentSms] = []
        self.failures: list[Exception] = []

    def send(self, *, to: str, template: str, variables: Mapping[str, Any]) -> None:
        self._raise_if_programmed()
        # Snapshot so later caller-side mutation cannot rewrite history.
        self.messages.append(
            SentSms(to=to, template=template, variables=dict(variables))
        )


class FakeEmailSender(_FailureProgrammable):
    """In-memory `EmailSender` recording exact deliveries."""

    def __init__(self) -> None:
        self.messages: list[SentEmail] = []
        self.failures: list[Exception] = []

    def send(self, *, to: str, template: str, variables: Mapping[str, Any]) -> None:
        self._raise_if_programmed()
        self.messages.append(
            SentEmail(to=to, template=template, variables=dict(variables))
        )


class FakeObjectStorage(_FailureProgrammable):
    """In-memory `ObjectStorage`.

    `create_upload_url` issues server-generated keys and pins the declared
    content type; `put_object` simulates the client completing the
    presigned PUT; `head_object` then reports the pinned metadata; and
    `download_to_file` replays the PUT content for worker-side reads
    (a missing key is `FileNotFoundError`, matching the port contract).
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self.failures: list[Exception] = []
        self.upload_urls: list[UploadUrl] = []
        self.download_urls: list[DownloadUrl] = []
        self.objects: dict[str, ObjectHead] = {}
        self.downloads: list[str] = []
        self._pinned_content_types: dict[str, str] = {}
        self._contents: dict[str, bytes] = {}

    def create_upload_url(
        self, *, claim_id: UUID, content_type: str, expires_in: timedelta
    ) -> UploadUrl:
        self._raise_if_programmed()
        object_key = f"submissions/{claim_id}/{uuid4()}"
        url = UploadUrl(
            object_key=object_key,
            url=f"{_FAKE_HOST}/upload/{object_key}",
            expires_at=self._clock.now() + expires_in,
        )
        self.upload_urls.append(url)
        self._pinned_content_types[url.object_key] = content_type
        return url

    def head_object(self, *, object_key: str) -> ObjectHead | None:
        self._raise_if_programmed()
        return self.objects.get(object_key)

    def create_download_url(
        self, *, object_key: str, expires_in: timedelta
    ) -> DownloadUrl:
        # Signs without checking existence, like real providers.
        self._raise_if_programmed()
        url = DownloadUrl(
            object_key=object_key,
            url=f"{_FAKE_HOST}/download/{object_key}",
            expires_at=self._clock.now() + expires_in,
        )
        self.download_urls.append(url)
        return url

    def put_object(
        self,
        *,
        object_key: str,
        size: int | None = None,
        content: bytes = b"",
    ) -> None:
        """Simulate the client completing the presigned PUT for `object_key`.

        Test-side helper, not part of the port: the upload itself does not
        flow through the adapter. `content` is the stored byte payload
        (what `download_to_file` later replays); `size` defaults to
        `len(content)` and remains independently overridable for the
        finalize size-mismatch paths. Content type comes from the pinned
        value on the issued upload URL.
        """
        pinned = self._pinned_content_types.get(object_key)
        if pinned is None:
            raise ValueError(f"no upload URL was issued for {object_key!r}")
        self.objects[object_key] = ObjectHead(
            object_key=object_key,
            size=len(content) if size is None else size,
            content_type=pinned,
        )
        self._contents[object_key] = content

    def download_to_file(self, *, object_key: str, destination: Path) -> None:
        # Worker-side read path: replay the PUT content byte-identically.
        self._raise_if_programmed()
        if object_key not in self.objects:
            raise FileNotFoundError(f"no object under key {object_key!r}")
        self.downloads.append(object_key)
        destination.write_bytes(self._contents.get(object_key, b""))
