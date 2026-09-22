# backend/tests/unit/submissions/test_xlsx_validator.py
"""Unit tests for the XLSX submission validator (spec §12.2, §12.4;
backend-engineering §14: read-only streaming parse, zip preflight,
bounded rows/cells/columns/time, parser exceptions -> validation
outcomes).

Fixtures are built programmatically: plain workbooks via openpyxl
(write mode), everything openpyxl cannot express — cached formula
results, huge declared dimensions, a million empty rows, pathological
sharedStrings, external-link parts — as hand-crafted OOXML zips, and
the zip-bomb family as crafted archives whose central directory
declares attacker-chosen sizes (what the preflight reads).

Coverage matrix:

- the brief's six fixture cases: valid workbook; target sheet missing;
  multiple sheets with a selector; huge empty dimension; formula cell
  (cached value honored, never evaluated); suspicious compression
  ratio
- zip preflight: entry-count cap, total uncompressed cap,
  sharedStrings-specific cap, per-entry ratio cap via lying
  central-directory declarations, real deflated zeros ~1000:1 for the
  aggregate ratio, suspicious entry names (absolute path, .., drive
  letter), non-zip masquerading as xlsx, corrupt/truncated archive,
  zero-byte file — every one a stable code, never an exception
- sheet selection: named selector wins (case-sensitive, matching the
  schema.py convention); missing sheet -> SHEET_NOT_FOUND; no selector
  -> first VISIBLE sheet
- resource bounds: cooperative timeout via fake clock; row-cap abort
  with scan_incomplete semantics (ROW_COUNT_TRUNCATED + no
  min-rows/null-ratio conclusions from partial counts); header wider
  than max_columns aborts; empty-row streak cutoff terminates a
  million-empty-row sheet fast; read-probe proves no whole-file read
  (clamped requests on a multi-megabyte fixture)
- sharedStrings: 30k rows over a shared string table validate with
  bounded work; a sharedStrings part past its dedicated cap is
  rejected before openpyxl materializes it
- formulas: cached value -> validated as that value; no cached value
  -> warning + null classification (never evaluated, per spec §12.2)
- typed cells end-to-end: int/float/datetime/boolean cells and an
  Excel error cell (#DIV/0!) through the shared classification
  contract; cell-length cap; ragged rows (short row pads with nulls,
  long row -> ROW_SHAPE_MISMATCH)
- header semantics (schema.py convention carry): case-sensitive
  exact-match, edge-whitespace trim, duplicate headers, extra columns
- surface: path and stream inputs, caller stream never closed,
  parser_version / file_type / duration_ms fields
"""

from __future__ import annotations

import io
import struct
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import SubmissionSchema
from app.modules.submissions.validators.common import (
    ValidationCode,
    ValidationFinding,
    ValidationLimits,
    ValidationReport,
)
from app.modules.submissions.validators.xlsx_validator import (
    PARSER_VERSION,
    validate_xlsx,
)

HEADER = ("url", "title", "publish_time", "likes")
R1 = ("https://a.example/1", "First", datetime(2026, 1, 2, 3, 4, 5), 10)
R2 = ("https://a.example/2", "Second", "2026-01-03", None)
R3 = ("https://a.example/3", "Third", "2026-01-04", 0)
DATA_ROWS = (R1, R2, R3)


def make_schema(**overrides: Any) -> SubmissionSchema:
    """The spec §12 worked example as a parsed schema, with overrides."""
    raw: dict[str, Any] = {
        "required_columns": [
            {"name": "url", "type": "string", "unique": True},
            {"name": "title", "type": "string"},
            {"name": "publish_time", "type": "datetime"},
        ],
        "optional_columns": [
            {"name": "likes", "type": "integer", "nullable": True},
        ],
    }
    raw.update(overrides)
    return SubmissionSchema.parse(raw)


def build_workbook(
    rows: list[tuple[Any, ...]] | None = None,
    *,
    header: tuple[str, ...] | list[str] = HEADER,
    first_sheet_hidden: bool = False,
    other_sheets: tuple[tuple[str, list[str], list[tuple[Any, ...]]], ...] = (),
) -> bytes:
    """A real openpyxl-written workbook (inline strings, no formulas)."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(header))
    for row in rows if rows is not None else list(DATA_ROWS):
        sheet.append(list(row))
    for name, other_header, other_rows in other_sheets:
        extra = workbook.create_sheet(name)
        extra.append(list(other_header))
        for row in other_rows:
            extra.append(list(row))
    if first_sheet_hidden:
        sheet.sheet_state = "hidden"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --- hand-crafted OOXML parts (what openpyxl cannot write) ----------------

# OOXML namespace / content-type URIs, split only to satisfy the line
# length lint (implicit concatenation; the assembled strings are the
# canonical literals).
_NS_PACKAGE = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT_RELS = "application/vnd.openxmlformats-package.relationships+xml"
_CT_XML = "application/xml"
_CT_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_CT_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_CT_SHARED_STRINGS = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
)

_CT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_NS_PACKAGE}">
<Default Extension="rels" ContentType="{_CT_RELS}"/>
<Default Extension="xml" ContentType="{_CT_XML}"/>
<Override PartName="/xl/workbook.xml" ContentType="{_CT_MAIN}"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{_CT_SHEET}"/>
</Types>"""
_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_RELATIONSHIPS}">
<Relationship Id="rId1" Type="{_NS_OFFICE}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
_WORKBOOK = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_OFFICE}">
<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
_WORKBOOK_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_RELATIONSHIPS}">
<Relationship Id="rId1" Type="{_NS_OFFICE}/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def _sheet(sheet_data: str, dimension: str = "A1:D4") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/>'
        f"<sheetData>{sheet_data}</sheetData>"
        "</worksheet>"
    )


