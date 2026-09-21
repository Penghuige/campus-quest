# backend/app/modules/submissions/validators/csv_validator.py
"""Streaming CSV submission validator (spec §12.1, §12.4;
backend-engineering §14).

``validate_csv(stream, schema, limits)`` turns an untrusted binary
stream into a structured ``ValidationReport`` — a validation outcome,
never an escaping exception. Pure unit work: no database, no services,
no wall clock (the monotonic clock is injectable).

Design decisions:

- **Streaming, always.** The file is decoded and parsed through
  chunked readers (``TextIOWrapper`` over a replay stream): no code
  path materializes the file. The only per-row state is counters,
  bounded finding samples, and the unique-value sets. A read-probe
  test pins that every single ``read()`` stays at chunk size.
- **Encoding policy (V1, controller decision):** UTF-8, with BOM
  (``utf-8-sig``), is the only accepted encoding; anything else fails
  with ``INVALID_ENCODING`` and a message asking for UTF-8. Spec §12.1
  permits either a clear rejection or a tested GB18030 fallback — V1
  ships the simpler rejection; a fallback would be added behind the
  same code surface. Detection is an early sniff: the first 8 KiB is
  run through an incremental ``utf-8-sig`` decoder (which buffers
  boundary-split multibyte characters, so a valid file is never
  falsely rejected) before any parse work starts; bytes that only
  turn invalid later still fail the same way during iteration.
- **Binary masquerade:** a NUL byte in the sniff chunk fails fast
  with ``BINARY_CONTENT`` (NUL never appears in CSV text, and it
  catches binaries that happen to decode as UTF-8); binaries that do
  not decode fail as ``INVALID_ENCODING``.
- **Dialect:** ``csv.Sniffer`` over the decoded sniff chunk,
  candidates restricted to ``, ; tab``. A ragged file defeats the
  sniffer's consistency check, so a sniff failure falls back to the
  first candidate delimiter whose header line contains a schema
  column name; a file readable under no candidate is rejected as
  ``MALFORMED_CSV`` (undialectable).
- **Field-size lesson from the plan-03 importer, carried:** csv's
  process-global field limit is raised (raise-only) above any cell
  the upload byte cap can admit, so an oversized cell PARSES and is
  classified as a row-level ``CELL_TOO_LONG`` instead of crashing the
  reader. Every ``csv.Error`` the strict reader still raises (an
  unterminated quote, ...) becomes a file-level ``MALFORMED_CSV``
  outcome.
- **Header matching carries the schema.py convention:** exact,
  case-SENSITIVE equality against the schema's column names, after
  edge-whitespace trim only. Missing required columns, extra columns
  (gated by ``allow_extra_columns``), and duplicate header names are
  each reported as errors.
- **Row budget:** the hard stop is ``min(limits.row_cap,
  schema.max_rows + 1)`` — past ``max_rows`` one row, no further row
  can change the verdict, so counting stops there while ``max_rows``
  itself is still checked at the end. ``min_rows`` is checked at the
  end; a header-only file fails with ``NO_DATA_ROWS`` (an empty
  dataset is never a valid submission, even without ``min_rows``)
  distinct from the zero-byte ``EMPTY_FILE`` — and a file whose every
  row is blank cells (`,,,`) is ``EMPTY_FILE`` too, the CSV twin of
  the XLSX empty-sheet verdict (§38.4; sub-F1: it previously bypassed
  the presence verdict entirely). ANY incomplete scan —
  row-cap stop, timeout, or a mid-file parse abort — emits a
  ``ROW_COUNT_TRUNCATED`` warning (``row_count`` is a lower bound)
  and skips the verdicts that need a completed scan (presence,
  ``min_rows``); a count already past ``max_rows``/the row cap stays
  conclusive.
- **Cooperative timeout:** every 4096 data rows the elapsed monotonic
  time is checked against ``limits.timeout_seconds``; on breach a
  ``TIMEOUT`` error is recorded and the scan stops. The clock is a
  parameter (``time.monotonic`` by default) so tests drive it.
- **Unique columns under bounded memory:** each unique column keeps a
  value set capped at ``UNIQUE_TRACKING_CAP``; at the cap the set
  freezes — repeats of already-seen values are still caught, repeats
  among unseen values are missed — and a warning marks
  ``duplicate_counts`` as a lower bound from that point on.
- Cells are classified per the shared contract documented in
  ``validators/common.py`` (edge-trimmed; whitespace-only is NULL).
  The stream is consumed but never closed — lifecycle belongs to the
  caller. ``OSError`` from the underlying read is deliberately NOT
  converted here: in-process callers see the raw exception. In the
  production path (the sandboxed child), the child ENTRY converts an
  escaping ``OSError`` into a terminal ``MALFORMED_CSV`` outcome — the
  file is a local temp the parent already downloaded, so the
  infrastructure-retry class is the parent's ``download_to_file``
  call alone.
"""

