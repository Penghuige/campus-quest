# backend/tests/unit/tasks/test_import_zip_safety.py
"""Assignment-import ZIP hardening (security round F3).

``parse_upload`` accepted any archive openpyxl could open: no
declared-size preflight (a lying directory or an honest bomb rode the
2 MiB byte cap straight into decompression) and no row cap during
enumeration (a million-row sheet materialized fully before the
5000-row limit ever ran). These tests pin the shared preflight
(``app.integrations.zip_safety``) as the import side's gate — the
same rules the submission XLSX validator enforces — plus the
in-iteration row cap.
"""

from __future__ import annotations

import io
import struct
import zipfile

from app.modules.tasks.import_parsing import parse_upload

_CAPS = {
    "max_file_bytes": 2 * 1024 * 1024,
    "max_rows": 5000,
    "keyword_max_length": 200,
}


def _zip_bytes(parts: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _xlsx_bytes(sheet_xml: str) -> bytes:
    """A minimal honest OOXML package around one worksheet (the rels
    chain openpyxl needs to resolve workbook -> sheet)."""
    return _zip_bytes(
        {
            "[Content_Types].xml": (
                '<Types xmlns="http://schemas.openxmlformats.org/'
                'package/2006/content-types"><Default Extension="rels" '
                'ContentType="application/vnd.openxmlformats-package.'
                'relationships+xml"/><Default Extension="xml" '
                'ContentType="application/xml"/><Override PartName='
                '"/xl/workbook.xml" ContentType="application/vnd.'
                "openxmlformats-officedocument.spreadsheetml.sheet."
                'main+xml"/><Override PartName="/xl/worksheets/sheet1.xml'
                '" ContentType="application/vnd.openxmlformats-'
                'officedocument.spreadsheetml.worksheet+xml"/></Types>'
            ),
            "_rels/.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships"><Relationship Id="r1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/'
                '2006/relationships/officeDocument" '
                'Target="xl/workbook.xml"/></Relationships>'
            ),
            "xl/workbook.xml": (
                '<workbook xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main" xmlns:r="http://schemas.'
                'openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="S" sheetId="1" r:id="r1"/>'
                "</sheets></workbook>"
            ),
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships"><Relationship Id="r1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/'
                '2006/relationships/worksheet" '
                'Target="worksheets/sheet1.xml"/></Relationships>'
            ),
            "xl/worksheets/sheet1.xml": sheet_xml,
        }
    )


def _sheet(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<row><c t="inlineStr"><is><t>{p}</t></is></c>'
        f'<c t="inlineStr"><is><t>{k}</t></is></c></row>'
        for p, k in rows
    )
    return (
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        f'<row><c t="inlineStr"><is><t>platform</t></is></c>'
        '<c t="inlineStr"><is><t>keyword</t></is></c></row>'
        f"{body}</sheetData></worksheet>"
    )


def lying_zip(
    name: str,
    *,
    declared_size: int,
    declared_compressed: int | None = None,
) -> bytes:
    """A zip whose central directory DECLARES chosen member sizes.

    Same craft as the submission validator's tests: the preflight
    reads declared sizes without decompressing, so the caps are
    exercised without ever materializing a bomb.
    """
    if declared_compressed is None:
        declared_compressed = declared_size
    data = b"payload"
    name_bytes = name.encode()
    # Local file header (STORED, crc never checked: nothing is read).
    local = (
        struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0,
            0,
            0,
            0,
            0,
            len(data),
            len(data),
            len(name_bytes),
            0,
        )
        + name_bytes
        + data
    )
    central = (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            20,
            20,
            0,
            0,
            0,
            0,
            0,
            declared_compressed,
            declared_size,
            len(name_bytes),
            0,
            0,
            0,
            0,
            0,
            0,
        )
        + name_bytes
    )
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(central), len(local), 0)
    return local + central + eocd


def test_honest_zip_bomb_rejected_before_decompression() -> None:
    # 64 MiB of zeros inside a 2 MiB-capped upload: compresses to well
    # under the byte cap, declared size sails past the per-part cap.
    bomb = _zip_bytes(
        {"[Content_Types].xml": b"x", "sheet.xml": b"\x00" * (64 * 1024 * 1024)}
    )
    parsed = parse_upload(bomb, **_CAPS)
    assert parsed.file_error is not None
    assert parsed.file_error.code.value == "MALFORMED_XLSX"
    assert "16" in parsed.file_error.message  # per-part cap named


def test_lying_directory_part_size_rejected() -> None:
    data = lying_zip(
        "xl/worksheets/sheet1.xml",
        declared_size=600 * 1024 * 1024,
        declared_compressed=6 * 1024 * 1024,
    )
    parsed = parse_upload(data, **_CAPS)
    assert parsed.file_error is not None
    assert parsed.file_error.code.value == "MALFORMED_XLSX"


def test_lying_directory_ratio_rejected() -> None:
    data = lying_zip(
        "xl/worksheets/sheet1.xml",
        declared_size=100 * 1024 * 1024,
        declared_compressed=100 * 1024,
    )
    parsed = parse_upload(data, **_CAPS)
    assert parsed.file_error is not None
    assert parsed.file_error.code.value == "MALFORMED_XLSX"


def test_suspicious_entry_name_rejected() -> None:
    data = _zip_bytes({"[Content_Types].xml": b"x", "../evil.xml": b"x"})
    parsed = parse_upload(data, **_CAPS)
    assert parsed.file_error is not None
    assert parsed.file_error.code.value == "MALFORMED_XLSX"


def test_entry_count_cap_rejected() -> None:
    data = _zip_bytes({f"p{index}.xml": b"x" for index in range(4097)})
    parsed = parse_upload(data, **_CAPS)
    assert parsed.file_error is not None
    assert parsed.file_error.code.value == "MALFORMED_XLSX"


def test_row_cap_enforced_during_enumeration() -> None:
    # 10k rows of tiny cells: an archive the byte cap happily admits.
    # The limit must fire while the sheet is being read, not after the
    # whole sheet has materialized (F3's second half).
    rows = [("xiaohongshu", f"关键词{index}") for index in range(10_000)]
    data = _xlsx_bytes(_sheet(rows))
    assert len(data) < _CAPS["max_file_bytes"]
    parsed = parse_upload(data, **_CAPS)
    assert parsed.file_error is not None
    assert parsed.file_error.code.value == "LIMIT_EXCEEDED"
    assert parsed.file_error.details["limit"] == _CAPS["max_rows"]


def test_honest_small_workbook_still_imports() -> None:
    rows = [("xiaohongshu", "考研"), ("douyin", "留学")]
    data = _xlsx_bytes(_sheet(rows))
    parsed = parse_upload(data, **_CAPS)
    assert parsed.file_error is None
    assert [(row.platform, row.keyword) for row in parsed.valid] == rows
