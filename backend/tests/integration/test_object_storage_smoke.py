# backend/tests/integration/test_object_storage_smoke.py
"""Real S3/MinIO smoke for the ``S3ObjectStorage`` adapter (hardening
P0-1; G2/G18: an integration point needs one live proof).

The rest of the integration suite runs against PostgreSQL/Redis with
the object-storage port faked; this module exercises the REAL adapter
against a REAL S3-compatible provider (local MinIO at
``S3_ENDPOINT_URL``, CI's step container — the pinned
RELEASE.2025-09-07 image, so a pass here is a pass in CI) over the
port's full contract, every upload through a REAL presigned URL and a
REAL HTTP PUT (never ``client.put_object`` — that would bypass the
signature path the contract lives on):

- presigned upload pins (S3 hardening P0/P1): a client PUT with a
  different Content-Type is rejected (403); a PUT whose body length
  differs from the signed Content-Length is rejected (403); a PUT
  omitting the signed If-None-Match header is rejected;
- the signing contract returned by ``create_upload_url`` (hardening
  P4c) is proven, not just asserted on the dataclass: a PUT that echoes
  the adapter's ``client_headers`` verbatim and frames Content-Length
  to ``pinned_content_length`` succeeds against the real signature
  (a browser satisfies the same pin with a size-declared Blob — it
  cannot set the forbidden header itself);
- write-once: the FIRST PUT onto the fresh server-generated key
  succeeds, and any second PUT over the same URL/key — same
  Content-Type, same byte length, before or after finalize's HEAD —
  is rejected 412, so a stored object can never be replaced through a
  presigned URL and the finalize-time bytes are what downloads serve
  (submission immutability and authoritative ``submitted_at``);
- metadata check, byte-faithful server-side download, blind download
  signing, and the §27 delete semantics — including the adapter's
  HEAD-before-DELETE behavior a plain provider DeleteObject would
  silently break (204 for a missing key);
- the browser half of the upload contract (final pass B P2): the real
  CORS preflight the frontend's cross-origin PUT triggers — Origin +
  Access-Control-Request-Method PUT + the SIGNED headers
  (Content-Type, If-None-Match) as requested headers — is answered
  with an allow by the provider's server-wide origin list
  (``MINIO_API_CORS_ALLOW_ORIGIN`` in the compose env; the pinned
  MinIO release has no bucket-CORS API, so that env is the only gate).

Skipped unless ``CQ_S3_SMOKE=1``: the default local run must stay
green without MinIO. CI sets the flag (the MinIO service is up before
the pytest step), and the hardening gate runs it locally against
localhost:9000.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.config import get_settings
from app.integrations.object_storage import ObjectStorage, UploadUrl
from app.integrations.object_storage_s3 import S3ObjectStorage

pytestmark = pytest.mark.integration

if os.environ.get("CQ_S3_SMOKE") != "1":
    pytest.skip(
        "real-S3 smoke: set CQ_S3_SMOKE=1 (with S3_* settings pointing at "
        "MinIO) to run",
        allow_module_level=True,
    )

_TTL = timedelta(minutes=5)


@pytest.fixture
def storage() -> ObjectStorage:
    return S3ObjectStorage(get_settings())


def _put(upload: UploadUrl, body: bytes) -> int:
    """Real HTTP PUT through the presigned URL exactly as the returned
    contract describes (P4c): echo the adapter's ``client_headers``
    verbatim, and frame Content-Length to ``pinned_content_length``
    when the URL pins one — a Python client CAN set that header; a
    browser cannot (forbidden header) and satisfies the same pin with a
    Blob of that declared size. A URL issued without a length pin
    leaves the header to urllib's automatic framing. Proving this path
    proves the echoed contract satisfies the real signature."""
    headers = dict(upload.client_headers)
    if upload.pinned_content_length is not None:
        headers["Content-Length"] = str(upload.pinned_content_length)
    request = urllib.request.Request(
        upload.url, data=body, method="PUT", headers=headers
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status


def _rejected_put(
    url: str, body: bytes, *, content_type: str, if_none_match: bool = True
) -> int:
    """Issue a PUT that must NOT succeed; return the provider's status."""
    headers = {"Content-Type": content_type}
    if if_none_match:
        headers["If-None-Match"] = "*"
    request = urllib.request.Request(url, data=body, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.status, response.read()


def _preflight(url: str) -> tuple[int, Any]:
    """Issue the CORS preflight a browser sends before the signed PUT
    (final pass B P2) and return ``(status, headers)`` — the headers
    object is the case-insensitive ``http.client.HTTPMessage``."""
    request = urllib.request.Request(
        url,
        method="OPTIONS",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PUT",
            # Exactly the headers the adapter signs onto the PUT
            # (client_headers minus the browser-forbidden
            # Content-Length, which a browser never requests).
            "Access-Control-Request-Headers": "content-type, if-none-match",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers


def test_cors_preflight_admits_the_signed_browser_put(
    storage: ObjectStorage,
) -> None:
    """The upload contract's browser half, against the real provider:
    the frontend PUTs cross-origin to MinIO carrying the signed
    Content-Type and If-None-Match, so the OPTIONS preflight must be
    answered with an allow — the origin allowlist
    (MINIO_API_CORS_ALLOW_ORIGIN) admits the frontend dev origin, and
    both signed headers are echoed in Access-Control-Allow-Headers. A
    rejection here would break every browser upload even though the
    Python-client PUTs (which never preflight) all pass."""
    upload = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="text/csv",
        expires_in=_TTL,
        content_length=6,
    )
    status, headers = _preflight(upload.url)
    assert 200 <= status < 300, f"preflight must be allowed, got {status}"
    allow_origin = headers.get("Access-Control-Allow-Origin")
    assert allow_origin == "http://localhost:3000", (
        f"the frontend origin must be allowed, got {allow_origin!r}"
    )
    allow_headers = {
        header.strip().lower()
        for header in (headers.get("Access-Control-Allow-Headers") or "").split(",")
        if header.strip()
    }
    assert {"content-type", "if-none-match"} <= allow_headers, (
        f"both signed headers must be allowed, got {sorted(allow_headers)}"
    )
    allow_methods = (headers.get("Access-Control-Allow-Methods") or "").upper()
    assert "PUT" in allow_methods, (
        f"the cross-origin PUT method must be allowed, got {allow_methods!r}"
    )
    # Nothing was uploaded through this URL; no object to clean up.


def test_roundtrip_upload_head_download_delete(
    storage: ObjectStorage, tmp_path: Path
) -> None:
    claim_id = uuid.uuid4()
    body = b"claim_id,payload\n123,hello\n"
    upload = storage.create_upload_url(
        claim_id=claim_id,
        content_type="text/csv",
        expires_in=_TTL,
        content_length=len(body),
    )

    # Server-generated key: claim-scoped, uuid tail, nothing caller-chosen.
    prefix = f"submissions/{claim_id}/"
    assert upload.object_key.startswith(prefix)
    uuid.UUID(upload.object_key[len(prefix) :])  # parses as a uuid4
    now = datetime.now(UTC)
    assert now + _TTL - timedelta(seconds=30) <= upload.expires_at <= now + _TTL

    # The signing contract rides on the grant (P4c): the echo headers
    # are exactly what was signed, Content-Length is NOT among them
    # (browser-forbidden header), and the pin is the scalar byte count.
    assert upload.client_headers == {"If-None-Match": "*", "Content-Type": "text/csv"}
    assert "Content-Length" not in upload.client_headers
    assert upload.pinned_content_length == len(body)

    assert _put(upload, body) == 200

    head = storage.head_object(object_key=upload.object_key)
    assert head is not None
    assert head.object_key == upload.object_key
    assert head.size == len(body)
    assert head.content_type == "text/csv"

    destination = tmp_path / "roundtrip.csv"
    storage.download_to_file(object_key=upload.object_key, destination=destination)
    assert destination.read_bytes() == body

    # The presigned GET (browser path) serves the same bytes.
    download = storage.create_download_url(
        object_key=upload.object_key, expires_in=_TTL
    )
    status, served = _get(download.url)
    assert status == 200
    assert served == body

    storage.delete_object(object_key=upload.object_key)
    assert storage.head_object(object_key=upload.object_key) is None
    # §27: deleting an absent object again must raise (the retention
    # worker's idempotent-success vs reconcile signal).
    with pytest.raises(FileNotFoundError):
        storage.delete_object(object_key=upload.object_key)


def test_upload_url_pins_content_type(storage: ObjectStorage) -> None:
    upload = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="text/csv",
        expires_in=_TTL,
        content_length=6,
    )
    assert upload.client_headers["Content-Type"] == "text/csv"
    assert _rejected_put(upload.url, b"a,b\n1,", content_type="text/plain") == 403, (
        "a PUT with an unsigned Content-Type must be rejected"
    )