def _text_cell(ref: str, text: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


_HEADER_ROW = (
    '<row r="1">'
    + _text_cell("A1", "url")
    + _text_cell("B1", "title")
    + _text_cell("C1", "publish_time")
    + _text_cell("D1", "likes")
    + "</row>"
)
_VALID_DATA_ROW = (
    '<row r="2">'
    + _text_cell("A2", "https://a.example/1")
    + _text_cell("B2", "First")
    + _text_cell("C2", "2026-01-02")
    + '<c r="D2"><v>10</v></c>'
    + "</row>"
)
_VALID_SHEET = _sheet(_HEADER_ROW + _VALID_DATA_ROW)


def _zip_bytes(parts: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def craft_xlsx(
    sheet_xml: str = _VALID_SHEET,
    *,
    extra_parts: dict[str, str] | None = None,
    shared_strings: str | None = None,
) -> bytes:
    """A minimal valid OOXML package around the given sheet1.xml."""
    content_types = _CT
    if shared_strings is not None:
        content_types = content_types.replace(
            "</Types>",
            '<Override PartName="/xl/sharedStrings.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml'
            '.sharedStrings+xml"/>\n</Types>',
        )
    parts: dict[str, bytes | str] = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": _RELS,
        "xl/workbook.xml": _WORKBOOK,
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS,
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    if shared_strings is not None:
        parts["xl/sharedStrings.xml"] = shared_strings
    parts.update(extra_parts or {})
    return _zip_bytes(parts)


def lying_zip(
    name: str,
    *,
    declared_size: int,
    declared_compressed: int | None = None,
    actual: bytes = b"payload",
) -> bytes:
    """A zip whose central directory DECLARES chosen member sizes.

    The preflight reads declared sizes from the central directory
    without decompressing, so this exercises the caps without ever
    materializing a bomb: the single STORED entry's on-disk bytes are
    tiny while the directory claims `declared_size`.
    """
    if declared_compressed is None:
        declared_compressed = declared_size
    name_bytes = name.encode()
    data = actual
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


def run(
    data: bytes,
    schema: SubmissionSchema | None = None,
    limits: ValidationLimits | None = None,
    clock: Any = None,
) -> ValidationReport:
    stream = io.BytesIO(data)
    if clock is None:
        report = validate_xlsx(stream, schema or make_schema(), limits)
    else:
        report = validate_xlsx(stream, schema or make_schema(), limits, clock=clock)
    # The validator consumes the stream but never closes the caller's.
    assert not stream.closed
    return report


def error_codes(report: ValidationReport) -> set[ValidationCode]:
    return {finding.code for finding in report.errors}


def warning_codes(report: ValidationReport) -> set[ValidationCode]:
    return {finding.code for finding in report.warnings}


def first_finding(report: ValidationReport, code: ValidationCode) -> ValidationFinding:
    return next(f for f in report.errors + report.warnings if f.code == code)


class FakeClock:
    """Monotonic-callable stub: every call advances time by `step`."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 100.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class _ReadProbe(io.RawIOBase):
    """Raw byte stream recording the largest single read() served."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.max_request = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 10**9
        self.max_request = max(self.max_request, size)
        return self._buffer.read(size)

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._buffer.seek(offset, whence)

    def tell(self) -> int:
        return self._buffer.tell()


# --- the brief's fixture cases ----------------------------------------------


def test_valid_workbook_passes_with_full_report() -> None:
    report = run(build_workbook())

    assert report.passed is True
    assert report.errors == ()
    assert report.warnings == ()
    assert report.parser_version == PARSER_VERSION
    assert report.file_type is FileType.XLSX
    assert report.row_count == 3
    assert report.detected_columns == HEADER
    assert report.missing_required_columns == ()
    assert report.extra_columns == ()
    assert report.type_error_counts == {
        "url": 0,
        "title": 0,
        "publish_time": 0,
        "likes": 0,
    }
    assert report.null_ratios == {
        "url": 0.0,
        "title": 0.0,
        "publish_time": 0.0,
        "likes": round(1 / 3, 6),
    }
    assert report.duplicate_counts == {"url": 0}
    assert report.duration_ms >= 0.0


def test_target_sheet_missing_fails() -> None:
    data = build_workbook(other_sheets=(("Data", list(HEADER), list(DATA_ROWS)),))
    report = run(data, make_schema(source_selector={"sheet_name": "Nope"}))

    assert report.passed is False
    assert ValidationCode.SHEET_NOT_FOUND in error_codes(report)
    finding = first_finding(report, ValidationCode.SHEET_NOT_FOUND)
    assert "Nope" in finding.message
    assert report.row_count == 0


def test_selector_reads_named_sheet_case_sensitive() -> None:
    # The main sheet holds garbage columns; only the named sheet is valid.
    data = build_workbook(
        [("junk", "junk", "junk", "junk")],
        header=("a", "b", "c", "d"),
        other_sheets=(("Data", list(HEADER), list(DATA_ROWS)),),
    )
    report = run(data, make_schema(source_selector={"sheet_name": "Data"}))

    assert report.passed is True
    assert report.row_count == 3

    # Case-sensitive exact match (schema.py convention): 'data' != 'Data'.
    wrong_case = run(data, make_schema(source_selector={"sheet_name": "data"}))
    assert ValidationCode.SHEET_NOT_FOUND in error_codes(wrong_case)


def test_first_visible_sheet_used_without_selector() -> None:
    data = build_workbook(
        [("junk", "junk", "junk", "junk")],
        header=("a", "b", "c", "d"),
        first_sheet_hidden=True,
        other_sheets=(("Data", list(HEADER), list(DATA_ROWS)),),
    )
    report = run(data)

    assert report.passed is True
    assert report.row_count == 3
    assert report.detected_columns == HEADER


def test_huge_declared_dimension_is_bounded() -> None:
    # The dimension claims the full XFD x 1048576 grid; only 2 rows of
    # XML exist. Read-only iteration must follow the XML, not the
    # declared dimension, and row width must stay clamped.
    sheet = _sheet(_HEADER_ROW + _VALID_DATA_ROW, dimension="A1:XFD1048576")
    report = run(craft_xlsx(sheet))

    assert report.passed is True
    assert report.row_count == 1
    assert report.detected_columns == HEADER


def test_formula_with_cached_value_validated_as_value() -> None:
    # data_only: the CACHED result 10 is read; the formula is never
    # executed (openpyxl never evaluates anything).
    sheet = _sheet(
        _HEADER_ROW
        + '<row r="2">'
        + _text_cell("A2", "https://a.example/1")
        + _text_cell("B2", "First")
        + _text_cell("C2", "2026-01-02")
        + '<c r="D2"><f>SUM(1,2,3,4)</f><v>10</v></c>'
        + "</row>"
    )
    report = run(craft_xlsx(sheet))

    assert report.passed is True
    assert report.type_error_counts["likes"] == 0


def test_suspicious_compression_ratio_rejected() -> None:
    # ~10 MB of zeros deflates to ~10 KB: a ~1000:1 ratio a legitimate
    # spreadsheet XML never approaches (measured: 3.7-7:1).
    data = _zip_bytes({"xl/worksheets/sheet1.xml": b"\x00" * 10_000_000})
    report = run(data)

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.SUSPICIOUS_COMPRESSION_RATIO}
    assert report.row_count == 0


# --- zip preflight -----------------------------------------------------------


def test_entry_count_cap() -> None:
    data = _zip_bytes({f"part{index}.xml": b"x" for index in range(4097)})
    report = run(data)

    assert error_codes(report) == {ValidationCode.ARCHIVE_TOO_LARGE}
    assert "4097" in first_finding(report, ValidationCode.ARCHIVE_TOO_LARGE).message


def test_total_uncompressed_cap_via_lying_directory() -> None:
    # Declared 600 MB uncompressed from a 6 MB compressed claim: under
    # the ratio cap but over the total-uncompressed cap.
    data = lying_zip(
        "xl/worksheets/sheet1.xml",
        declared_size=600 * 1024 * 1024,
        declared_compressed=6 * 1024 * 1024,
    )
    report = run(data)

    assert error_codes(report) == {ValidationCode.ARCHIVE_TOO_LARGE}
    assert report.row_count == 0


def test_per_entry_ratio_cap_via_lying_directory() -> None:
    # 100 MB declared from 100 KB compressed: under the total cap but a
    # 1000:1 per-entry ratio.
    data = lying_zip(
        "xl/worksheets/sheet1.xml",
        declared_size=100 * 1024 * 1024,
        declared_compressed=100 * 1024,
    )
    report = run(data)

    assert error_codes(report) == {ValidationCode.SUSPICIOUS_COMPRESSION_RATIO}


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "../escape.xml", "xl/../../evil.xml", "C:/evil.xml"],
)
def test_suspicious_entry_names_rejected(name: str) -> None:
    data = _zip_bytes({"[Content_Types].xml": b"x", name: b"x"})
    report = run(data)

    assert ValidationCode.SUSPICIOUS_ARCHIVE_ENTRY in error_codes(report)