from __future__ import annotations

import codecs
import csv
import io
import time
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import ColumnRule, SubmissionSchema

from .common import (
    PreviewSpec,
    ScanAggregates,
    ValidationCode,
    ValidationLimits,
    ValidationReport,
    ValidationReportBuilder,
    analyze_header,
    check_row,
    finalize_scan,
)

__all__ = ["PARSER_VERSION", "validate_csv"]

#: Recorded in the §12.4 report's parser_version; bump on any change
#: to this module's parsing or classification semantics. csv-2: a file
#: whose every row is blank (all-empty cells) is now EMPTY_FILE
#: (sub-F1: it previously slipped past the has-header guard and could
#: pass machine validation with no min_rows).
PARSER_VERSION = "csv-2"

_SNIFF_BYTES = 8192
_CSV_DELIMITERS = ",;\t"
_TIMEOUT_CHECK_ROWS = 4096
#: See the module docstring's field-size lesson: a 200 MB pure-ASCII
#: upload holds at most ~200M characters, so 2**28 (268M) characters
#: sits above anything the byte cap can admit while still bounding a
#: single materialized cell.
_FIELD_SIZE_CEILING = 2**28

_ENCODING_MESSAGE = "文件编码必须是 UTF-8（可带 BOM）；请转换为 UTF-8 后重新上传"
_STRUCTURE_MESSAGE = "CSV 结构无法解析（例如未闭合的引号）"
_NO_DATA_MESSAGE = "CSV 只有表头，没有数据行"


class _ReplayStream(io.RawIOBase):
    """Raw byte stream replaying the sniffed prefix, then delegating.

    Serves only bounded reads (short reads are legal for a raw
    stream); an unbounded request is clamped to chunk size so the
    streaming guarantee cannot be defeated through this seam.
    ``readinto`` is the entry point ``io.BufferedReader`` uses
    (``RawIOBase`` leaves it unimplemented in Python 3.12).
    """

    def __init__(self, prefix: bytes, rest: BinaryIO) -> None:
        super().__init__()
        self._prefix = prefix
        self._rest = rest

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = _SNIFF_BYTES
        if not self._prefix:
            return self._rest.read(size)
        part, self._prefix = self._prefix[:size], self._prefix[size:]
        if len(part) < size:
            tail = self._rest.read(size - len(part))
            return part + (tail if tail else b"")
        return part

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def validate_csv(
    stream: BinaryIO,
    schema: SubmissionSchema,
    limits: ValidationLimits | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    preview: PreviewSpec | None = None,
) -> ValidationReport:
    """Validate a CSV byte stream against a parsed submission schema.

    The stream is read to (at most) the configured row budget and is
    never closed; every parse-level failure comes back inside the
    report (backend-engineering §14). ``preview`` collects the first N
    data rows into the report (§12.4 安全预览) — plain truncated cells,
    never evaluated anything.
    """
    effective_limits = ValidationLimits() if limits is None else limits
    started = clock()
    builder = ValidationReportBuilder(
        parser_version=PARSER_VERSION, file_type=FileType.CSV, preview=preview
    )
    aggregates = _scan(stream, schema, effective_limits, builder, clock, started)
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