def test_write_once_url_first_put_wins_and_replay_is_rejected(
    storage: ObjectStorage, tmp_path: Path
) -> None:
    """The P0 write-once contract, proven over real HTTP: one URL, one
    successful PUT — replays with identical headers and byte length, a
    PUT after finalize's HEAD confirmation, and a PUT that omits the
    signed condition are all rejected, and the stored object keeps
    serving the FIRST PUT's bytes."""
    body = b"claim_id,value\n42,original\n"  # 25 bytes
    upload = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="text/csv",
        expires_in=_TTL,
        content_length=len(body),
    )
    assert upload.pinned_content_length == len(body)

    # First PUT onto the fresh key succeeds (echoing the returned
    # contract: client_headers + the framed pinned length).
    assert _put(upload, body) == 200

    # Same URL, same Content-Type, same byte length, different bytes:
    # rejected (412 PreconditionFailed on this provider).
    replay = b"claim_id,value\n42,tampered\n"[: len(body)]
    assert len(replay) == len(body)
    status = _rejected_put(upload.url, replay, content_type="text/csv")
    assert status in (412, 403), f"replay PUT must fail, got {status}"

    # finalize's verification step is head_object: the object is there
    # with the original metadata …
    head = storage.head_object(object_key=upload.object_key)
    assert head is not None
    assert head.size == len(body)
    assert head.content_type == "text/csv"

    # … and an overwrite attempt AFTER that confirmation still fails —
    # nothing can replace a stored object through a presigned URL,
    # inside or past the TTL (submission immutability).
    assert _rejected_put(upload.url, replay, content_type="text/csv") in (412, 403), (
        "post-finalize overwrite PUT must fail"
    )

    # The worker-side read serves exactly the finalize-time bytes.
    destination = tmp_path / "write-once.csv"
    storage.download_to_file(object_key=upload.object_key, destination=destination)
    assert destination.read_bytes() == body

    # A PUT that drops the signed If-None-Match header is rejected too:
    # the condition is part of the signature, so the client cannot
    # sidestep the write-once gate by omitting it.
    assert _rejected_put(
        upload.url, replay, content_type="text/csv", if_none_match=False
    ) in (400, 403), "a PUT omitting the signed If-None-Match header must be rejected"

    storage.delete_object(object_key=upload.object_key)