def test_non_zip_masquerading_as_xlsx_rejected() -> None:
    report = run(b"\x89PNG\r\n\x1a\nIHDR\x12\x34\x56\x78\x9a")

    assert error_codes(report) == {ValidationCode.MALFORMED_XLSX}
    assert report.row_count == 0


def test_truncated_archive_rejected() -> None:
    report = run(build_workbook()[:60])

    assert error_codes(report) == {ValidationCode.MALFORMED_XLSX}


def test_empty_file_rejected() -> None:
    report = run(b"")

    assert error_codes(report) == {ValidationCode.EMPTY_FILE}
    assert report.row_count == 0


def test_empty_worksheet_rejected() -> None:
    report = run(craft_xlsx(_sheet("")))

    assert error_codes(report) == {ValidationCode.EMPTY_FILE}
    assert "工作表" in first_finding(report, ValidationCode.EMPTY_FILE).message


def test_all_empty_cells_sheet_rejected() -> None:
    # sub-F1's shape probed on this side of the format fence (the CSV
    # twin of this input was the bypass): rows of cells that exist but
    # read as empty values are blank rows, so a sheet of only such
    # rows never yields a header and must fail EMPTY_FILE — never a
    # silent pass.
    row = (
        '<row r="1">'
        + "".join(
            f'<c r="{column}1" t="inlineStr"><is><t></t></is></c>' for column in "ABC"
        )
        + "</row>"
    )
    report = run(craft_xlsx(_sheet(row, dimension="A1:C1")))

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.EMPTY_FILE}


