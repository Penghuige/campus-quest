# backend/app/modules/submissions/validators/common.py
"""Shared infrastructure of the submission format validators (spec
§12.4; backend-engineering §14).

Everything the CSV / XLSX / SQLite validators (plan 04 tasks 4-6) have
in common lives here: the resource-limit bundle, the stable code
registry, the frozen §12.4 report with its bounded builder, the
per-cell type checks for the five DSL column types, and — since plan
04 task 5 — the format-agnostic row-scan pieces (header analysis,
row classification, end-of-scan verdicts) every tabular format
validator drives. Pure code: no database, no clock, no I/O.

Value-classification contract (shared verbatim by every format
validator; cells are classified on their edge-trimmed form —
``str.strip`` removes Unicode edge whitespace — and a whitespace-only
cell is a NULL, not a value):

- string:  any non-null trimmed value.
- integer: ``[+-]?[0-9]+`` — ASCII digits only. No decimal point, no
  exponent, no thousands separators, no underscores, no full-width
  digits (``int()`` accepts several of those; a validation report
  must not).
- number:  ``[+-]?(digits[.digits?]? | .digits)([eE][+-]?digits)?`` —
  decimal or scientific notation. ``inf`` / ``nan`` / hex are rejected
  (a bare ``float()`` would accept all three).
- boolean: case-insensitive member of {true, false, 1, 0, yes, no} —
  the closed, unambiguous CSV boolean vocabulary.
- datetime: Python 3.12 ``datetime.fromisoformat`` grammar — the
  documented tolerant set: ISO-8601 dates/timestamps with ``T`` or a
  space separator, optional time part (a bare date reads as
  midnight), optional fractional seconds, ``Z`` or ``±HH:MM``
  offsets, and the compact basic format (``20260102``,
  ``2026-01-02T03:04``). Slash-separated dates are NOT accepted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import ColumnRule, ColumnType, SubmissionSchema

__all__ = [
    "MAX_ERROR_FINDINGS",
    "MAX_WARNING_FINDINGS",
    "UNIQUE_TRACKING_CAP",
    "ScanAggregates",
    "ValidationCode",
    "ValidationFinding",
    "ValidationLimits",
    "ValidationReport",
    "ValidationReportBuilder",
    "analyze_header",
    "check_cell_type",
    "check_row",
    "finalize_scan",
    "is_valid_datetime",
    "truncate_for_report",
]

#: Sample caps for the report's error / warning lists (§12.4 bounded
#: samples). The count fields (type_error_counts, duplicate_counts,
#: ...) stay exact; only the per-finding sample lists are truncated.
MAX_ERROR_FINDINGS = 100
MAX_WARNING_FINDINGS = 100

#: Per-column cap on the unique-value set (bounded memory). Beyond the
#: cap the set is frozen — membership checks keep catching repeats of
#: already-seen values, but repeats among unseen values are missed, so
#: duplicate_counts degrades to a lower bound and a warning says so.
UNIQUE_TRACKING_CAP = 100_000

#: §14 safe preview truncation: how much of an offending cell value is
#: echoed into a finding.
_ECHO_MAX_LENGTH = 64

_INTEGER_PATTERN = re.compile(r"[+-]?[0-9]+")
_NUMBER_PATTERN = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_BOOLEAN_LITERALS = frozenset({"true", "false", "1", "0", "yes", "no"})


class ValidationCode(StrEnum):
    """Stable code for every validation finding; ``value == member name``.

    The registry is shared across the format validators so the §12.4
    report (and the Student-facing rendering on top of it) never has
    to translate per-format vocabularies. ``*_TRUNCATED`` /
    ``UNIQUE_TRACKING_DEGRADED`` / ``EXTERNAL_LINK`` /
    ``FORMULA_WITHOUT_CACHED_VALUE`` travel as warnings; everything
    else is an error.
    """

    EMPTY_FILE = "EMPTY_FILE"
    INVALID_ENCODING = "INVALID_ENCODING"
    BINARY_CONTENT = "BINARY_CONTENT"
    MALFORMED_CSV = "MALFORMED_CSV"
    MALFORMED_XLSX = "MALFORMED_XLSX"
    ARCHIVE_TOO_LARGE = "ARCHIVE_TOO_LARGE"
    SUSPICIOUS_COMPRESSION_RATIO = "SUSPICIOUS_COMPRESSION_RATIO"
    SUSPICIOUS_ARCHIVE_ENTRY = "SUSPICIOUS_ARCHIVE_ENTRY"
    SHEET_NOT_FOUND = "SHEET_NOT_FOUND"
    EXTERNAL_LINK = "EXTERNAL_LINK"
    FORMULA_WITHOUT_CACHED_VALUE = "FORMULA_WITHOUT_CACHED_VALUE"
    TOO_MANY_COLUMNS = "TOO_MANY_COLUMNS"
    DUPLICATE_HEADER = "DUPLICATE_HEADER"
    MISSING_REQUIRED_COLUMN = "MISSING_REQUIRED_COLUMN"
    EXTRA_COLUMN = "EXTRA_COLUMN"
    NO_DATA_ROWS = "NO_DATA_ROWS"
    ROW_SHAPE_MISMATCH = "ROW_SHAPE_MISMATCH"
    CELL_TOO_LONG = "CELL_TOO_LONG"
    NULL_VIOLATION = "NULL_VIOLATION"
    TYPE_ERROR = "TYPE_ERROR"
    DUPLICATE_VALUE = "DUPLICATE_VALUE"
    NULL_RATIO_EXCEEDED = "NULL_RATIO_EXCEEDED"
    MIN_ROWS_NOT_MET = "MIN_ROWS_NOT_MET"
    MAX_ROWS_EXCEEDED = "MAX_ROWS_EXCEEDED"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    UNIQUE_TRACKING_DEGRADED = "UNIQUE_TRACKING_DEGRADED"
    ROW_COUNT_TRUNCATED = "ROW_COUNT_TRUNCATED"
    FINDINGS_TRUNCATED = "FINDINGS_TRUNCATED"


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    """The resource bounds a validator run operates under (§14).

    Defaults are deployment choices, documented here:

    - ``max_columns`` 1024: far above any sane dataset, far below the
      pathological (a wider header aborts before any row work).
    - ``max_cell_length`` 65_536 characters on the RAW cell.
    - ``row_cap`` the schema DSL's absolute ceiling (10_000_000): the
      validator never processes more rows than the DSL can even ask
      for, even when the schema sets no ``max_rows``.
    - ``timeout_seconds`` 30.0 of cooperative clock budget.

    A tighter Task-specific budget is assembled by the validation
    service, not by the parser.
    """

    max_columns: int = 1024
    max_cell_length: int = 65_536
    row_cap: int = 10_000_000
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("max_columns", "max_cell_length", "row_cap"):
            value = getattr(self, name)
            # bool is an int subclass — a literal true/false is a
            # configuration error, not a bound.
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        timeout = self.timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not timeout > 0
        ):
            raise ValueError("timeout_seconds 必须是正数")


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """One bounded sample of a validation outcome.

    ``row`` is the 1-based DATA row (None for file- or column-level
    findings — the header is not a data row); ``value`` is the
    offending cell echoed through ``truncate_for_report`` (§14 safe
    preview truncation; oversized cells are not echoed at all).
    """

    code: ValidationCode
    message: str
    row: int | None = None
    column: str | None = None
    value: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The structured §12.4 machine-validation report.

    Exactly the spec's field list; the mappings/tuples are immutable.
    ``passed`` is the machine gate the worker records (VALIDATED vs
    VALIDATION_FAILED): no error finding, warnings allowed.
    """

    parser_version: str
    file_type: FileType
    row_count: int
    detected_columns: tuple[str, ...]
    missing_required_columns: tuple[str, ...]
    extra_columns: tuple[str, ...]
    type_error_counts: Mapping[str, int]
    null_ratios: Mapping[str, float]
    duplicate_counts: Mapping[str, int]
    warnings: tuple[ValidationFinding, ...]
    errors: tuple[ValidationFinding, ...]
    duration_ms: float

    @property
    def passed(self) -> bool:
        return not self.errors


