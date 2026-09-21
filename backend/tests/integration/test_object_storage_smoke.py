# backend/tests/integration/test_object_storage_smoke.py
"""Real S3/MinIO smoke for the ``S3ObjectStorage`` adapter (hardening
P0-1; G2/G18: an integration point needs one live proof).

The rest of the integration suite runs against PostgreSQL/Redis with
the object-storage port faked; this module exercises the REAL adapter
against a REAL S3-compatible provider (local MinIO at
``S3_ENDPOINT_URL``, CI's step container) over the port's full
contract: presigned upload (content-type pinned), metadata check,
byte-faithful server-side download, blind download signing, and the
§27 delete semantics — including the adapter's HEAD-before-DELETE
behavior a plain provider DeleteObject would silently break (204 for a
missing key).

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

import pytest

from app.core.config import get_settings
from app.integrations.object_storage import ObjectStorage
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


def _put(url: str, body: bytes, *, content_type: str) -> int:
    request = urllib.request.Request(
        url, data=body, method="PUT", headers={"Content-Type": content_type}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.status, response.read()


def test_roundtrip_upload_head_download_delete(
    storage: ObjectStorage, tmp_path: Path
) -> None:
    claim_id = uuid.uuid4()
    upload = storage.create_upload_url(
        claim_id=claim_id, content_type="text/csv", expires_in=_TTL
    )

    # Server-generated key: claim-scoped, uuid tail, nothing caller-chosen.
    prefix = f"submissions/{claim_id}/"
    assert upload.object_key.startswith(prefix)
    uuid.UUID(upload.object_key[len(prefix) :])  # parses as a uuid4
    now = datetime.now(UTC)
    assert now + _TTL - timedelta(seconds=30) <= upload.expires_at <= now + _TTL

    body = b"claim_id,payload\n123,hello\n"
    assert _put(upload.url, body, content_type="text/csv") == 200

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
        claim_id=uuid.uuid4(), content_type="text/csv", expires_in=_TTL
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        _put(upload.url, b"a,b\n", content_type="text/plain")
    assert caught.value.code in (403, 400)


def test_binary_boundary_payload_roundtrip(
    storage: ObjectStorage, tmp_path: Path
) -> None:
    upload = storage.create_upload_url(
        claim_id=uuid.uuid4(),
        content_type="application/octet-stream",
        expires_in=_TTL,
    )
    # Deterministic non-UTF-8, non-ASCII-aligned bytes: every byte value
    # appears; byte fidelity must survive presigned PUT, provider
    # storage, and the streamed server-side download.
    body = bytes(range(256)) * 4096  # 1 MiB
    assert _put(upload.url, body, content_type="application/octet-stream") == 200

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
    )
    assert _put(upload.url, b"", content_type="application/octet-stream") == 200
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