def test_external_links_warn_but_never_block() -> None:
    data = craft_xlsx(
        extra_parts={
            "xl/externalLinks/externalLink1.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                "<externalLink xmlns="
                '"http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
            )
        }
    )
    report = run(data)

    assert ValidationCode.EXTERNAL_LINK in warning_codes(report)
    assert report.passed is True  # a warning never fails the file
    assert report.row_count == 1


def test_whole_read_part_capped_reviewer_probe() -> None:
    # Fix-round-1 F1 regression: the reviewer's probe — a
    # legitimate-looking 100 MB [Content_Types].xml (4 MB compressed,
    # 25:1 ratio, 101 MB total, every earlier cap satisfied) used to
    # reach openpyxl, which reads the manifest whole (measured 12x
    # amplification: 1.23 GB peak, 35.8 s, passed=True). The per-part
    # cap rejects it in the preflight before a single part is opened.
    import tracemalloc

    data = lying_zip(
        "[Content_Types].xml",
        declared_size=100 * 1024 * 1024,
        declared_compressed=4 * 1024 * 1024,
    )
    tracemalloc.start()
    started = time.monotonic()
    report = run(data)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.monotonic() - started

    assert error_codes(report) == {ValidationCode.PART_TOO_LARGE}
    assert report.row_count == 0
    assert peak < 32 * 1024 * 1024  # nothing was ever materialized
    assert elapsed < 2.0


@pytest.mark.parametrize(
    "name",
    [
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/theme/theme1.xml",
        "docProps/core.xml",
        "docProps/custom.xml",
        "xl/pivotCache/pivotCacheDefinition1.xml",
        "xl/worksheets/_rels/sheet1.xml.rels",
    ],
)
def test_whole_read_part_family_capped(name: str) -> None:
    # Every part openpyxl reads whole and parses into objects, one
    # past the 16 MB per-part cap (compressed claim kept under the
    # ratio cap so the part cap is what fires).
    data = lying_zip(
        name,
        declared_size=17 * 1024 * 1024,
        declared_compressed=1 * 1024 * 1024,
    )
    report = run(data)

    assert error_codes(report) == {ValidationCode.PART_TOO_LARGE}


@pytest.mark.parametrize(
    "name",
    [
        "xl/worksheets/sheet1.xml",  # streamed row-by-row, never whole
        "xl/sharedStrings.xml",  # own dedicated 128 MB cap
        "xl/media/image1.png",  # binary, never parsed in this path
        "xl/drawings/drawing1.xml",  # not parsed by the read-only data path
    ],
)
def test_streamed_or_untouched_families_not_part_capped(name: str) -> None:
    # 100 MB declared from 4 MB: every cap satisfied for these
    # families — they must NOT hit the whole-read part cap (no false
    # positives on big streamed sheets or embedded media); the lying
    # archive then simply fails to parse as a workbook.
    data = lying_zip(
        name,
        declared_size=100 * 1024 * 1024,
        declared_compressed=4 * 1024 * 1024,
    )
    report = run(data)

    assert ValidationCode.PART_TOO_LARGE not in error_codes(report)
    assert error_codes(report) == {ValidationCode.MALFORMED_XLSX}


# --- rename bypass (fix round 2): parts are capped by DEFAULT, and the
# two content-type-resolved roles carry their caps under any name ----