def truncate_for_report(value: str | None, limit: int = _ECHO_MAX_LENGTH) -> str | None:
    """Bound an echoed cell value for a finding (§14 safe truncation)."""
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


class ValidationReportBuilder:
    """Mutable accumulator behind the frozen ``ValidationReport``.

    Owns exactly the bounded pieces — the error/warning sample lists —
    so every format validator gets identical truncation behavior for
    free: the first ``MAX_*_FINDINGS`` findings are kept, the overflow
    is counted into a truncation flag, and ``build`` appends the
    FINDINGS_TRUNCATED note. Count fields are NOT the builder's to
    bound; each validator keeps them exact and passes them in.
    """

    def __init__(self, *, parser_version: str, file_type: FileType) -> None:
        self._parser_version = parser_version
        self._file_type = file_type
        self._errors: list[ValidationFinding] = []
        self._warnings: list[ValidationFinding] = []
        self._errors_truncated = False
        self._warnings_truncated = False

    def add_error(
        self,
        code: ValidationCode,
        message: str,
        *,
        row: int | None = None,
        column: str | None = None,
        value: str | None = None,
    ) -> None:
        if len(self._errors) >= MAX_ERROR_FINDINGS:
            self._errors_truncated = True
            return
        self._errors.append(
            ValidationFinding(
                code, message, row=row, column=column, value=truncate_for_report(value)
            )
        )

    def add_warning(
        self,
        code: ValidationCode,
        message: str,
        *,
        row: int | None = None,
        column: str | None = None,
        value: str | None = None,
    ) -> None:
        if len(self._warnings) >= MAX_WARNING_FINDINGS:
            self._warnings_truncated = True
            return
        self._warnings.append(
            ValidationFinding(
                code, message, row=row, column=column, value=truncate_for_report(value)
            )
        )

    def build(
        self,
        *,
        row_count: int,
        detected_columns: Sequence[str],
        missing_required_columns: Sequence[str],
        extra_columns: Sequence[str],
        type_error_counts: Mapping[str, int],
        null_ratios: Mapping[str, float],
        duplicate_counts: Mapping[str, int],
        duration_ms: float,
    ) -> ValidationReport:
        warnings = list(self._warnings)
        if self._errors_truncated:
            warnings.append(
                ValidationFinding(
                    ValidationCode.FINDINGS_TRUNCATED,
                    f"错误样本已截断至 {MAX_ERROR_FINDINGS} 条，计数字段仍为精确值",
                )
            )
        if self._warnings_truncated:
            warnings.append(
                ValidationFinding(
                    ValidationCode.FINDINGS_TRUNCATED,
                    f"警告样本已截断至 {MAX_WARNING_FINDINGS} 条",
                )
            )
        return ValidationReport(
            parser_version=self._parser_version,
            file_type=self._file_type,
            row_count=row_count,
            detected_columns=tuple(detected_columns),
            missing_required_columns=tuple(missing_required_columns),
            extra_columns=tuple(extra_columns),
            type_error_counts=MappingProxyType(dict(type_error_counts)),
            null_ratios=MappingProxyType(dict(null_ratios)),
            duplicate_counts=MappingProxyType(dict(duplicate_counts)),
            warnings=tuple(warnings),
            errors=tuple(self._errors),
            duration_ms=duration_ms,
        )


