# backend/app/modules/submissions/validators/xlsx_validator.py
"""Read-only XLSX submission validator (spec §12.2, §12.4;
backend-engineering §14).

``validate_xlsx(path_or_stream, schema, limits)`` turns an untrusted
XLSX file into a structured ``ValidationReport`` — a validation
outcome, never an escaping exception. Pure unit work: no database, no
services, no wall clock (the monotonic clock is injectable).

Design decisions (each pinned by a test):

- **ZIP preflight before openpyxl (spec §12.2 zip-bomb defense).**
  ``zipfile`` reads every entry's DECLARED sizes from the central
  directory without decompressing anything, so the whole bomb family
  is rejected before a single entry inflates:

  - entry count > 4096 -> ``ARCHIVE_TOO_LARGE``;
  - declared total uncompressed > 512 MB -> ``ARCHIVE_TOO_LARGE``;
  - sharedStrings part > 128 MB -> ``ARCHIVE_TOO_LARGE`` (openpyxl
    materializes that table as Python strings — measured ~4x the XML
    size — so it gets a dedicated, tighter cap);
  - per-entry OR aggregate compression ratio > 100:1 ->
    ``SUSPICIOUS_COMPRESSION_RATIO`` (deflate's theoretical ceiling is
    ~1032:1, zeros hit ~1027:1; measured legitimate workbook XML sits
    at 3.7-7:1, a million empty ``<row/>`` elements at ~7:1);
  - suspicious entry names (absolute paths, ``..`` components, drive
    letters) -> ``SUSPICIOUS_ARCHIVE_ENTRY`` — we never extract, but
    an archive that would be dangerous to anyone who does is not
    admitted (defense in depth).

  zipfile resolves zip64 extended-size records transparently, so the
  declared sizes are the true ones and the caps bound whatever a
  zip64 archive can claim; no special zip64 path exists or is needed.
- **Read-only, data-only, formulas never executed (spec §12.2).**
  openpyxl opens the workbook with ``read_only=True`` (streaming XML
  parse) and ``data_only=True`` (the CACHED ``<v>`` result of a
  formula, never the formula). ``keep_links=False`` so external links
  are not even loaded; their mere PRESENCE (a
  ``xl/externalLinks/...`` entry) is an ``EXTERNAL_LINK`` warning —
  nothing is ever fetched. A formula cell whose cached value is
  missing (openpyxl's own writer, LibreOffice recalc-off, ...) reads
  as ``None`` and is distinguishable from an absent cell: it is
  classified as NULL (null counts, NULL_VIOLATION for non-nullable
  columns) plus one ``FORMULA_WITHOUT_CACHED_VALUE`` warning per
  affected column — the honest answer for a value we cannot know
  without executing untrusted code.
- **Sheet selection (spec §12.2):** ``source_selector.sheet_name``
  picks that sheet (exact, case-SENSITIVE match — the schema.py
  convention); a missing sheet fails ``SHEET_NOT_FOUND`` listing the
  available names. No selector -> the first VISIBLE sheet; a workbook
  with none fails the same way.
- **Bounded iteration, not bounded by declarations:** read-only
  iteration follows the rows actually present in the sheet XML, so a
  stale ``<dimension ref="A1:XFD1048576"/>`` costs nothing; every row
  is additionally clamped to ``max_columns + 1`` columns so the
  padding read-only mode adds up to the declared width cannot
  materialize 16k-cell tuples. A header wider than ``max_columns``
  aborts with ``TOO_MANY_COLUMNS``.
- **Empty rows:** skipped without counting (a blank spreadsheet row
  is not a data row, mirroring CSV blank lines); a run of
  ``_MAX_EMPTY_ROW_STREAK`` (1024) consecutive empty rows ends the
  scan — bounded time on the million-empty-row sheet. Data after
  such a run is not read; that is the documented trade-off, and the
  scan is NOT marked incomplete (trailing styled-empty padding is
  normal in real workbooks).
- **Row budget / timeout / unique tracking / bounded findings:** the
  shared scan layer in ``validators/common.py`` (``analyze_header``,
  ``check_row``, ``finalize_scan``) — same semantics as the CSV
  validator, including the scan_incomplete contract: any abort makes
  ``row_count`` a lower bound, warns ``ROW_COUNT_TRUNCATED``, and
  skips every verdict that needs the full count (presence,
  ``min_rows``, ``NULL_RATIO_EXCEEDED``). The cooperative timeout is
  checked every 1024 data rows against the injected clock.
- **Cell values** are rendered to their Python string form (``str()``
  of int/float/datetime/bool/error-text) and classified by the shared
  contract in ``common.py``; Excel booleans ``True``/``False`` fall to
  the boolean vocabulary's case-insensitive side, datetime cells
  render as ISO-8601 the datetime grammar accepts, and error cells
  (``#DIV/0!``) fail any typed column as ``TYPE_ERROR``. A row
  SHORTER than the header pads with nulls (missing cells are empty
  cells in Excel semantics — NOT the CSV ragged-row error); a longer
  row is ``ROW_SHAPE_MISMATCH``.
- **Failure surface:** a non-``PK`` magic or any ``BadZipFile`` /
  openpyxl parse failure becomes ``MALFORMED_XLSX``; the openpyxl
  phase catches ``Exception`` broadly BY DESIGN (an untrusted
  workbook must never crash the worker; openpyxl's malformed-file
  exceptions span ``BadZipFile``, XML ``ParseError``, ``KeyError`` on
  missing parts, encrypted-archive ``RuntimeError``, ...) with
  ``OSError`` deliberately re-raised — storage failures are
  infrastructure the worker retries, not a property of the file
  (same policy as the CSV validator).
- **Bounded reads:** the caller's stream is wrapped in a clamping
  proxy (``_BoundedReads``) so no code path — zipfile's end-of-
  central-directory probe issues an unbounded ``read()`` — can slurp
  the whole file; a read-probe test pins it on a multi-MB fixture.
  Streams must be seekable (zipfile and openpyxl both require it) and
  are rewound to 0 at entry; the caller's stream is never closed. A
  path input is opened by the validator and closed when done (the
  extension is irrelevant — openpyxl's own extension gate is bypassed
  by handing it the stream).
"""