def craft_renamed_workbook(
    part_name: str, workbook_padding: int = 17 * 1024 * 1024
) -> bytes:
    """A REAL workbook whose main part lives at `part_name`.

    openpyxl locates the workbook part by its manifest content type
    (an Override), not by name: with the Override pointed at
    `part_name`, an oversized main part parses exactly as it would
    under xl/workbook.xml. The padding is incompressible random ASCII
    inside an XML comment — real, valid bytes the parser tolerates,
    sized past the 16 MB per-part cap while under the ratio cap.
    """
    import random

    random.seed(23)
    padding = "".join(
        random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
        for _ in range(workbook_padding)
    )
    ct = _CT.replace(
        f'<Override PartName="/xl/workbook.xml" ContentType="{_CT_MAIN}"/>',
        f'<Override PartName="/{part_name}" ContentType="{_CT_MAIN}"/>',
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<!--{padding}-->"
        f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_OFFICE}">'
        '<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    # openpyxl derives the workbook's rels from the part's own path.
    directory, _, base = part_name.rpartition("/")
    workbook_rels_name = f"{directory}/_rels/{base}.rels"
    return _zip_bytes(
        {
            "[Content_Types].xml": ct,
            "_rels/.rels": _RELS,
            part_name: workbook,
            workbook_rels_name: _WORKBOOK_RELS,
            "xl/worksheets/sheet1.xml": _VALID_SHEET,
        }
    )


def test_renamed_oversized_workbook_part_capped() -> None:
    # Fix-round-2 regression, the re-reviewer's probe: an 18 MB main
    # workbook part renamed to xl/big.xml (Override fixed accordingly)
    # sailed through the by-name caps — byte-identical to the pre-fix
    # baseline (150 MB peak, passed=True). Default-deny caps it.
    import tracemalloc

    data = craft_renamed_workbook("xl/big.xml")
    tracemalloc.start()
    started = time.monotonic()
    report = run(data)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.monotonic() - started

    assert error_codes(report) == {ValidationCode.PART_TOO_LARGE}
    assert report.row_count == 0
    assert peak < 64 * 1024 * 1024  # the padded part is never read
    assert elapsed < 5.0


@pytest.mark.parametrize("hiding_place", ["xl/media/", "xl/drawings/"])
def test_renamed_workbook_hidden_in_streaming_family_capped(
    hiding_place: str,
) -> None:
    # The same rename taken one step further: the oversized main part
    # parked INSIDE a streaming-exempt family name. The manifest scan
    # maps Override content types back to roles, so the cap follows
    # the part under any name.
    data = lying_manifest_zip(
        f"{hiding_place}big.xml",
        declared_size=18 * 1024 * 1024,
        declared_compressed=1 * 1024 * 1024,
        content_type=_CT_MAIN,
    )
    report = run(data)

    assert error_codes(report) == {ValidationCode.PART_TOO_LARGE}


def test_renamed_string_table_capped() -> None:
    # A giant shared-string table under a non-canonical name: the
    # canonical 128 MB streaming cap applies to the canonical name
    # only; anything else falls to the 16 MB default (documented
    # decision — renamed tables get the stricter bound).
    data = lying_manifest_zip(
        "xl/strings.xml",
        declared_size=20 * 1024 * 1024,
        declared_compressed=1 * 1024 * 1024,
        content_type=_CT_SHARED_STRINGS,
    )
    report = run(data)

    assert error_codes(report) == {ValidationCode.PART_TOO_LARGE}


def test_renamed_string_table_hidden_in_media_family_capped() -> None:
    data = lying_manifest_zip(
        "xl/media/strings.xml",
        declared_size=20 * 1024 * 1024,
        declared_compressed=1 * 1024 * 1024,
        content_type=_CT_SHARED_STRINGS,
    )
    report = run(data)

    assert error_codes(report) == {ValidationCode.PART_TOO_LARGE}


def test_canonical_shared_strings_keep_their_streaming_cap() -> None:
    # 20 MB under the CANONICAL name and content type: below the
    # dedicated 128 MB cap, above the 16 MB default — the streaming
    # exemption must keep it admissible (it reaches openpyxl, which
    # then rejects the lying archive as unparseable, NOT as too large).
    data = lying_manifest_zip(
        "xl/sharedStrings.xml",
        declared_size=20 * 1024 * 1024,
        declared_compressed=1 * 1024 * 1024,
        content_type=_CT_SHARED_STRINGS,
    )
    report = run(data)

    assert ValidationCode.PART_TOO_LARGE not in error_codes(report)
    assert error_codes(report) == {ValidationCode.MALFORMED_XLSX}


def test_small_non_canonical_part_still_passes() -> None:
    # Default-deny must not reject ordinary small oddities: a 1 KB
    # part under an unrecognized name validates fine alongside real
    # data.
    data = craft_xlsx(extra_parts={"xl/customStuff/notes.xml": "<notes/>"})
    report = run(data)

    assert report.passed is True
    assert report.row_count == 1