# --- per-cell type checks (the contract in the module docstring) ------------


def is_valid_datetime(value: str) -> bool:
    """ISO-8601 plus the documented tolerant set (see module docstring)."""
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_cell_type(value: str, column_type: ColumnType) -> bool:
    """Does the trimmed cell value satisfy the column's DSL type?"""
    match column_type:
        case ColumnType.STRING:
            return True
        case ColumnType.INTEGER:
            return _INTEGER_PATTERN.fullmatch(value) is not None
        case ColumnType.NUMBER:
            return _NUMBER_PATTERN.fullmatch(value) is not None
        case ColumnType.BOOLEAN:
            return value.lower() in _BOOLEAN_LITERALS
        case ColumnType.DATETIME:
            return is_valid_datetime(value)


# --- the format-agnostic row scan (shared by CSV / XLSX) --------------------
#
# A format validator supplies a header as ``list[str]`` (edge-trimmed,
# trailing padding removed) and data rows as ``list[str]`` (short rows
# padded with empty strings — a missing cell is a NULL — long rows
# left long so the shape check fires); these helpers do the rest.
# Pure code: no clock, no I/O; the owning validator drives iteration
# and the cooperative timeout around them.


@dataclass(frozen=True, slots=True)
class ScanAggregates:
    """The countable §12.4 fields a scan accumulated (empty on early
    file-level rejections)."""

    row_count: int = 0
    detected_columns: tuple[str, ...] = ()
    missing_required_columns: tuple[str, ...] = ()
    extra_columns: tuple[str, ...] = ()
    type_error_counts: Mapping[str, int] = field(default_factory=dict)
    null_ratios: Mapping[str, float] = field(default_factory=dict)
    duplicate_counts: Mapping[str, int] = field(default_factory=dict)