from __future__ import annotations

import io
import os
import struct
import time
import zipfile
from collections.abc import Callable
from typing import Any, BinaryIO

from openpyxl import load_workbook
from openpyxl.cell.read_only import EmptyCell, ReadOnlyCell

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import ColumnRule, SubmissionSchema

from .common import (
    ScanAggregates,
    ValidationCode,
    ValidationLimits,
    ValidationReport,
    ValidationReportBuilder,
    analyze_header,
    check_row,
    finalize_scan,
)

__all__ = ["PARSER_VERSION", "validate_xlsx"]

#: Recorded in the §12.4 report's parser_version; bump on any change
#: to this module's parsing or classification semantics.
PARSER_VERSION = "xlsx-1"

#: Preflight caps (see the module docstring for the reasoning).
_MAX_ARCHIVE_ENTRIES = 4096
_MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
_MAX_SHARED_STRINGS_XML = 128 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100.0

#: Scan bounds.
_MAX_EMPTY_ROW_STREAK = 1024
_TIMEOUT_CHECK_ROWS = 1024

#: The largest single read any phase may issue through the validator
#: (T4's clamping-replay lesson, applied to zipfile/openpyxl). One
#: megabyte comfortably covers the largest central directory a
#: 4096-entry legitimate workbook can have (~400 KB); a declared-larger
#: directory is treated by zipfile as corruption -> MALFORMED_XLSX.
_READ_CHUNK = 1024 * 1024

_NO_DATA_MESSAGE = "XLSX 工作表只有表头，没有数据行"
_EMPTY_SHEET_MESSAGE = "XLSX 工作表为空（没有任何内容）"
_NOT_XLSX_MESSAGE = "文件不是有效的 XLSX（ZIP）归档"
_UNPARSEABLE_MESSAGE = "工作簿无法解析（结构损坏或不是有效的 XLSX）"

Source = str | os.PathLike[str] | BinaryIO