def lying_manifest_zip(
    name: str,
    *,
    declared_size: int,
    declared_compressed: int,
    content_type: str,
) -> bytes:
    """A lying single-part zip plus a REAL [Content_Types].xml mapping
    `name` to `content_type` — the manifest resolution openpyxl uses.

    The lying entry's sizes are attacker-declared, so it cannot be
    written through zipfile; the two archives are spliced at the byte
    level with a corrected EOCD.
    """
    manifest = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_NS_PACKAGE}">'
        f'<Override PartName="/{name}" ContentType="{content_type}"/>'
        "</Types>"
    )
    real = _zip_bytes({"[Content_Types].xml": manifest})
    lying = lying_zip(
        name, declared_size=declared_size, declared_compressed=declared_compressed
    )
    real_central_at = real.rfind(b"PK\x01\x02")
    real_local = real[:real_central_at]
    real_central = real[real_central_at : real.rfind(b"PK\x05\x06")]
    lying_central_at = lying.rfind(b"PK\x01\x02")
    lying_local = lying[:lying_central_at]
    lying_central = bytearray(lying[lying_central_at : lying.rfind(b"PK\x05\x06")])
    # The lying central directory was built for a standalone zip: repoint
    # its local-header offset (last field of the fixed record) to where
    # the local header lands inside the splice.
    struct.pack_into("<I", lying_central, 42, len(real_local))
    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        2,
        2,
        len(real_central) + len(lying_central),
        len(real_local) + len(lying_local),
        0,
    )
    return real_local + lying_local + real_central + bytes(lying_central) + eocd


# --- resource bounds ---------------------------------------------------------


def test_timeout_trips_with_fake_clock() -> None:
    rows = [(f"https://a.example/{i}", "T", "2026-01-02", None) for i in range(3000)]
    limits = ValidationLimits(timeout_seconds=1.0)
    report = run(build_workbook(rows), limits=limits, clock=FakeClock(step=10.0))

    assert ValidationCode.TIMEOUT in error_codes(report)
    assert report.passed is False
    # The cooperative check runs every 1024 data rows.
    assert report.row_count == 1024
    assert ValidationCode.ROW_COUNT_TRUNCATED in warning_codes(report)
    assert report.duration_ms == 20000.0


def test_row_cap_aborts_with_scan_incomplete_semantics() -> None:
    rows = [(f"https://a.example/{i}", "T", "2026-01-02", None) for i in range(10)]
    schema = make_schema(
        min_rows=10,
        optional_columns=[
            {
                "name": "likes",
                "type": "integer",
                "nullable": True,
                "max_null_ratio": 0.05,
            },
        ],
    )
    report = run(build_workbook(rows), schema, ValidationLimits(row_cap=5))

    assert ValidationCode.ROW_LIMIT_EXCEEDED in error_codes(report)
    assert report.row_count == 6  # counted one past the cap, then stopped
    assert ValidationCode.ROW_COUNT_TRUNCATED in warning_codes(report)
    # No conclusion from truncated counts: neither min_rows...
    assert ValidationCode.MIN_ROWS_NOT_MET not in error_codes(report)
    # ...nor the null-ratio verdict the T4 carry gates.
    assert ValidationCode.NULL_RATIO_EXCEEDED not in error_codes(report)


def test_too_many_columns_aborts() -> None:
    header = tuple(f"c{index}" for index in range(6))
    data = build_workbook([("x",) * 6], header=header)
    report = run(data, limits=ValidationLimits(max_columns=5))

    assert error_codes(report) == {ValidationCode.TOO_MANY_COLUMNS}
    assert report.row_count == 0


def test_million_empty_rows_terminate_fast() -> None:
    empty_rows = b"".join(b'<row r="%d"/>' % index for index in range(3, 1_000_003))
    sheet = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<dimension ref="A1:D1000003"/><sheetData>'
        + _HEADER_ROW.encode()
        + _VALID_DATA_ROW.encode()
        + empty_rows
        + b"</sheetData></worksheet>"
    )
    data = _zip_bytes(
        {
            "[Content_Types].xml": _CT,
            "_rels/.rels": _RELS,
            "xl/workbook.xml": _WORKBOOK,
            "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS,
            "xl/worksheets/sheet1.xml": sheet,
        }
    )
    started = time.monotonic()
    report = run(data)
    elapsed = time.monotonic() - started

    # The empty-row streak cutoff ends the scan right after the data;
    # unbounded iteration of the same sheet takes > 2 s.
    assert report.row_count == 1
    assert report.passed is True
    assert ValidationCode.ROW_COUNT_TRUNCATED not in warning_codes(report)
    assert elapsed < 5.0


def test_reads_stay_bounded_on_multimegabyte_file() -> None:
    import random

    from app.modules.submissions.validators import xlsx_validator

    # Incompressible titles keep the ZIP itself multi-megabyte (the
    # clamp must bound reads on the compressed archive we hold).
    random.seed(11)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    rows = [
        (
            f"https://a.example/{i}",
            "".join(random.choice(alphabet) for _ in range(170)),
            "2026-01-02",
            i,
        )
        for i in range(15000)
    ]
    payload = build_workbook(rows)
    assert len(payload) > 2_000_000  # the fixture really is multi-megabyte
    probe = _ReadProbe(payload)
    report = validate_xlsx(probe, make_schema())

    assert report.passed is True
    assert report.row_count == 15000
    # No whole-file materialization: every served read stayed at the
    # validator's chunk clamp, far below the payload size.
    assert probe.max_request <= xlsx_validator._READ_CHUNK
    assert probe.max_request < len(payload)


# --- sharedStrings ------------------------------------------------------------