def test_upload_url_pins_content_length(storage: ObjectStorage) -> None:
    """The P1 size gate at the entry: the declared size is signed as the
    PUT's Content-Length, so an over- or under-length body breaks the
    signature and the provider rejects the PUT (403)."""
    # Exact declared length succeeds (the pin echoes on the grant).
    upload = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="text/csv",
        expires_in=_TTL,
        content_length=10,
    )
    assert upload.pinned_content_length == 10
    assert _put(upload, b"0123456789") == 200
    storage.delete_object(object_key=upload.object_key)

    # Eleven bytes through a URL signed for ten: rejected.
    oversized = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="text/csv",
        expires_in=_TTL,
        content_length=10,
    )
    assert _rejected_put(oversized.url, b"0123456789!", content_type="text/csv") == 403
    assert storage.head_object(object_key=oversized.object_key) is None, (
        "a rejected PUT must not leave an object behind"
    )

    # Nine bytes through a URL signed for ten: rejected as well.
    undersized = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="text/csv",
        expires_in=_TTL,
        content_length=10,
    )
    assert _rejected_put(undersized.url, b"012345678", content_type="text/csv") == 403
    assert storage.head_object(object_key=undersized.object_key) is None


def test_binary_boundary_payload_roundtrip(
    storage: ObjectStorage, tmp_path: Path
) -> None:
    upload = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="application/octet-stream",
        expires_in=_TTL,
    )
    # A URL issued without a length pin (legacy/test callers): the
    # scalar pin is None and the PUT frames Content-Length itself.
    assert upload.pinned_content_length is None
    # Deterministic non-UTF-8, non-ASCII-aligned bytes: every byte value
    # appears; byte fidelity must survive presigned PUT, provider
    # storage, and the streamed server-side download.
    body = bytes(range(256)) * 4096  # 1 MiB
    assert _put(upload, body) == 200

    head = storage.head_object(object_key=upload.object_key)
    assert head is not None
    assert head.size == len(body)

    destination = tmp_path / "boundary.bin"
    storage.download_to_file(object_key=upload.object_key, destination=destination)
    assert destination.read_bytes() == body
    storage.delete_object(object_key=upload.object_key)


def test_empty_payload_is_a_stored_object(storage: ObjectStorage) -> None:
    upload = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="application/octet-stream",
        expires_in=_TTL,
        content_length=0,
    )
    assert upload.pinned_content_length == 0
    assert _put(upload, b"") == 200
    head = storage.head_object(object_key=upload.object_key)
    assert head is not None
    assert head.size == 0
    storage.delete_object(object_key=upload.object_key)


def test_missing_object_semantics(storage: ObjectStorage, tmp_path: Path) -> None:
    upload = storage.create_upload_url(
        claim_id=uuid.uuid4(), content_type="text/csv", expires_in=_TTL
    )
    # The URL was issued but never used: the object does not exist.
    assert storage.head_object(object_key=upload.object_key) is None
    with pytest.raises(FileNotFoundError):
        storage.download_to_file(
            object_key=upload.object_key, destination=tmp_path / "missing.csv"
        )
    with pytest.raises(FileNotFoundError):
        storage.delete_object(object_key=upload.object_key)
    # Providers sign without checking existence, and so does the port:
    # the URL exists even though the object does not (fetching it fails
    # provider-side — 403/404 — never at signing time).
    download = storage.create_download_url(
        object_key=upload.object_key, expires_in=_TTL
    )
    assert download.url
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(download.url)
    assert caught.value.code in (403, 404)