class _BoundedReads(io.RawIOBase):
    """Raw wrapper clamping every read to ``_READ_CHUNK`` bytes.

    Short reads are legal for a raw stream; zipfile's end-of-archive
    probe issues an unbounded ``read()`` after seeking near the end,
    and a lying central directory can request gigabytes — the clamp
    bounds both without changing what well-formed archives see.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = _READ_CHUNK
        return self._stream.read(min(size, _READ_CHUNK))

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()


def validate_xlsx(
    path_or_stream: Source,
    schema: SubmissionSchema,
    limits: ValidationLimits | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ValidationReport:
    """Validate an XLSX file (path or seekable binary stream) against
    a parsed submission schema.

    The file is read to (at most) the configured row budget; a caller
    stream is rewound and never closed; every parse-level failure
    comes back inside the report (backend-engineering §14).
    """
    effective_limits = ValidationLimits() if limits is None else limits
    started = clock()
    builder = ValidationReportBuilder(
        parser_version=PARSER_VERSION, file_type=FileType.XLSX
    )
    aggregates = _run(path_or_stream, schema, effective_limits, builder, clock, started)
    duration_ms = round((clock() - started) * 1000.0, 3)
    return builder.build(
        row_count=aggregates.row_count,
        detected_columns=aggregates.detected_columns,
        missing_required_columns=aggregates.missing_required_columns,
        extra_columns=aggregates.extra_columns,
        type_error_counts=aggregates.type_error_counts,
        null_ratios=aggregates.null_ratios,
        duplicate_counts=aggregates.duplicate_counts,
        duration_ms=duration_ms,
    )


def _run(
    source: Source,
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    clock: Callable[[], float],
    started: float,
) -> ScanAggregates:
    """Sniff, preflight, open, scan — every failure into the builder."""
    stream, owns_file = _open_source(source)
    try:
        stream.seek(0)
        head = stream.read(4)
        stream.seek(0)
        if not head:
            builder.add_error(ValidationCode.EMPTY_FILE, "XLSX 文件为空")
            return ScanAggregates()
        if not head.startswith(b"PK"):
            builder.add_error(ValidationCode.MALFORMED_XLSX, _NOT_XLSX_MESSAGE)
            return ScanAggregates()
        reader = _BoundedReads(stream)
        if not _zip_preflight(reader, builder):
            return ScanAggregates()
        reader.seek(0)
        return _parse(reader, schema, limits, builder, clock, started)
    finally:
        if owns_file:
            stream.close()


def _open_source(source: Source) -> tuple[BinaryIO, bool]:
    """Normalize path/stream input; returns (stream, close_when_done).

    ``OSError`` (missing path, storage) propagates deliberately — an
    infrastructure failure is not a property of the file.
    """
    if isinstance(source, (str, os.PathLike)):
        return open(source, "rb"), True
    return source, False


def _zip_preflight(reader: _BoundedReads, builder: ValidationReportBuilder) -> bool:
    """Declared-size and layout checks before anything is decompressed.

    ``False`` means a fatal violation was recorded; warnings (external
    links) do not abort. First fatal violation wins, deterministic
    order: entry count, then per-entry (name, sharedStrings size,
    ratio), then totals (size, aggregate ratio).
    """
    try:
        archive = zipfile.ZipFile(reader)
    except zipfile.BadZipFile:
        builder.add_error(ValidationCode.MALFORMED_XLSX, _NOT_XLSX_MESSAGE)
        return False
    try:
        infos = archive.infolist()
    except (zipfile.BadZipFile, struct.error):
        builder.add_error(ValidationCode.MALFORMED_XLSX, _NOT_XLSX_MESSAGE)
        return False
    finally:
        archive.close()

    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        builder.add_error(
            ValidationCode.ARCHIVE_TOO_LARGE,
            f"归档条目数 {len(infos)} 超过上限 {_MAX_ARCHIVE_ENTRIES}",
        )
        return False

    total_uncompressed = 0
    total_compressed = 0
    external_links = False
    for info in infos:
        name = info.filename
        if _is_suspicious_name(name):
            builder.add_error(
                ValidationCode.SUSPICIOUS_ARCHIVE_ENTRY,
                f"归档内条目路径可疑: {name!r}",
            )
            return False
        if name.startswith("xl/externalLinks/"):
            external_links = True
        if name.startswith("xl/sharedStrings") and info.file_size > (
            _MAX_SHARED_STRINGS_XML
        ):
            builder.add_error(
                ValidationCode.ARCHIVE_TOO_LARGE,
                f"sharedStrings 部分 {info.file_size} 字节超过上限"
                f" {_MAX_SHARED_STRINGS_XML}，已拒绝解析",
            )
            return False
        if info.file_size and (
            info.compress_size == 0
            or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
        ):
            builder.add_error(
                ValidationCode.SUSPICIOUS_COMPRESSION_RATIO,
                f"条目 {name} 的压缩比超过 {_MAX_COMPRESSION_RATIO}:1"
                f"（声明解压后 {info.file_size} 字节）",
            )
            return False
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
    if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED:
        builder.add_error(
            ValidationCode.ARCHIVE_TOO_LARGE,
            f"归档声明解压后总大小 {total_uncompressed} 字节超过上限"
            f" {_MAX_TOTAL_UNCOMPRESSED}",
        )
        return False
    if total_compressed and (
        total_uncompressed / total_compressed > _MAX_COMPRESSION_RATIO
    ):
        builder.add_error(
            ValidationCode.SUSPICIOUS_COMPRESSION_RATIO,
            f"归档整体压缩比超过 {_MAX_COMPRESSION_RATIO}:1",
        )
        return False
    if external_links:
        builder.add_warning(
            ValidationCode.EXTERNAL_LINK,
            "工作簿包含指向外部文件的链接；校验过程不会访问任何外部资源",
        )
    return True


def _is_suspicious_name(name: str) -> bool:
    """Extraction-hazard entry names, checked before anything opens."""
    if not name or name.startswith(("/", "\\")):
        return True
    if len(name) > 1 and name[1] == ":":  # Windows drive letter
        return True
    return ".." in name.replace("\\", "/").split("/")


def _parse(
    reader: _BoundedReads,
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    clock: Callable[[], float],
    started: float,
) -> ScanAggregates:
    """Open the workbook read-only/data-only and scan the sheet.

    The broad ``except Exception`` is the §14 contract: openpyxl's
    malformed-file surface spans many exception types and an untrusted
    workbook must never crash the worker. ``OSError`` stays
    infrastructure (re-raised).
    """
    workbook: Any = None
    try:
        workbook = load_workbook(
            reader, read_only=True, data_only=True, keep_links=False
        )
        worksheet = _select_sheet(workbook, schema, builder)
        if worksheet is None:
            return ScanAggregates()
        return _scan_sheet(worksheet, schema, limits, builder, clock, started)
    except OSError:
        raise
    except Exception:
        builder.add_error(ValidationCode.MALFORMED_XLSX, _UNPARSEABLE_MESSAGE)
        return ScanAggregates()
    finally:
        if workbook is not None:
            workbook.close()


def _select_sheet(
    workbook: Any, schema: SubmissionSchema, builder: ValidationReportBuilder
) -> Any | None:
    """Selector sheet if named (exact, case-sensitive); else first visible."""
    selector: str | None = None
    if schema.source_selector is not None:
        selector = schema.source_selector.sheet_name
    if selector is not None:
        if selector not in workbook.sheetnames:
            available = ", ".join(workbook.sheetnames[:10])
            builder.add_error(
                ValidationCode.SHEET_NOT_FOUND,
                f"找不到名为 {selector!r} 的工作表；可用工作表: {available}",
            )
            return None
        return workbook[selector]
    for name in workbook.sheetnames:
        if workbook[name].sheet_state == "visible":
            return workbook[name]
    builder.add_error(
        ValidationCode.SHEET_NOT_FOUND,
        "工作簿中没有可见的工作表",
    )
    return None


def _scan_sheet(
    worksheet: Any,
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    clock: Callable[[], float],
    started: float,
) -> ScanAggregates:
    """Stream the sheet's rows through the shared scan layer."""
    header_cells: list[str] | None = None
    plan: list[tuple[int, ColumnRule]] = []
    missing: list[str] = []
    extra: list[str] = []
    row_count = 0
    scan_incomplete = False
    empty_streak = 0
    null_counts: dict[str, int] = {}
    type_errors: dict[str, int] = {}
    duplicate_counts: dict[str, int] = {}
    unique_seen: dict[str, set[str]] = {}
    degraded_columns: set[str] = set()
    formula_nulls: dict[str, int] = {}
    hard_cap = (
        limits.row_cap
        if schema.max_rows is None
        else min(limits.row_cap, schema.max_rows + 1)
    )
    # max_columns + 1: enough to PROVE a header exceeds the cap without
    # ever materializing a wider row.
    rows = worksheet.iter_rows(min_row=1, max_col=limits.max_columns + 1)

    for cells in rows:
        # Read-only pads rows to the declared width with EmptyCell;
        # trailing padding is not content.
        end = len(cells)
        while end > 0 and isinstance(cells[end - 1], EmptyCell):
            end -= 1
        texts = [_cell_text(cell) for cell in cells[:end]]
        if not any(texts):
            empty_streak += 1
            if empty_streak >= _MAX_EMPTY_ROW_STREAK:
                break  # documented: treated as end of sheet data
            continue
        empty_streak = 0
        if header_cells is None:
            # Header names match the schema after edge-whitespace trim
            # (the schema.py convention; the CSV validator does the
            # same on its candidate header).
            texts = [value.strip() for value in texts]
            while texts and texts[-1] == "":
                texts.pop()  # trailing empty header cells
            analysis = analyze_header(texts, schema, limits, builder)
            if analysis is None:
                return ScanAggregates(
                    detected_columns=tuple(texts[: limits.max_columns])
                )
            header_cells = texts
            plan, missing, extra = analysis
            continue
        row_count += 1
        if row_count > hard_cap:
            scan_incomplete = True
            break
        if (
            row_count % _TIMEOUT_CHECK_ROWS == 0
            and clock() - started > limits.timeout_seconds
        ):
            builder.add_error(
                ValidationCode.TIMEOUT,
                f"解析超过时间上限 {limits.timeout_seconds} 秒，已停止",
            )
            scan_incomplete = True
            break
        if len(texts) < len(header_cells):
            # Excel semantics: missing trailing cells are empty cells.
            texts += [""] * (len(header_cells) - len(texts))
        _count_formula_nulls(cells[:end], texts, header_cells, schema, formula_nulls)
        check_row(
            texts,
            row_count,
            header_cells,
            plan,
            limits,
            builder,
            null_counts,
            type_errors,
            duplicate_counts,
            unique_seen,
            degraded_columns,
        )

    if header_cells is None:
        # No non-empty row at all: the selected sheet carries no
        # content — not even a header (distinct from a header-only
        # sheet, which finalize_scan reports as NO_DATA_ROWS).
        builder.add_error(ValidationCode.EMPTY_FILE, _EMPTY_SHEET_MESSAGE)

    for name, count in formula_nulls.items():
        if count:
            builder.add_warning(
                ValidationCode.FORMULA_WITHOUT_CACHED_VALUE,
                f"列 {name} 有 {count} 个单元格读不到值（通常是无缓存结果的"
                "公式；系统不执行公式），已按空值处理",
                column=name,
            )
    return finalize_scan(
        header_cells=header_cells,
        plan=plan,
        missing=missing,
        extra=extra,
        row_count=row_count,
        scan_incomplete=scan_incomplete,
        null_counts=null_counts,
        type_errors=type_errors,
        duplicate_counts=duplicate_counts,
        schema=schema,
        limits=limits,
        builder=builder,
        no_data_message=_NO_DATA_MESSAGE,
    )


def _cell_text(cell: Any) -> str:
    """The shared classification contract's string form of one cell.

    ``EmptyCell``/empty cells (``value is None``) are NULLs; every
    other value renders through ``str()`` — int/float keep their
    literal digits, datetimes render ISO-8601, booleans ``True`` /
    ``False`` (the boolean vocabulary is case-insensitive), error
    cells arrive as their ``#...`` text.
    """
    value = cell.value
    if value is None:
        return ""
    return str(value)


def _count_formula_nulls(
    cells: tuple[Any, ...],
    texts: list[str],
    header_cells: list[str],
    schema: SubmissionSchema,
    formula_nulls: dict[str, int],
) -> None:
    """Per-column tally of value-bearing cells that read as None.

    A cell PRESENT in the XML whose ``data_only`` value is None is a
    formula without a cached result (openpyxl cannot expose the
    formula itself under ``data_only=True``); absent cells are
    ``EmptyCell`` and stay silent nulls.
    """
    for index, cell in enumerate(cells):
        if index >= len(texts):
            break
        if texts[index] == "" and isinstance(cell, ReadOnlyCell):
            name = header_cells[index]
            if name in schema.columns:
                formula_nulls[name] = formula_nulls.get(name, 0) + 1
