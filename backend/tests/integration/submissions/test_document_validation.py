# backend/tests/integration/submissions/test_document_validation.py
"""Document-family validation over the real service (spec §10.1, PR #54).

The full DOCX/PDF pipeline through ``ValidationService.validate_submission``
against a live PostgreSQL and the real sandbox: a genuine document passes
with an integrity-only report (row_count=None), a mismatched file (a
DOCX declared as PDF) fails with exactly one FILE_CORRUPT finding, and
a structured submission against a document task is refused at the
allowed-set gate. These close the coverage gap the ratchet flagged
after PR #54 (submissions 93.1 -> 92.7: ``_assemble_document``'s
branches were green-path-only).

Sync test bodies with explicit ``asyncio.run`` steps — the validation
service runs its own event loop inside the sandbox boundary, so the
harness must not already be inside one (the validation-worker suite's
established shape).
"""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.modules.submissions.enums import ValidationStatus
from tests.integration.submissions.test_validation_worker import (
    _cleanup,
    _new_factory,
    _seed,
    _service,
    _store,
    _validate,
)

_NOW = datetime(2026, 10, 9, 8, 0, 0, tzinfo=UTC)
_GRACE = _NOW + timedelta(hours=2)


def _docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document><w:body/></w:document>")
    return buffer.getvalue()


def _pdf_bytes() -> bytes:
    return b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


@pytest.mark.integration
def test_docx_validates_with_integrity_only_report() -> None:
    """A genuine DOCX on a document task: VALIDATED, row_count=None,
    no findings — the machine check answered integrity and stopped."""
    factory = _new_factory()
    run = uuid.uuid4().hex[:8]
    task_ids: list[Any] = []
    user_ids: list[Any] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                declared_type="DOCX",
                content=_docx_bytes(),
                task_schema=None,
                allowed_types=["DOCX", "PDF"],
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _docx_bytes()))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)

        assert result.status is ValidationStatus.VALIDATED
        assert result.detected_type is not None
        assert result.detected_type.value == "DOCX"
        assert result.report.row_count is None
        assert result.report.errors == ()
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_pdf_validates_with_integrity_only_report() -> None:
    factory = _new_factory()
    run = uuid.uuid4().hex[:8]
    task_ids: list[Any] = []
    user_ids: list[Any] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                declared_type="PDF",
                content=_pdf_bytes(),
                task_schema=None,
                allowed_types=["DOCX", "PDF"],
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _pdf_bytes()))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)

        assert result.status is ValidationStatus.VALIDATED
        assert result.detected_type is not None
        assert result.detected_type.value == "PDF"
        assert result.report.row_count is None
        assert result.report.errors == ()
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_declared_pdf_with_docx_content_fails_file_corrupt() -> None:
    """The §10.1 mismatch branch: content says DOCX, declaration says
    PDF — one FILE_CORRUPT finding, VALIDATION_FAILED, row_count None."""
    factory = _new_factory()
    run = uuid.uuid4().hex[:8]
    task_ids: list[Any] = []
    user_ids: list[Any] = []
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                declared_type="PDF",
                content=_docx_bytes(),
                task_schema=None,
                allowed_types=["DOCX", "PDF"],
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, _docx_bytes()))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)

        assert result.status is ValidationStatus.VALIDATION_FAILED
        codes = [finding.code for finding in result.report.errors]
        assert codes == ["FILE_CORRUPT"]
        assert result.report.row_count is None
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))


@pytest.mark.integration
def test_document_task_refuses_structured_upload() -> None:
    """The allowed-set gate is family-agnostic: a CSV against a
    document-only task is refused before any parsing."""
    factory = _new_factory()
    run = uuid.uuid4().hex[:8]
    task_ids: list[Any] = []
    user_ids: list[Any] = []
    csv_bytes = b"url,title\nhttps://example.com/a,demo\n"
    try:
        seed = asyncio.run(
            _seed(
                factory,
                run,
                declared_type="CSV",
                content=csv_bytes,
                task_schema=None,
                allowed_types=["DOCX", "PDF"],
            )
        )
        task_ids.append(seed.task.id)
        user_ids.extend((seed.teacher.id, seed.student.id))
        storage = asyncio.run(_store(factory, seed, csv_bytes))
        service = _service(storage)
        result = _validate(service, factory, seed.submission.id)

        assert result.status is ValidationStatus.VALIDATION_FAILED
        messages = " ".join(finding.message for finding in result.report.errors)
        assert "不接受此文件类型" in messages
    finally:
        asyncio.run(_cleanup(factory, task_ids=task_ids, user_ids=user_ids))
