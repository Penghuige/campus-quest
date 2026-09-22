# backend/app/integrations/object_storage_s3.py
"""Real S3/MinIO adapter for the ``ObjectStorage`` port (hardening P0-1).

WHY this module exists: the port (``integrations/object_storage.py``)
was protocol-only with every composition root raising
``NotImplementedError`` — a deployment's upload-intent request and every
validation job failed until an adapter landed. This is that adapter:
boto3's SYNCHRONOUS client. aioboto3 is deliberately not introduced, so
the SYNCHRONOUS call boundary is the caller's concern: the Celery task
body is its own thread, while FastAPI request handlers run on an event
loop and MUST move blocking calls (``head_object`` in the finalize flow)
across a thread boundary — ``upload_service.finalize_upload`` does that
with ``asyncio.to_thread``. Presigning (``create_upload_url`` /
``create_download_url``) is local computation with no provider I/O and
stays inline.

Port contract, implemented clause by clause (docstring there is the
frozen source of truth):

- Keys are ALWAYS server-generated ``submissions/{claim_id}/{uuid4()}``;
  no caller path fragment ever reaches a key (interfaces.md Adapter
  Ports; backend-engineering §16).
- ``create_upload_url`` presigns a WRITE-ONCE, size-pinned PUT (S3
  hardening P0/P1, proven live against the pinned MinIO
  RELEASE.2025-09-07 — the same image CI runs — see
  tests/integration/test_object_storage_smoke.py):
  * ``IfNoneMatch="*"`` is signed as a header, so a client PUT whose
    key already holds an object is rejected 412 and an upload whose
    PUT omits the header is rejected — the URL is good for exactly
    one successful PUT, forever (the condition outlives the TTL;
    nothing can replace a stored object through any presigned URL).
  * the declared Content-Type is signed (a PUT with a different
    Content-Type is rejected 403);
  * the declared ``content_length`` is signed as the Content-Length
    header (a PUT whose body length differs is rejected 403 — the
    client cannot frame the request with any other length without
    breaking the signature). ``expires_at`` comes from the injected
    ``Clock`` (``SystemClock`` by default), mirroring the fake;
  * the signing contract is returned, not documented elsewhere:
    ``client_headers`` (If-None-Match, Content-Type — no
    Content-Length, a browser-forbidden header) and
    ``pinned_content_length`` describe exactly what was signed, and the
    application echoes them to the client verbatim (hardening P4c:
    single-source contract, no independent reconstruction).
- ``head_object`` maps a provider 404 to ``None``; ``NoSuchBucket`` is
  NOT a missing-object answer but a broken deployment configuration and
  fails closed as ``PermanentProviderError``.
- ``create_download_url`` signs blind, exactly like the provider: no
  existence check (the port's stated contract; authorization is the
  caller's job — query_service verifies ownership before signing).
- ``download_to_file`` streams server-side into ``destination``; a
  missing key is ``FileNotFoundError``.
- ``delete_object`` HEADs first and raises ``FileNotFoundError`` for a
  missing object. S3/MinIO's plain DeleteObject answers 204 even for a
  nonexistent key, which would VIOLATE the port's §27 contract (the
  second delete of an absent object must raise — the retention worker
  distinguishes idempotent success from a reconcile on exactly that
  signal), so the existence check is the adapter's job, not the
  provider's. A key vanishing between HEAD and DELETE deletes as a
  provider no-op — the next cycle's HEAD is the reconcile.

Failure taxonomy (``integrations.errors``): provider 5xx/throttling
and pre-request connection failures are ``TemporaryProviderError``
(bounded retry safe — botocore's own standard-mode retries already
absorb most of these; what escapes is genuinely persistent), a read
timeout (request sent, answer never came — the side effect may have
happened) is ``UnknownOutcomeError``, and definitive 4xx rejections or
SDK configuration errors are ``PermanentProviderError``. All three,
plus the ``FileNotFoundError`` (an ``OSError``), are retry classes for
the validation job's bounded policy.

Fail-closed configuration chain (G5): every s3_* field on
``Settings`` is required, so a missing endpoint/bucket/credential
fails at ``Settings`` construction — this module contains zero
credential literals and no environment fallback: what it reads is
exactly the ``Settings`` fields handed to it (the signing region is
``Settings.s3_region``; its default matches MinIO's ignore-the-region
behavior, and a real AWS deployment sets the bucket region). There is
no in-memory or no-op fallback anywhere in this chain.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import boto3
from botocore.client import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.integrations.object_storage import DownloadUrl, ObjectHead, UploadUrl

#: ClientError codes that are provider-side transient (retry-safe).
_TEMPORARY_ERROR_CODES = frozenset(
    {
        "InternalError",
        "InternalServerError",
        "RequestTimeout",
        "RequestTimeout2",
        "ServiceUnavailable",
        "SlowDown",
        "ThrottlingException",
    }
)

#: ClientError codes proving the OBJECT is absent (a normal result for
#: head/missing-key paths), distinct from a broken bucket.
_MISSING_OBJECT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

#: Bounded provider calls (final-review P0, Option A): the per-attempt
#: connect/read caps and the TOTAL attempts per API call live in
#: ``Settings`` (``s3_connect_timeout_seconds`` / ``s3_read_timeout_seconds``
#: / ``s3_delete_total_attempts``) — the single source of truth. The
#: Settings model validator machine-checks that the cleanup lease
#: strictly exceeds the derived worst-case delete budget
#: (``Settings.s3_worst_case_delete_budget_seconds``: total attempts x
#: (connect + read) x the delete path's HEAD+DELETE pair + a backoff
#: margin), so a lease can never expire while a predecessor worker's
#: already-sent DELETE could still be in flight. This bound is the
#: SECOND line of defense behind the fencing token: the token fences
#: late DB writes, the bound fences the stale EXTERNAL side effect.


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    code = error.get("Code", "")
    return code if isinstance(code, str) else ""


def _http_status(exc: ClientError) -> int:
    metadata = exc.response.get("ResponseMetadata", {})
    status = metadata.get("HTTPStatusCode", 0)
    return status if isinstance(status, int) else 0


def _is_missing_object(exc: ClientError) -> bool:
    """A 404 whose code names the OBJECT, not the bucket/deployment."""
    if _http_status(exc) == 404 or _error_code(exc) in _MISSING_OBJECT_CODES:
        return _error_code(exc) != "NoSuchBucket"
    return False


def _translate(exc: Exception) -> Exception:
    """Map one botocore failure onto the integrations.errors taxonomy.

    Anything outside botocore's exception surface re-raises unchanged:
    an unexpected exception class is a programming error, not a
    provider outcome, and must not be laundered into a retry verdict.
    """
    if isinstance(exc, ClientError):
        code = _error_code(exc)
        if _http_status(exc) >= 500 or code in _TEMPORARY_ERROR_CODES:
            return TemporaryProviderError(
                f"S3 provider transient failure ({code or _http_status(exc)}): {exc}"
            )
        return PermanentProviderError(
            f"S3 provider rejected the request ({code or _http_status(exc)}): {exc}"
        )
    if isinstance(exc, ReadTimeoutError):
        # Request sent, answer never came: the side effect may have
        # happened — the taxonomy's unknown-outcome contract.
        return UnknownOutcomeError(f"S3 provider read timeout: {exc}")
    if isinstance(exc, (ConnectTimeoutError, EndpointConnectionError)):
        return TemporaryProviderError(f"S3 provider unreachable: {exc}")
    if isinstance(exc, BotoCoreError):
        # SDK-level rejection (credentials missing, parameter invalid):
        # retrying an identical request cannot succeed.
        return PermanentProviderError(f"S3 SDK rejected the request: {exc}")
    raise exc


class S3ObjectStorage:
    """``ObjectStorage`` over a boto3 synchronous client.

    One instance owns one client; composition roots construct it from
    ``Settings`` (the FastAPI provider in the submissions router and
    the worker's default storage factory). The client is thread-safe
    for serialized per-call use, which is all the port promises.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._bucket = settings.s3_bucket
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            # path-style addressing: the endpoint is a host:port (MinIO
            # or an S3-compatible gateway), never a wildcard-DNS
            # virtual-host scheme; s3v4 signs the presigned PUT's pinned
            # headers (Content-Type, Content-Length, If-None-Match),
            # which is how the port's write-once and size pins enforce.
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                # total_max_attempts (not max_attempts): the count
                # INCLUDES the initial request, so the worst-case budget
                # in Settings is the exact configured contract, not a
                # prose estimate (botocore's max_attempts counts only
                # retries after the first send).
                retries={
                    "total_max_attempts": settings.s3_delete_total_attempts,
                    "mode": "standard",
                },
                # Bounded provider calls: every API call — including
                # delete_object's HEAD-then-DELETE pair — is capped at
                # connect+read per attempt; see the constant block above
                # and Settings.s3_worst_case_delete_budget_seconds.
                connect_timeout=settings.s3_connect_timeout_seconds,
                read_timeout=settings.s3_read_timeout_seconds,
            ),
        )

    def create_upload_url(
        self,
        *,
        claim_id: UUID,
        content_type: str,
        expires_in: timedelta,
        content_length: int | None = None,
    ) -> UploadUrl:
        """Presigned WRITE-ONCE PUT on a server-generated key (port
        contract).

        ``IfNoneMatch="*"`` is always signed: exactly one successful PUT
        per key, and no PUT — this URL or any later one — can replace a
        stored object (412 while the provider enforces the condition,
        which on S3-compatible providers outlives the URL TTL). A
        ``content_length`` is signed as the Content-Length header: the
        client cannot frame its request at any other length without
        breaking the signature (403). The smoke test proves both
        behaviors against real MinIO over real HTTP.

        The signing contract rides on the return value (single source of
        truth, hardening P4c): ``client_headers`` carries exactly the
        headers a client must echo verbatim (If-None-Match, the pinned
        Content-Type — Content-Length is browser-forbidden and stays
        OUT of it), and ``pinned_content_length`` is the byte count the
        body must carry. The application passes both through to the
        client untouched; it never rebuilds them.
        """
        object_key = f"submissions/{claim_id}/{uuid4()}"
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": object_key,
            "ContentType": content_type,
            "IfNoneMatch": "*",
        }
        if content_length is not None:
            params["ContentLength"] = content_length
        try:
            url: str = self._client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=int(expires_in.total_seconds()),
            )
        except (ClientError, BotoCoreError) as exc:
            raise _translate(exc) from exc
        return UploadUrl(
            object_key=object_key,
            url=url,
            expires_at=self._clock.now() + expires_in,
            client_headers={
                "If-None-Match": "*",
                "Content-Type": content_type,
            },
            pinned_content_length=content_length,
        )

    def head_object(self, *, object_key: str) -> ObjectHead | None:
        """``None`` for a missing object; metadata for a present one."""
        try:
            head: Any = self._client.head_object(Bucket=self._bucket, Key=object_key)
        except (ClientError, BotoCoreError) as exc:
            if isinstance(exc, ClientError) and _is_missing_object(exc):
                return None
            raise _translate(exc) from exc
        return ObjectHead(
            object_key=object_key,
            size=int(head["ContentLength"]),
            content_type=str(head.get("ContentType", "application/octet-stream")),
        )

    def create_download_url(
        self, *, object_key: str, expires_in: timedelta
    ) -> DownloadUrl:
        """Presigned GET, signed blind — no existence check (port)."""
        try:
            url: str = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": object_key},
                ExpiresIn=int(expires_in.total_seconds()),
            )
        except (ClientError, BotoCoreError) as exc:
            raise _translate(exc) from exc
        return DownloadUrl(
            object_key=object_key,
            url=url,
            expires_at=self._clock.now() + expires_in,
        )

    def download_to_file(self, *, object_key: str, destination: Path) -> None:
        """Stream the object server-side into ``destination`` (worker reads)."""
        try:
            self._client.download_file(self._bucket, object_key, str(destination))
        except (ClientError, BotoCoreError) as exc:
            if isinstance(exc, ClientError) and _is_missing_object(exc):
                raise FileNotFoundError(f"no object under key {object_key!r}") from exc
            raise _translate(exc) from exc

    def delete_object(self, *, object_key: str) -> None:
        """Delete one object; absent raises (§27) — see module docstring."""
        # HEAD first: DeleteObject alone answers 204 for a missing key,
        # which would break the port's second-delete-raises contract.
        if self.head_object(object_key=object_key) is None:
            raise FileNotFoundError(f"no object under key {object_key!r}")
        try:
            self._client.delete_object(Bucket=self._bucket, Key=object_key)
        except (ClientError, BotoCoreError) as exc:
            raise _translate(exc) from exc
