# backend/app/modules/submissions/validators/common.py
"""Shared infrastructure of the submission format validators (spec
§12.4; backend-engineering §14).

Everything the CSV / XLSX / SQLite validators (plan 04 tasks 4-6) have
in common lives here: the resource-limit bundle, the stable code
registry, the frozen §12.4 report with its bounded builder, and the
per-cell type checks for the five DSL column types. Pure code: no
database, no clock, no I/O.

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
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import ColumnType

__all__ = [
    "MAX_ERROR_FINDINGS",
    "MAX_WARNING_FINDINGS",
    "UNIQUE_TRACKING_CAP",
    "ValidationCode",
    "ValidationFinding",
    "ValidationLimits",
    "ValidationReport",
    "ValidationReportBuilder",
    "check_cell_type",
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
    ``UNIQUE_TRACKING_DEGRADED`` travel as warnings; everything else
    is an error.
    """

    EMPTY_FILE = "EMPTY_FILE"
    INVALID_ENCODING = "INVALID_ENCODING"
    BINARY_CONTENT = "BINARY_CONTENT"
    MALFORMED_CSV = "MALFORMED_CSV"
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
