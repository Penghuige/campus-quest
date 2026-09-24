# backend/tests/fixtures/files/build_fixtures.py
"""Runtime generation of the pathological upload fixtures (plan 10
task 6, step 1; the e2e file-security module's whole fixture source).

Every case is BOUNDED BY CONSTRUCTION (the plan's Global Constraints:
security fixtures must be safe; no test executes untrusted uploaded
code): nothing here writes a multi-megabyte artifact, nothing is
committed to the repository, and the "zip bomb" is a SHAPE — a tiny
archive whose DECLARED compression ratio trips the validator's
preflight (>100:1) long before any inflation could run. The largest
in-memory body this module builds is ~140 KB (the long-cell CSV),
orders of magnitude below the 200 MB upload cap and the CI sandbox's
1 GB RLIMIT_AS.

Each builder returns a ``PathologicalFixture``: the bytes to upload,
the declared ``FileType``, the Task-schema overrides the seeded task
must carry, and the stable ``ValidationCode`` set the §12.4 report must
answer with — so the e2e module asserts typed outcomes, never crash
shapes.

Formula-injection note (task 6 step 3): the product has NO spreadsheet
export path — ``grep -rn "openpyxl" app/`` answers exactly the
read-only ``validators/xlsx_validator.py`` and the sandbox child entry
— so the display contract under test is the §12.4 report itself:
``= + - @``-prefixed values ride the preview/findings as literal text
(``formula_injection_csv``), never evaluated, never transformed.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from dataclasses import dataclass, field
from typing import Any

from app.modules.submissions.enums import FileType
from app.modules.submissions.validators.common import ValidationCode

__all__ = [
    "PathologicalFixture",
    "binary_renamed_csv",
    "fake_sqlite",
    "formula_injection_csv",
    "high_compression_xlsx",
    "oversized_row_count_csv",
    "sqlite_multiple_tables",
    "very_long_csv_cell",
    "xlsx_missing_sheet",
]


@dataclass(frozen=True, slots=True)
class PathologicalFixture:
    """One generated pathological upload and its expected verdict.

    ``schema_overrides`` merge into the Task's ``submission_schema``
    at seed time (the e2e module's local task seeder): row-count and
    selector cases need schema-level bounds/pointers the default
    factories schema does not carry.
    """

    name: str
    declared_type: FileType
    body: bytes
    #: Error codes the report MUST carry (typed failure, not a crash).
    expected_error_codes: frozenset[ValidationCode] = frozenset()
    #: Warning codes the report MUST carry (bounded-degradation notes).
    expected_warning_codes: frozenset[ValidationCode] = frozenset()
    schema_overrides: dict[str, Any] = field(default_factory=dict)


def binary_renamed_csv() -> PathologicalFixture:
    """A binary blob renamed/declared .csv (spec §38.4 假 .csv).

    PNG magic + NUL bytes + a non-UTF-8 tail: content detection answers
    "none of the three types", so the validation service fails it with
    the typed ``FILE_TYPE_NOT_ALLOWED`` — never a parser crash on
    binary input.
    """
    body = (
        b"\x89PNG\r\n\x1a\n"  # PNG magic: decisively not text
        + bytes(range(256)) * 4  # includes NUL and non-UTF-8 bytes
        + b"\x00\x00\x00IEND\xaeB`\x82"
    )
    return PathologicalFixture(
        name="binary-renamed-csv",
        declared_type=FileType.CSV,
        body=body,
        expected_error_codes=frozenset({ValidationCode.FILE_TYPE_NOT_ALLOWED}),
    )


def fake_sqlite() -> PathologicalFixture:
    """SQLite magic followed by garbage (spec §38.4 假 .sqlite).

    Content detection classifies it SQLITE (the 16-byte magic is all it
    trusts), the validator's own header gate passes, and the read-only
    open fails as the typed ``MALFORMED_SQLITE`` outcome.
    """
    body = b"SQLite format 3\x00" + b"\xde\xad\xbe\xef" * 64
    return PathologicalFixture(
        name="fake-sqlite",
        declared_type=FileType.SQLITE,
        body=body,
        expected_error_codes=frozenset({ValidationCode.MALFORMED_SQLITE}),
    )


def sqlite_multiple_tables() -> PathologicalFixture:
    """A real SQLite file with two user tables and no selector.

    Table selection without ``source_selector.table_name`` demands
    exactly one user table; two answer the typed ``AMBIGUOUS_TABLE``.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE notes_a (url TEXT, title TEXT)")
        connection.execute("INSERT INTO notes_a VALUES ('https://a', '甲')")
        connection.execute("CREATE TABLE notes_b (url TEXT, title TEXT)")
        connection.execute("INSERT INTO notes_b VALUES ('https://b', '乙')")
        connection.commit()
        body = connection.serialize()
    finally:
        connection.close()
    return PathologicalFixture(
        name="sqlite-multiple-tables",
        declared_type=FileType.SQLITE,
        body=bytes(body),
        expected_error_codes=frozenset({ValidationCode.AMBIGUOUS_TABLE}),
    )


def oversized_row_count_csv() -> PathologicalFixture:
    """The minimal ROW_COUNT_TRUNCATED trigger (行数字段边界).

    Four tiny data rows against a Task schema with ``max_rows = 2``:
    the scan budget is max_rows+1 = 3 rows (the proof row), so the
    FOURTH row stops the scan mid-file — ``row_count`` becomes a lower
    bound (the warning) while the count already past the bound stays
    conclusive (the error). The smallest input that exercises the
    row-count boundary without any giant fixture (the plan's
    "oversized logical row count with small bounded fixture").
    """
    body = (
        b"url,title\n"
        b"https://example.com/note/1,one\n"
        b"https://example.com/note/2,two\n"
        b"https://example.com/note/3,three\n"
        b"https://example.com/note/4,four\n"
    )
    return PathologicalFixture(
        name="oversized-row-count",
        declared_type=FileType.CSV,
        body=body,
        expected_error_codes=frozenset({ValidationCode.MAX_ROWS_EXCEEDED}),
        expected_warning_codes=frozenset({ValidationCode.ROW_COUNT_TRUNCATED}),
        schema_overrides={"max_rows": 2},
    )


def very_long_csv_cell() -> PathologicalFixture:
    """One cell past the 65_536-character cap (§14 resource bounds).

    A ~70k-character title cell: the row scan answers the per-cell
    ``CELL_TOO_LONG`` error and keeps scanning — bounded echo, no
    memory blow-up, no crash.
    """
    long_cell = "长" * 70_000
    body = (
        "url,title\n"
        f"https://example.com/note/long,{long_cell}\n"
        "https://example.com/note/ok,正常\n"
    ).encode()
    return PathologicalFixture(
        name="very-long-csv-cell",
        declared_type=FileType.CSV,
        body=body,
        expected_error_codes=frozenset({ValidationCode.CELL_TOO_LONG}),
    )


def xlsx_missing_sheet() -> PathologicalFixture:
    """A well-formed XLSX whose target sheet does not exist.

    The schema's ``source_selector.sheet_name`` names a sheet the
    workbook does not carry; sheet selection fails with the typed
    ``SHEET_NOT_FOUND`` listing the available names.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Data"
    sheet.append(["url", "title"])
    sheet.append(["https://example.com/note/1", "第一条"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return PathologicalFixture(
        name="xlsx-missing-sheet",
        declared_type=FileType.XLSX,
        body=buffer.getvalue(),
        expected_error_codes=frozenset({ValidationCode.SHEET_NOT_FOUND}),
        schema_overrides={"source_selector": {"sheet_name": "目标工作表"}},
    )


def high_compression_xlsx() -> PathologicalFixture:
    """The zip-bomb SHAPE, bounded far below CI resource limits.

    A real ZIP carrying the OOXML manifest (so detection classifies it
    XLSX) plus one worksheet part of highly repetitive XML: ~256 KB
    declared uncompressed against a ~1 KB stored size — a >100:1 ratio
    that trips the preflight ``SUSPICIOUS_COMPRESSION_RATIO`` BEFORE
    any entry is inflated. The artifact itself stays ~2 KB on disk and
    nothing ever decompresses the big part: the defense under test is
    the declared-size gate, not the sandbox's memory wall.
    """
    worksheet = b'<row r="1">' + b"<c></c>" * 32_768 + b"</row>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"></Types>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return PathologicalFixture(
        name="high-compression-xlsx",
        declared_type=FileType.XLSX,
        body=buffer.getvalue(),
        expected_error_codes=frozenset({ValidationCode.SUSPICIOUS_COMPRESSION_RATIO}),
        schema_overrides={"source_selector": {"sheet_name": "Data"}},
    )


def formula_injection_csv() -> PathologicalFixture:
    """``= + - @``-prefixed values through the real validation chain.

    Every injection-prefix family rides a legal string cell; the
    machine gate passes and the §12.4 report echoes the values as
    LITERAL TEXT in the safe preview (the e2e module asserts the
    preview carries them verbatim — the server has no formula engine,
    and ``grep -rn "openpyxl" app/`` shows no export path exists to
    escape, so preview/report rendering IS the display surface).
    """
    rows = [
        ("=SUM(A1:A9)", "=1+1"),
        ("+70.1", "+70.1"),
        ("-2+3", "-2+3"),
        ("@SUM(A1:A9)", "@SUM(A1:A9)"),
    ]
    buffer = io.StringIO()
    buffer.write("url,title\n")
    for url, title in rows:
        buffer.write(f"{url},{title}\n")
    body = buffer.getvalue().encode()
    return PathologicalFixture(
        name="formula-injection",
        declared_type=FileType.CSV,
        body=body,
        # No errors: string-typed cells pass; the assertion surface is
        # the preview text, carried by the e2e module.
        expected_error_codes=frozenset(),
    )
