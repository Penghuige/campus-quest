# backend/tests/unit/submissions/test_declared_type_content_types.py
"""The MIME map covers the FULL upload universe (§10.1, fourth miss).

The frontend's document e2e died at a KeyError: the map had three
members while the enum had five. The pin test: every FileType member
has a MIME entry, the two document MIMEs are the registered IANA
values browsers actually send, and the CSV/XLSX/SQLITE values are
frozen (they ride signed presigned URLs — a change breaks live
sessions mid-flight). This is a contract freeze, not a copy of the
implementation: the _full coverage_ assertion is the invariant that
failed in production shape.
"""

from __future__ import annotations

from app.modules.submissions.enums import FileType
from app.modules.submissions.upload_service import DECLARED_TYPE_CONTENT_TYPES


def test_every_file_type_member_has_a_mime_entry() -> None:
    """The invariant whose absence was the fourth §10.1 miss: the map
    must cover the enum exactly — no member without a MIME, no MIME
    without a member."""
    assert set(DECLARED_TYPE_CONTENT_TYPES) == set(FileType)


def test_document_family_mimes_are_the_registered_values() -> None:
    assert (
        DECLARED_TYPE_CONTENT_TYPES[FileType.DOCX]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert DECLARED_TYPE_CONTENT_TYPES[FileType.PDF] == "application/pdf"


def test_structured_family_mimes_are_frozen() -> None:
    """The three original entries ride signed URLs in live sessions —
    any change here invalidates every in-flight presigned PUT. The
    freeze is deliberate and review-gated."""
    assert DECLARED_TYPE_CONTENT_TYPES[FileType.CSV] == "text/csv"
    assert DECLARED_TYPE_CONTENT_TYPES[FileType.XLSX] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert DECLARED_TYPE_CONTENT_TYPES[FileType.SQLITE] == "application/vnd.sqlite3"