def test_many_shared_string_rows_validate() -> None:
    strings = "".join(
        f"<si><t>shared-value-{index:03d}</t></si>" for index in range(100)
    )
    sst = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'count="30000" uniqueCount="100">' + strings + "</sst>"
    )
    header = (
        '<row r="1">' + _text_cell("A1", "url") + _text_cell("B1", "title") + "</row>"
    )
    cells = b"".join(
        b'<row r="%d"><c r="A%d" t="s"><v>%d</v></c>'
        b'<c r="B%d" t="s"><v>%d</v></c></row>'
        % (index, index, index % 100, index, (index + 1) % 100)
        for index in range(2, 30002)
    )
    sheet = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<dimension ref="A1:B30002"/><sheetData>'
        + header.encode()
        + cells
        + b"</sheetData></worksheet>"
    ).decode()
    schema = make_schema(
        required_columns=[
            {"name": "url", "type": "string"},
            {"name": "title", "type": "string"},
        ]
    )
    report = run(craft_xlsx(sheet, shared_strings=sst), schema)

    assert report.passed is True
    assert report.row_count == 30000
    assert report.detected_columns == ("url", "title")


def test_pathological_sharedstrings_rejected_before_parsing() -> None:
    # A 200 MB sharedStrings claim (compressed claim kept under the
    # ratio cap so the dedicated size cap is what fires): openpyxl
    # materializes that table, so it is rejected up front.
    data = lying_zip(
        "xl/sharedStrings.xml",
        declared_size=200 * 1024 * 1024,
        declared_compressed=2 * 1024 * 1024,
    )
    report = run(data)

    assert error_codes(report) == {ValidationCode.ARCHIVE_TOO_LARGE}
    finding = first_finding(report, ValidationCode.ARCHIVE_TOO_LARGE)
    assert "sharedStrings" in finding.message


# --- formulas ------------------------------------------------------------------


def test_formula_without_cached_value_is_null_plus_warning() -> None:
    # openpyxl writes formulas with no cached result; data_only reads
    # None. Policy (documented in the validator): such cells classify
    # as null with a per-column warning — never evaluated.
    sheet = _sheet(
        _HEADER_ROW
        + '<row r="2">'
        + _text_cell("A2", "https://a.example/1")
        + '<c r="B2"><f>CONCATENATE("Fi","rst")</f></c>'
        + _text_cell("C2", "2026-01-02")
        + '<c r="D2"><f>1+1</f></c>'
        + "</row>"
    )
    report = run(craft_xlsx(sheet))

    # title is required non-nullable -> the unreadable cell is a null
    # violation; likes is nullable -> only counted.
    assert ValidationCode.NULL_VIOLATION in error_codes(report)
    finding = first_finding(report, ValidationCode.NULL_VIOLATION)
    assert finding.column == "title"
    warning = first_finding(report, ValidationCode.FORMULA_WITHOUT_CACHED_VALUE)
    assert warning.column == "title"
    # F5: the wording names BOTH causes precisely — a formula without
    # a cached result or a styled-but-valueless cell (both present as
    # a valueless cell in the XML); neither is claimed with certainty.
    assert "无缓存结果的公式" in warning.message
    assert "仅设置了格式" in warning.message
    assert "不执行公式" in warning.message
    assert report.null_ratios == {
        "url": 0.0,
        "title": 1.0,
        "publish_time": 0.0,
        "likes": 1.0,
    }


def test_openpyxl_written_formula_reads_as_unreadable() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(HEADER))
    sheet.append(["https://a.example/1", '=A2&"-title"', "2026-01-02", 10])
    buffer = io.BytesIO()
    workbook.save(buffer)

    report = run(buffer.getvalue())

    assert ValidationCode.FORMULA_WITHOUT_CACHED_VALUE in warning_codes(report)
    assert ValidationCode.NULL_VIOLATION in error_codes(report)


# --- typed cells and per-row semantics -------------------------------------------


def test_typed_cells_end_to_end() -> None:
    schema = SubmissionSchema.parse(
        {
            "required_columns": [
                {"name": "score", "type": "number"},
                {"name": "when", "type": "datetime"},
                {"name": "verified", "type": "boolean"},
            ],
        }
    )
    data = build_workbook(
        [
            (1.5, datetime(2026, 1, 2, 3, 4, 5), True),
            ("abc", "not-a-date", 2),
        ],
        header=("score", "when", "verified"),
    )
    report = run(data, schema)

    assert report.type_error_counts == {"score": 1, "when": 1, "verified": 1}
    assert report.row_count == 2


def test_excel_error_cell_is_type_error() -> None:
    sheet = _sheet(
        '<row r="1">' + _text_cell("A1", "score") + "</row>"
        '<row r="2"><c r="A2" t="e"><v>#DIV/0!</v></c></row>',
        dimension="A1:A2",
    )
    schema = SubmissionSchema.parse(
        {"required_columns": [{"name": "score", "type": "number"}]}
    )
    report = run(craft_xlsx(sheet), schema)

    assert ValidationCode.TYPE_ERROR in error_codes(report)
    assert report.type_error_counts == {"score": 1}