def _scan(
    stream: BinaryIO,
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    clock: Callable[[], float],
    started: float,
) -> ScanAggregates:
    """Bounded prefix sniff, then the streaming row scan."""
    prefix = stream.read(_SNIFF_BYTES)
    if not prefix:
        builder.add_error(ValidationCode.EMPTY_FILE, "CSV 文件为空（没有表头）")
        return ScanAggregates()
    if b"\x00" in prefix:
        builder.add_error(
            ValidationCode.BINARY_CONTENT,
            "文件包含二进制内容（检测到 NUL 字节），不是文本 CSV",
        )
        return ScanAggregates()
    try:
        sample = _decode_sniff(prefix)
    except UnicodeDecodeError:
        builder.add_error(ValidationCode.INVALID_ENCODING, _ENCODING_MESSAGE)
        return ScanAggregates()
    if not sample.strip():
        builder.add_error(ValidationCode.EMPTY_FILE, "CSV 文件为空（没有表头）")
        return ScanAggregates()
    _ensure_field_size_limit()
    dialect = _sniff_dialect(sample, schema)
    if dialect is None:
        builder.add_error(ValidationCode.MALFORMED_CSV, "无法识别 CSV 分隔符格式")
        return ScanAggregates()
    # newline="" keeps \r\n intact for the csv reader; errors="strict"
    # turns later invalid bytes into UnicodeDecodeError inside the
    # iteration, converted to a validation outcome in _consume.
    text = io.TextIOWrapper(
        io.BufferedReader(_ReplayStream(prefix, stream), buffer_size=_SNIFF_BYTES),
        encoding="utf-8-sig",
        errors="strict",
        newline="",
    )
    try:
        reader = csv.reader(text, dialect=dialect, strict=True)
        return _consume(reader, schema, limits, builder, clock, started)
    finally:
        text.close()


def _decode_sniff(prefix: bytes) -> str:
    """Decode the sniff chunk, tolerating a boundary-split character."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    return decoder.decode(prefix)


def _ensure_field_size_limit() -> None:
    """Raise csv's process-global field limit (raise-only, plan-03
    carry) so an oversized cell parses and is classified row-level."""
    if csv.field_size_limit() < _FIELD_SIZE_CEILING:
        csv.field_size_limit(_FIELD_SIZE_CEILING)


def _sniff_dialect(
    sample: str, schema: SubmissionSchema
) -> csv.Dialect | type[csv.Dialect] | None:
    """Sniff the dialect, falling back to a header-candidate scan."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=_CSV_DELIMITERS)
    except csv.Error:
        pass
    lines = sample.splitlines()
    header_line = lines[0] if lines else ""
    for delimiter in _CSV_DELIMITERS:
        cells = header_line.split(delimiter)
        if any(cell.strip() in schema.columns for cell in cells):
            dialect: csv.Dialect = type(
                "HeaderMatch", (csv.excel,), {"delimiter": delimiter}
            )()
            return dialect
    return None


def _consume(
    reader: Iterator[list[str]],
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    clock: Callable[[], float],
    started: float,
) -> ScanAggregates:
    """Stream the rows, accumulating bounded state only."""
    header_cells: list[str] | None = None
    plan: list[tuple[int, ColumnRule]] = []
    missing: list[str] = []
    extra: list[str] = []
    row_count = 0
    scan_incomplete = False
    null_counts: dict[str, int] = {}
    type_errors: dict[str, int] = {}
    duplicate_counts: dict[str, int] = {}
    unique_seen: dict[str, set[str]] = {}
    degraded_columns: set[str] = set()
    hard_cap = (
        limits.row_cap
        if schema.max_rows is None
        else min(limits.row_cap, schema.max_rows + 1)
    )

    try:
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue  # blank line: not a spreadsheet row
            if header_cells is None:
                candidate = [cell.strip() for cell in raw]
                analysis = analyze_header(candidate, schema, limits, builder)
                if analysis is None:
                    return ScanAggregates(
                        detected_columns=tuple(candidate[: limits.max_columns])
                    )
                header_cells = candidate
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
            check_row(
                raw,
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
            builder.add_preview_row(raw)
    except csv.Error:
        builder.add_error(ValidationCode.MALFORMED_CSV, _STRUCTURE_MESSAGE)
        scan_incomplete = True
    except UnicodeDecodeError:
        builder.add_error(ValidationCode.INVALID_ENCODING, _ENCODING_MESSAGE)
        scan_incomplete = True

    if header_cells is None and not scan_incomplete:
        # Every row was blank cells (e.g. b",,,\n,,,\n,,,\n"): the file
        # carries no header and no data — content-free, so it must fail
        # exactly like the XLSX path's empty sheet (§38.4 空/仅 header
        # 必须失败). Without this, finalize_scan's ``header_cells is
        # not None`` guard skipped NO_DATA_ROWS and a schema without
        # min_rows let the file PASS machine validation (sub-F1: a
        # locked PROVISIONAL reward on an empty upload). A mid-file
        # abort (scan_incomplete) already recorded its own error and
        # says nothing about the rest of the file, so it is excluded.
        builder.add_error(ValidationCode.EMPTY_FILE, "CSV 文件为空（没有表头）")

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
