# backend/tests/unit/submissions/test_document_detect.py
"""Document-family detection and the integrity verdict (spec §10.1).

The two new members of the upload universe classify by content:
DOCX is a ZIP whose OOXML manifest sits beside ``word/`` members
(the XLSX/DOCX disambiguation rule), PDF is ``%PDF-`` with an
``%%EOF`` trailer inside the bounded tail read. The task-side family
exclusivity and the schema-empty rule are covered in the task
lifecycle suite; these tests pin the CONTENT side.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.modules.submissions.enums import FileType
from app.modules.submissions.validators.detect import detect_file_type


def _docx_bytes() -> bytes:
    """A minimal-but-real OOXML word-processing package: the manifest
    plus one ``word/`` member is exactly what separates DOCX from
    XLSX at the detection layer."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return buffer.getvalue()


def _write(tmp_path: Path, data: bytes) -> Path:
    target = tmp_path / "upload.bin"
    target.write_bytes(data)
    return target


def test_docx_detected_by_word_members(tmp_path: Path) -> None:
    assert detect_file_type(_write(tmp_path, _docx_bytes())) == FileType.DOCX


def test_xlsx_still_detected_without_word_members(tmp_path: Path) -> None:
    assert detect_file_type(_write(tmp_path, _xlsx_bytes())) == FileType.XLSX


def test_pdf_with_eof_trailer_detected(tmp_path: Path) -> None:
    pdf = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    assert detect_file_type(_write(tmp_path, pdf)) == FileType.PDF


def test_pdf_header_without_trailer_is_none(tmp_path: Path) -> None:
    """§10.1's head+tail rule: a ``%PDF-`` header with no ``%%EOF`` in
    the bounded tail is not a usable PDF — truncated or a fake."""
    assert detect_file_type(_write(tmp_path, b"%PDF-1.7\njunk")) is None


def test_large_pdf_trailer_beyond_sniff_detected(tmp_path: Path) -> None:
    """A real PDF larger than the sniff window carries its ``%%EOF`` at
    the file's end, not in the first 8 KiB (C-F1: the prefix-only
    trailer search misjudged every such document as corrupt). The
    header plus a page-body's worth of padding plus the trailer is the
    smallest shape that reproduces it."""
    pdf = (
        b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
        + b"1 0 obj\n<< /Length 9000 >>\nstream\n"
        + b"x" * 9000
        + b"\nendstream\nendobj\ntrailer\n<<>>\n%%EOF\n"
    )
    assert len(pdf) > 8192
    assert detect_file_type(_write(tmp_path, pdf)) == FileType.PDF


def test_pdf_early_fake_trailer_without_tail_is_none(tmp_path: Path) -> None:
    """The other direction of the same defect: garbage whose ``%%EOF``
    sits inside the sniffed prefix but whose tail carries none is NOT
    a PDF (the trailer must live within the bounded tail, ISO 32000
    §7.5.5) — the prefix-only search accepted exactly this shape."""
    garbage = b"%PDF-1.7\n%%EOF\n" + b"j" * 9000
    assert len(garbage) > 8192
    assert detect_file_type(_write(tmp_path, garbage)) is None


def test_zip_without_manifest_is_none(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    assert detect_file_type(_write(tmp_path, buffer.getvalue())) is None


def test_csv_still_routes_to_text(tmp_path: Path) -> None:
    assert detect_file_type(_write(tmp_path, b"url,title\na,b\n")) == FileType.CSV