def analyze_header(
    header_cells: list[str],
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
) -> tuple[list[tuple[int, ColumnRule]], list[str], list[str]] | None:
    """Classify the header; ``None`` means the column cap aborted.

    Returns the positional validation plan (header position -> rule;
    extras are never type-checked), the missing required names, and
    the extra names — errors are already recorded here.
    """
    if len(header_cells) > limits.max_columns:
        builder.add_error(
            ValidationCode.TOO_MANY_COLUMNS,
            f"表头列数 {len(header_cells)} 超过上限 {limits.max_columns}",
        )
        return None
    seen: set[str] = set()
    for name in header_cells:
        if name in seen:
            builder.add_error(
                ValidationCode.DUPLICATE_HEADER,
                f"表头列名重复: {name!r}",
                column=name,
            )
        seen.add(name)
    missing = [rule.name for rule in schema.required_columns if rule.name not in seen]
    for name in missing:
        builder.add_error(
            ValidationCode.MISSING_REQUIRED_COLUMN,
            f"缺少必填列: {name}",
            column=name,
        )
    extra = [name for name in dict.fromkeys(header_cells) if name not in schema.columns]
    if not schema.allow_extra_columns:
        for name in extra:
            builder.add_error(
                ValidationCode.EXTRA_COLUMN,
                f"未在 schema 中声明的列: {name}",
                column=name,
            )
    plan = [
        (index, rule)
        for index, name in enumerate(header_cells)
        if (rule := schema.columns.get(name)) is not None
    ]
    return plan, missing, extra


def check_row(
    raw: list[str],
    row_number: int,
    header_cells: list[str],
    plan: list[tuple[int, ColumnRule]],
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    null_counts: dict[str, int],
    type_errors: dict[str, int],
    duplicate_counts: dict[str, int],
    unique_seen: dict[str, set[str]],
    degraded_columns: set[str],
) -> None:
    """Classify one data row; every outcome is a bounded finding."""
    if len(raw) != len(header_cells):
        builder.add_error(
            ValidationCode.ROW_SHAPE_MISMATCH,
            f"第 {row_number} 行列数 {len(raw)} 与表头列数 {len(header_cells)} 不一致",
            row=row_number,
        )
    for index, rule in plan:
        if index >= len(raw):
            break  # short row: the shape error already covers it
        cell = raw[index]
        if len(cell) > limits.max_cell_length:
            builder.add_error(
                ValidationCode.CELL_TOO_LONG,
                f"第 {row_number} 行列 {rule.name} 的单元格超过最大长度"
                f" {limits.max_cell_length}",
                row=row_number,
                column=rule.name,
            )
            continue
        value = cell.strip()
        if not value:
            null_counts[rule.name] = null_counts.get(rule.name, 0) + 1
            if not rule.nullable:
                builder.add_error(
                    ValidationCode.NULL_VIOLATION,
                    f"第 {row_number} 行列 {rule.name} 不允许为空",
                    row=row_number,
                    column=rule.name,
                )
            continue
        if not check_cell_type(value, rule.type):
            type_errors[rule.name] = type_errors.get(rule.name, 0) + 1
            builder.add_error(
                ValidationCode.TYPE_ERROR,
                f"第 {row_number} 行列 {rule.name} 的值不符合 {rule.type.value} 类型",
                row=row_number,
                column=rule.name,
                value=value,
            )
        if rule.unique:
            seen_values = unique_seen.setdefault(rule.name, set())
            if value in seen_values:
                duplicate_counts[rule.name] = duplicate_counts.get(rule.name, 0) + 1
                builder.add_error(
                    ValidationCode.DUPLICATE_VALUE,
                    f"第 {row_number} 行列 {rule.name} 的值重复",
                    row=row_number,
                    column=rule.name,
                    value=value,
                )
            elif rule.name not in degraded_columns:
                if len(seen_values) >= UNIQUE_TRACKING_CAP:
                    degraded_columns.add(rule.name)
                    builder.add_warning(
                        ValidationCode.UNIQUE_TRACKING_DEGRADED,
                        f"列 {rule.name} 的唯一值超过 {UNIQUE_TRACKING_CAP} 个，"
                        "重复检测退化为已知值匹配，duplicate_counts 为下界",
                        column=rule.name,
                    )
                else:
                    seen_values.add(value)


