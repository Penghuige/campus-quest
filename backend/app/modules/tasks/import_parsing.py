# backend/app/modules/tasks/import_parsing.py
"""Assignment CSV parsing and row validation (spec §7.1;
backend-engineering §14).

Single responsibility: turn untrusted upload bytes into a structured
validation outcome — canonicalized valid rows plus per-row errors, or
exactly one file-level rejection. Pure code: no database, no Redis, no
clock. The preview/confirm orchestration lives in ``importer``.

Design decisions:

- **CSV only for V1** (the binding minimum; spec §7.1 says Teacher 可
  CSV/XLSX 导入). The XLSX seam is ``_parse_csv``: everything downstream
  of it speaks already-canonicalized ``(platform, keyword)`` pairs, so an
  XLSX parser is a sibling reader in front of the same
  validate/store/confirm pipeline — no other change needed.
- **Teacher uploads are untrusted** (backend-engineering §14): bounded
  file size — the byte cap fast-fails BEFORE any parsing, which is what
  bounds all downstream parse work (the reader itself materializes the
  whole decoded file); bounded row count (validation stops one row past
  the cap); bounded cell length (``csv.field_size_limit`` is raised
  above the byte cap by ``ensure_field_size_limit`` so an oversized cell
  parses and reports TEXT_TOO_LONG instead of crashing the reader);
  strict UTF-8 decode; and no formula execution — CSV cells are stored
  as plain text data, never interpreted. Every ``csv.Error`` the strict
  reader still raises (e.g. an unterminated quote) is converted into a
  file-level MALFORMED_CSV validation outcome — parser exceptions never
  escape this module.
- **Row numbers are 1-based data-row indexes** (the header is not a data
  row; blank lines are skipped without consuming a row number, matching
  what a teacher sees in a spreadsheet).
- **File-level vs row-level errors.** Oversize, bad encoding, an
  undialectable file, a wrong header, or exceeding the per-import row cap
  rejects the whole preview and mints NO token — nothing in such a file
  is confirmable. Row-level errors keep their valid rows importable.
- Canonicalization: platform is case-insensitively mapped onto the
  controlled codes (the models' CHECK set); keyword is edge-trimmed only
  (``str.strip`` removes Unicode edge whitespace) with internal Unicode —
  spacing, composed/decomposed forms — preserved byte-exactly, because
  spec §7 compares keyword 按精确文本 and the UNIQUE constraint dedups
  on exactly what we store.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from app.integrations.zip_safety import (
    ZipSafetyKind,
    ZipSafetyViolation,
    preflight,
)

# Canonical platform codes (spec §7): case-insensitive input, exact output.
# Parity with the assignments CHECK constraint set is pinned by tests.
SUPPORTED_IMPORT_PLATFORMS: frozenset[str] = frozenset(
    {"xiaohongshu", "douyin", "zhihu"}
)

# Dialect sniffing: restrict the candidate delimiters so a one-column file
# cannot be sniffed as something odd, and bound the sample size.
_CSV_DELIMITERS = ",;\t"
_SNIFF_SAMPLE_BYTES = 8192
_HEADER = ("platform", "keyword")
# Cap on raw values echoed into preview error DTOs (§14 safe truncation).
_ECHO_MAX_LENGTH = 64
# The ZIP local-header magic routes xlsx bytes to the openpyxl reader
# (the submission validator's same convention; the byte cap already
# bounds the archive before openpyxl sees it).
_XLSX_LOCAL_MAGIC = b"PK\x03\x04"


# --- stable row/file error codes (spec §7.1 detection list) -----------------------


class ImportErrorCode(StrEnum):
    """Stable code for every preview error; ``value == member name``.

    ``FILE_TOO_LARGE`` reuses the §29 registry code name for oversize
    uploads; the rest are import-specific and travel only inside the
    preview DTO (the API layer surfaces them verbatim).
    """

    EMPTY_PLATFORM = "EMPTY_PLATFORM"
    EMPTY_KEYWORD = "EMPTY_KEYWORD"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    DUPLICATE_IN_FILE = "DUPLICATE_IN_FILE"
    DUPLICATE_IN_DB = "DUPLICATE_IN_DB"
    TEXT_TOO_LONG = "TEXT_TOO_LONG"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    INVALID_ENCODING = "INVALID_ENCODING"
    MALFORMED_CSV = "MALFORMED_CSV"
    MALFORMED_XLSX = "MALFORMED_XLSX"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"


# --- messages (§29 envelope text) --------------------------------------------------

_FILE_TOO_LARGE_MESSAGE = "导入文件超过大小上限"
_INVALID_ENCODING_MESSAGE = "文件编码必须是 UTF-8"
_BAD_DIALECT_MESSAGE = "无法识别 CSV 分隔符格式"
_BAD_STRUCTURE_MESSAGE = "CSV 结构无法解析（例如未闭合的引号）"
_BAD_HEADER_MESSAGE = "CSV 表头必须是 platform,keyword"
_EMPTY_FILE_MESSAGE = "文件没有数据行"
_MALFORMED_XLSX_MESSAGE = "XLSX 工作簿无法解析（或不是有效的 xlsx 文件）"
_XLSX_HEADER_MESSAGE = "XLSX 表头必须恰好是 platform 和 keyword 两列"
_ROW_COLUMNS_MESSAGE = "每行必须恰好是 platform,keyword 两列"
_EMPTY_PLATFORM_MESSAGE = "platform 不能为空"
_EMPTY_KEYWORD_MESSAGE = "keyword 不能为空"
_UNSUPPORTED_PLATFORM_MESSAGE = "不支持的 platform"
_DUPLICATE_IN_FILE_MESSAGE = "文件内重复的 platform + keyword 组合"
_DUPLICATE_IN_DB_MESSAGE = "与任务现有 Assignment 重复"
_TEXT_TOO_LONG_MESSAGE = "keyword 超过最大长度"
_LIMIT_EXCEEDED_MESSAGE = "单次导入行数超过上限"


def ensure_field_size_limit(max_file_bytes: int) -> None:
    """Raise ``csv``'s process-global field limit above the byte cap.

    csv's default field limit (131072 chars) would turn one large cell
    into a csv.Error crash; raised above the byte cap, every cell the
    size gate admitted is parseable and the row check can classify it as
    TEXT_TOO_LONG instead (engineering §14: parser exceptions become
    validation outcomes). UTF-8 guarantees #chars <= #bytes, so the byte
    cap bounds the char count. The limit is process-global, hence
    raise-only: a small-caps instance must never tighten it for others.
    """
    if max_file_bytes > csv.field_size_limit():
        csv.field_size_limit(max_file_bytes)


def _header_match_dialect(text: str) -> csv.Dialect | None:
    """The first candidate delimiter whose header line reads
    ``platform,keyword`` (the sniff-failure fallback; see _parse_csv)."""
    header_line = text.splitlines()[0] if text.splitlines() else ""
    for delimiter in _CSV_DELIMITERS:
        if (
            tuple(cell.strip().lower() for cell in header_line.split(delimiter))
            == _HEADER
        ):
            # Sniffer's own dynamically-built dialect, same trick.
            dialect: csv.Dialect = type(
                "HeaderMatch", (csv.excel,), {"delimiter": delimiter}
            )()
            return dialect
    return None


def _parse_xlsx(
    data: bytes,
    *,
    max_rows: int,
) -> tuple[list[list[str]] | None, AssignmentImportError | None]:
    """Read the first visible worksheet's cells as plain text (QA #19).

    The sibling of ``_parse_csv`` returning the same shape: rows minus
    the header, or one file-level rejection. openpyxl runs read-only +
    data-only + no links (the submission validator's same posture —
    formulas never execute, cached values ride as text); every openpyxl
    failure surface (not a zip, a corrupt sheet, a weird sharedStrings
    table) lands as one MALFORMED_XLSX rejection — parser exceptions
    never escape this module.

    Security round F3, two guards before/around openpyxl:

    - the shared ZIP preflight (``app.integrations.zip_safety``,
      default-deny — no streaming exemptions on the import path) reads
      the central directory's DECLARED sizes and rejects lying
      directories, bombs, and extraction-hazard names before any
      member is decompressed;
    - the row cap fires DURING enumeration: a sheet that never ends is
      cut off one row past ``max_rows`` data rows instead of
      materializing first and classifying after. Blank rows stay
      non-rows (the ``_classify_rows`` semantics mirrored in-loop).
    """
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile:
        return None, AssignmentImportError(
            ImportErrorCode.MALFORMED_XLSX, _MALFORMED_XLSX_MESSAGE
        )
    violation = preflight(infos)
    if violation is not None:
        return None, _zip_safety_rejection(violation)

    from typing import Any, BinaryIO

    from openpyxl import load_workbook

    workbook: Any = None
    try:
        reader: BinaryIO = io.BytesIO(data)
        workbook = load_workbook(
            reader, read_only=True, data_only=True, keep_links=False
        )
        sheet = workbook.worksheets[0]
        rows: list[list[str]] = []
        nonblank = 0  # header included; data rows = nonblank - 1
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if value is None else str(value) for value in row]
            if any(cell.strip() for cell in cells):
                nonblank += 1
                if nonblank - 1 > max_rows:
                    return None, AssignmentImportError(
                        ImportErrorCode.LIMIT_EXCEEDED,
                        _LIMIT_EXCEEDED_MESSAGE,
                        details={"rows": max_rows + 1, "limit": max_rows},
                    )
            rows.append(cells)
    except Exception:
        return None, AssignmentImportError(
            ImportErrorCode.MALFORMED_XLSX, _MALFORMED_XLSX_MESSAGE
        )
    finally:
        if workbook is not None:
            workbook.close()

    if not rows or not any(any(cell.strip() for cell in row) for row in rows):
        return None, AssignmentImportError(
            ImportErrorCode.MALFORMED_XLSX, _EMPTY_FILE_MESSAGE
        )
    header = tuple(cell.strip().lower() for cell in rows[0])
    if header[:2] != _HEADER or len(header) != 2:
        return None, AssignmentImportError(
            ImportErrorCode.MALFORMED_XLSX, _XLSX_HEADER_MESSAGE
        )
    return rows[1:], None


def _zip_safety_rejection(
    violation: ZipSafetyViolation,
) -> AssignmentImportError:
    """Map one shared ZIP-safety violation onto the import vocabulary.

    One MALFORMED_XLSX verdict whose message names the exact rule and
    numbers (the import surface has no per-rule codes; the message is
    the teacher-facing explanation, same posture as the submission
    validator's findings).
    """
    if violation.kind is ZipSafetyKind.TOO_MANY_ENTRIES:
        text = f"归档条目数 {violation.entries} 超过上限 4096"
    elif violation.kind is ZipSafetyKind.SUSPICIOUS_NAME:
        text = f"归档内条目路径可疑: {violation.name!r}"
    elif violation.kind is ZipSafetyKind.PART_TOO_LARGE:
        text = (
            f"归档部件 {violation.name} 声明解压后 {violation.file_size} 字节"
            "超过单部件上限 16 MiB"
        )
    elif violation.kind is ZipSafetyKind.COMPRESSION_RATIO:
        text = (
            f"条目 {violation.name} 的压缩比超过 100:1"
            f"（声明解压后 {violation.file_size} 字节）"
        )
    elif violation.kind is ZipSafetyKind.TOTAL_TOO_LARGE:
        text = (
            f"归档声明解压后总大小 {violation.total_uncompressed} 字节超过上限 512 MiB"
        )
    else:
        text = "归档整体压缩比超过 100:1"
    return AssignmentImportError(ImportErrorCode.MALFORMED_XLSX, text)


def _parse_csv(
    text: str,
) -> tuple[list[list[str]] | None, AssignmentImportError | None]:
    """Sniff the dialect and read the file.

    Returns ``(rows, None)`` on success (rows exclude the header) or
    ``(None, error)`` for a file-level rejection. Sniffing is primary
    (it catches ``;``/tab files), but a ragged file defeats the
    sniffer's per-line consistency check — exactly the file that
    needs per-row column errors — so a sniff failure falls back to
    any candidate delimiter whose header line reads as
    ``platform,keyword``; only a file readable under no candidate is
    rejected as undialectable. The XLSX parser (spec §7.1 CSV/XLSX)
    plugs in as a sibling returning the same shape.
    """
    sample = text[:_SNIFF_SAMPLE_BYTES]
    if not sample.strip():
        return None, AssignmentImportError(
            ImportErrorCode.MALFORMED_CSV, _EMPTY_FILE_MESSAGE
        )
    # csv.reader accepts a Dialect instance or its class (Sniffer
    # returns the latter), so both flow into `dialect`.
    dialect: csv.Dialect | type[csv.Dialect] | None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=_CSV_DELIMITERS)
    except csv.Error:
        dialect = None
    if dialect is None:
        dialect = _header_match_dialect(text)
        if dialect is None:
            return None, AssignmentImportError(
                ImportErrorCode.MALFORMED_CSV, _BAD_DIALECT_MESSAGE
            )
    reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
    try:
        # strict=True turns structural defects (an unterminated
        # quoted field, ...) into csv.Error here; that is a
        # validation outcome, never an exception past this module
        # (engineering §14). Oversize cells cannot raise anymore:
        # field_size_limit was raised above the byte cap (see
        # ensure_field_size_limit), so they parse and the row check
        # reports TEXT_TOO_LONG.
        rows = list(reader)
    except csv.Error:
        return None, AssignmentImportError(
            ImportErrorCode.MALFORMED_CSV, _BAD_STRUCTURE_MESSAGE
        )
    if not rows:
        return None, AssignmentImportError(
            ImportErrorCode.MALFORMED_CSV, _EMPTY_FILE_MESSAGE
        )
    header = tuple(cell.strip().lower() for cell in rows[0])
    if header != _HEADER:
        return None, AssignmentImportError(
            ImportErrorCode.MALFORMED_CSV, _BAD_HEADER_MESSAGE
        )
    return rows[1:], None


# --- DTOs (explicit, never serialized ORM objects) ---------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentImportError:
    """One preview error; ``row_number is None`` marks a file-level error.

    ``details`` carries the limits behind file-level rejections (size,
    rows, limit) so the error is actionable without re-uploading.
    """

    code: ImportErrorCode
    message: str
    row_number: int | None = None
    platform: str | None = None
    keyword: str | None = None
    details: Mapping[str, int] | None = None


@dataclass(frozen=True, slots=True)
class AssignmentPreviewRow:
    """One canonicalized importable row, with its source row number."""

    row_number: int
    platform: str
    keyword: str


@dataclass(frozen=True, slots=True)
class ParsedImport:
    """Outcome of parsing and validating one upload's bytes.

    Either ``file_error`` names the single file-level rejection (and the
    row collections are empty), or every data row was classified into
    ``valid`` / ``errors``. ``total_rows`` counts data rows (blanks
    skipped), valid and erroneous alike.
    """

    total_rows: int = 0
    valid: list[AssignmentPreviewRow] = field(default_factory=list)
    errors: list[AssignmentImportError] = field(default_factory=list)
    file_error: AssignmentImportError | None = None

    @classmethod
    def file_rejection(cls, error: AssignmentImportError) -> ParsedImport:
        return cls(total_rows=0, valid=[], errors=[], file_error=error)


def parse_upload(
    data: bytes,
    *,
    max_file_bytes: int,
    max_rows: int,
    keyword_max_length: int,
) -> ParsedImport:
    """Byte-cap, decode, parse, and classify every data row (spec §7.1
    steps 2-3). File-level failures are validation outcomes — a
    ``ParsedImport`` carrying the single file-level error — never
    exceptions (backend-engineering §14).

    CSV and XLSX are both accepted (spec §7.1 Teacher 可 CSV/XLSX 导入):
    the ZIP local-header magic routes xlsx bytes to the openpyxl reader,
    everything else to the strict CSV reader. Both produce the same
    canonicalized ``(platform, keyword)`` stream, so every downstream
    validation, dedup, preview, and confirm path is shared.

    Security round F3: the XLSX leg runs the shared ZIP preflight
    (``app.integrations.zip_safety`` — declared sizes, entry count,
    suspicious names, ratios) BEFORE anything is decompressed, and the
    row cap fires DURING sheet enumeration instead of after the whole
    sheet has materialized.
    """
    if len(data) > max_file_bytes:
        return ParsedImport.file_rejection(
            AssignmentImportError(
                ImportErrorCode.FILE_TOO_LARGE,
                _FILE_TOO_LARGE_MESSAGE,
                details={
                    "size": len(data),
                    "limit": max_file_bytes,
                },
            )
        )

    if data[:4] == _XLSX_LOCAL_MAGIC:
        rows, file_error = _parse_xlsx(data, max_rows=max_rows)
        if file_error is not None:
            return ParsedImport.file_rejection(file_error)
        assert rows is not None
        return _classify_rows(
            rows,
            max_rows=max_rows,
            keyword_max_length=keyword_max_length,
        )

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParsedImport.file_rejection(
            AssignmentImportError(
                ImportErrorCode.INVALID_ENCODING, _INVALID_ENCODING_MESSAGE
            )
        )

    rows, file_error = _parse_csv(text)
    if file_error is not None:
        return ParsedImport.file_rejection(file_error)
    assert rows is not None  # _parse_csv returns (rows, None) or (None, error)

    return _classify_rows(
        rows,
        max_rows=max_rows,
        keyword_max_length=keyword_max_length,
    )


def _classify_rows(
    rows: list[list[str]],
    *,
    max_rows: int,
    keyword_max_length: int,
) -> ParsedImport:
    """Shared row validation over canonicalized cells (both readers)."""
    valid: list[AssignmentPreviewRow] = []
    errors: list[AssignmentImportError] = []
    seen: set[tuple[str, str]] = set()
    row_number = 0
    for raw in rows:
        if not any(cell.strip() for cell in raw):
            continue  # blank line: not a spreadsheet row
        row_number += 1
        if row_number > max_rows:
            return ParsedImport.file_rejection(
                AssignmentImportError(
                    ImportErrorCode.LIMIT_EXCEEDED,
                    _LIMIT_EXCEEDED_MESSAGE,
                    details={"rows": row_number, "limit": max_rows},
                )
            )
        if len(raw) != 2:
            errors.append(
                AssignmentImportError(
                    ImportErrorCode.MALFORMED_CSV,
                    _ROW_COLUMNS_MESSAGE,
                    row_number=row_number,
                )
            )
            continue
        platform_raw, keyword_raw = raw
        platform_input = platform_raw.strip()
        keyword = keyword_raw.strip()
        if not platform_input:
            errors.append(
                AssignmentImportError(
                    ImportErrorCode.EMPTY_PLATFORM,
                    _EMPTY_PLATFORM_MESSAGE,
                    row_number=row_number,
                )
            )
            continue
        platform = platform_input.lower()
        if platform not in SUPPORTED_IMPORT_PLATFORMS:
            errors.append(
                AssignmentImportError(
                    ImportErrorCode.UNSUPPORTED_PLATFORM,
                    _UNSUPPORTED_PLATFORM_MESSAGE,
                    row_number=row_number,
                    # Echo for display, truncated: the raw value is
                    # unbounded (a giant cell parses — see _parse_csv
                    # — and fails this check), and §14 keeps preview
                    # echoes small.
                    platform=platform_raw[:_ECHO_MAX_LENGTH],
                )
            )
            continue
        if not keyword:
            errors.append(
                AssignmentImportError(
                    ImportErrorCode.EMPTY_KEYWORD,
                    _EMPTY_KEYWORD_MESSAGE,
                    row_number=row_number,
                )
            )
            continue
        if len(keyword) > keyword_max_length:
            # The oversize keyword is deliberately not echoed (§14
            # safe preview truncation); the message carries the cap.
            message = f"{_TEXT_TOO_LONG_MESSAGE}（不超过 {keyword_max_length} 字符）"
            errors.append(
                AssignmentImportError(
                    ImportErrorCode.TEXT_TOO_LONG,
                    message,
                    row_number=row_number,
                )
            )
            continue
        pair = (platform, keyword)
        if pair in seen:
            errors.append(
                AssignmentImportError(
                    ImportErrorCode.DUPLICATE_IN_FILE,
                    _DUPLICATE_IN_FILE_MESSAGE,
                    row_number=row_number,
                    platform=platform,
                    keyword=keyword,
                )
            )
            continue
        seen.add(pair)
        valid.append(
            AssignmentPreviewRow(
                row_number=row_number, platform=platform, keyword=keyword
            )
        )

    if not valid and not errors:
        return ParsedImport.file_rejection(
            AssignmentImportError(ImportErrorCode.MALFORMED_CSV, _EMPTY_FILE_MESSAGE)
        )

    return ParsedImport(
        total_rows=row_number, valid=valid, errors=errors, file_error=None
    )