def test_extremely_long_cell_is_row_level_error() -> None:
    # openpyxl truncates written strings at Excel's own 32,767-char
    # cell limit, so the oversized cell is crafted directly — from
    # pseudo-random text, because a long run of one character would
    # compress past the preflight's ratio cap (as it should).
    import random

    random.seed(7)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    long_cell = "".join(random.choice(alphabet) for _ in range(65_537))
    sheet = _sheet(
        _HEADER_ROW
        + '<row r="2">'
        + _text_cell("A2", "https://a.example/1")
        + _text_cell("B2", long_cell)  # one past max_cell_length
        + _text_cell("C2", "2026-01-02")
        + '<c r="D2"><v>10</v></c>'
        + "</row>"
    )
    report = run(craft_xlsx(sheet))

    assert ValidationCode.CELL_TOO_LONG in error_codes(report)
    finding = first_finding(report, ValidationCode.CELL_TOO_LONG)
    assert finding.row == 1
    assert finding.column == "title"
    assert finding.value is None  # the giant cell is never echoed
    assert report.row_count == 1


def test_short_row_pads_with_nulls_long_row_mismatches() -> None:
    rows = [
        ("https://a.example/1",),  # short: missing cells are nulls
        ("https://a.example/2", "Second", "2026-01-03", 5, "extra"),  # long
    ]
    report = run(build_workbook(rows))

    # Short row: no shape error for it — missing trailing cells are
    # nulls (Excel semantics, not CSV raggedness).
    mismatches = [
        f for f in report.errors if f.code == ValidationCode.ROW_SHAPE_MISMATCH
    ]
    assert len(mismatches) == 1
    assert mismatches[0].row == 2
    # ...but the nulls still violate the required title column.
    assert ValidationCode.NULL_VIOLATION in error_codes(report)
    assert report.null_ratios["title"] == 0.5


def test_header_only_rejected_no_data_rows() -> None:
    report = run(build_workbook(rows=[]))

    assert error_codes(report) == {ValidationCode.NO_DATA_ROWS}
    assert report.row_count == 0
    assert report.detected_columns == HEADER


def test_duplicate_value_in_unique_column() -> None:
    rows = [
        ("https://a.example/1", "First", "2026-01-02", 10),
        ("https://a.example/1", "Second", "2026-01-03", 20),
    ]
    report = run(build_workbook(rows))

    finding = first_finding(report, ValidationCode.DUPLICATE_VALUE)
    assert finding.row == 2
    assert finding.column == "url"
    assert report.duplicate_counts == {"url": 1}


# --- header semantics (schema.py convention carry) ------------------------------


def test_header_match_is_case_sensitive() -> None:
    data = build_workbook(
        rows=list(DATA_ROWS), header=("URL", "title", "publish_time", "likes")
    )
    report = run(data)

    assert report.missing_required_columns == ("url",)
    assert report.extra_columns == ("URL",)


def test_header_edge_whitespace_trimmed() -> None:
    data = build_workbook(
        rows=list(DATA_ROWS), header=(" url ", "title", "publish_time ", "likes")
    )
    report = run(data)

    assert report.passed is True
    assert report.detected_columns == HEADER


def test_duplicate_header_names_error() -> None:
    data = build_workbook(
        rows=list(DATA_ROWS), header=("url", "url", "publish_time", "likes")
    )
    report = run(data)

    assert ValidationCode.DUPLICATE_HEADER in error_codes(report)


def test_extra_column_rejected_by_default() -> None:
    data = build_workbook(
        [("https://a.example/1", "First", "2026-01-02", 10, "note")],
        header=("url", "title", "publish_time", "likes", "notes"),
    )
    report = run(data)

    assert ValidationCode.EXTRA_COLUMN in error_codes(report)
    assert report.extra_columns == ("notes",)


def test_extra_column_allowed_when_configured() -> None:
    data = build_workbook(
        [("https://a.example/1", "First", "2026-01-02", 10, "note")],
        header=("url", "title", "publish_time", "likes", "notes"),
    )
    report = run(data, make_schema(allow_extra_columns=True))

    assert report.passed is True
    assert report.extra_columns == ("notes",)


# --- surface -----------------------------------------------------------------------


def test_accepts_file_path_and_pathlib_path(tmp_path: Path) -> None:
    as_str = tmp_path / "upload.xlsx"
    as_str.write_bytes(build_workbook())
    report = validate_xlsx(str(as_str), make_schema())
    assert report.passed is True

    as_path = tmp_path / "second.bin"  # extension is irrelevant
    as_path.write_bytes(build_workbook())
    report = validate_xlsx(as_path, make_schema())
    assert report.passed is True


def test_stream_is_rewound_not_consumed_blindly() -> None:
    stream = io.BytesIO(build_workbook())
    stream.seek(4)  # a caller mid-stream still validates from the start
    report = validate_xlsx(stream, make_schema())

    assert report.passed is True
    assert not stream.closed


def test_duration_ms_from_fake_clock() -> None:
    report = run(build_workbook(), clock=FakeClock(step=0.0))

    assert report.duration_ms == 0.0