def finalize_scan(
    *,
    header_cells: list[str] | None,
    plan: list[tuple[int, ColumnRule]],
    missing: list[str],
    extra: list[str],
    row_count: int,
    scan_incomplete: bool,
    null_counts: dict[str, int],
    type_errors: dict[str, int],
    duplicate_counts: dict[str, int],
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    no_data_message: str,
) -> ScanAggregates:
    """End-of-scan checks (min/max rows, null ratios) and the counts.

    ANY incomplete scan (row-cap stop, timeout, mid-file abort) makes
    ``row_count`` a lower bound, so every verdict that would need the
    full count is skipped: presence, ``min_rows``, and — the T4
    carry fix — ``NULL_RATIO_EXCEEDED`` and any other ratio-derived
    conclusion. A null ratio computed from truncated counts proves
    nothing about the whole file (the observed prefix may be 99.98%
    null while the file is not). Counts already past ``max_rows`` /
    the row cap stay conclusive either way. The ``null_ratios`` map is
    still reported (informational, with the truncation warning).
    """
    detected = tuple(header_cells) if header_cells is not None else ()
    column_names = list(dict.fromkeys(rule.name for _, rule in plan))
    null_ratios = {
        name: round(null_counts.get(name, 0) / row_count, 6) if row_count else 0.0
        for name in column_names
    }
    if not scan_incomplete:
        for rule in schema.columns.values():
            if rule.max_null_ratio is None or rule.name not in null_ratios:
                continue
            if not row_count:
                continue
            if null_counts.get(rule.name, 0) / row_count > rule.max_null_ratio:
                builder.add_error(
                    ValidationCode.NULL_RATIO_EXCEEDED,
                    f"列 {rule.name} 的空值占比超过上限 {rule.max_null_ratio}",
                    column=rule.name,
                )
    if scan_incomplete:
        builder.add_warning(
            ValidationCode.ROW_COUNT_TRUNCATED,
            "行数统计提前停止，row_count 为下界",
        )
        if row_count > limits.row_cap:
            builder.add_error(
                ValidationCode.ROW_LIMIT_EXCEEDED,
                f"数据行数超过校验行数上限 {limits.row_cap}，已提前停止",
            )
    # An incomplete scan (row cap, timeout, structural/encoding abort)
    # proves nothing about how many rows follow: the presence and
    # min-row verdicts that need a completed scan are skipped
    # (max_rows stays — a count already past the bound is conclusive
    # either way).
    if not scan_incomplete:
        if row_count == 0 and header_cells is not None:
            builder.add_error(
                ValidationCode.NO_DATA_ROWS,
                no_data_message,
            )
        if schema.min_rows is not None and row_count < schema.min_rows:
            builder.add_error(
                ValidationCode.MIN_ROWS_NOT_MET,
                f"数据行数 {row_count} 少于 min_rows {schema.min_rows}",
            )
    if schema.max_rows is not None and row_count > schema.max_rows:
        builder.add_error(
            ValidationCode.MAX_ROWS_EXCEEDED,
            f"数据行数 {row_count} 超过 max_rows {schema.max_rows}",
        )
    return ScanAggregates(
        row_count=row_count,
        detected_columns=detected,
        missing_required_columns=tuple(missing),
        extra_columns=tuple(extra),
        type_error_counts=MappingProxyType(
            {name: type_errors.get(name, 0) for name in column_names}
        ),
        null_ratios=MappingProxyType(null_ratios),
        duplicate_counts=MappingProxyType(
            {
                rule.name: duplicate_counts.get(rule.name, 0)
                for rule in schema.columns.values()
                if rule.unique and rule.name in null_ratios
            }
        ),
    )
