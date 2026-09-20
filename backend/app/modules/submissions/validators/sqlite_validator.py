# backend/app/modules/submissions/validators/sqlite_validator.py
"""Read-only SQLite submission validator (spec §12.3, §12.4;
backend-engineering §14).

``validate_sqlite(path, schema, limits)`` turns an untrusted SQLite
file into a structured ``ValidationReport`` — a validation outcome,
never an escaping exception. Pure unit work: no database, no services,
no wall clock (the monotonic clock is injectable). The uploaded file is
opened on a dedicated throwaway ``sqlite3`` connection only — it is
never attached to, mounted as, or compared against the application
database (spec §12.3's last bullet).

Design decisions (each pinned by a test):

- **Real SQLite 3 files only.** The first 16 bytes must equal the
  SQLite magic ``"SQLite format 3\\0"`` — checked BEFORE any SQLite
  open, so a fake ``.sqlite`` never reaches the library. A missing path
  raises ``OSError`` from this pre-read (infrastructure the worker
  retries, not a property of the file — same policy as CSV/XLSX). An
  empty file is ``EMPTY_FILE``; a wrong magic is ``MALFORMED_SQLITE``.
- **Read-only, immutable open (spec §12.3).** The connection URI is
  ``file:<quoted>?mode=ro&immutable=1``. ``mode=ro`` is the hard
  read-only flag; ``immutable=1`` (documented choice) additionally
  tells SQLite the file will never change, so it skips ALL locking and
  the WAL/-shm machinery: the validator needs no write access to the
  storage directory and never creates sidecar files beside the upload
  (a plain ``mode=ro`` open of a WAL-mode file would create
  ``-wal``/``-shm``), and a WAL-mode upload validates its checkpointed
  main-file state. The assumption is exactly what the upload pipeline
  guarantees: the artifact is frozen the moment it is finalized.
  ``PRAGMA query_only=ON`` and a ``busy_timeout`` (the timeout budget,
  in ms) are set on the connection, and extension loading is
  re-disabled explicitly (``enable_load_extension(False)``; Python's
  sqlite3 ships with loading disabled — the call re-asserts it, and a
  build without extension support raises ``AttributeError``, which is
  swallowed: loading is then impossible anyway).
- **Authorizer default-deny.** A ``set_authorizer`` callback denies
  EVERY action except exactly what this validator generates:

  ============ ========================= =====================================
  action       argument                  verdict
  ============ ========================= =====================================
  SQLITE_SELECT  (any)                   OK — server-generated SELECTs
  SQLITE_READ   table in the allowlist    OK (``sqlite_master`` while
                                          introspecting; the chosen table
                                          once selected)
  SQLITE_PRAGMA ``table_info``           OK — the one introspection pragma
  everything    else (ATTACH, DETACH,    DENY — includes every other
                 INSERT/UPDATE/DELETE,    pragma (reads AND writes), all
                 CREATE/DROP/ALTER,       DDL/DML, transactions, function
                 TRANSACTION, SAVEPOINT,  invocations, recursion
                 SQLITE_FUNCTION, ...)
  ============ ========================= =====================================

  The callback is installed AFTER the connection's own setup pragmas
  (``busy_timeout``, ``query_only``), so the pragma allowlist stays at
  one entry. User SQL is never executed: the only statements this
  module ever issues are the fixed ``sqlite_master`` listing, a
  ``PRAGMA table_info`` with a quoted identifier, and a SELECT of
  explicitly double-quoted column names from the chosen table — no
  string from the file is ever interpolated into SQL unquoted, and
  identifiers are sanity-checked (non-empty, no NUL, bounded length)
  before quoting as defense in depth.
- **Table selection (spec §12.3):** ``source_selector.table_name``
  picks that table (exact, case-SENSITIVE match — the schema.py
  convention; a schema carrying only ``sheet_name`` reads as no
  selector, since each validator reads only its own field). A missing
  selection fails ``TABLE_NOT_FOUND`` naming the available tables. No
  selector: exactly one user table -> chosen; none ->
  ``TABLE_NOT_FOUND``; several -> ``AMBIGUOUS_TABLE``. User tables are
  ``sqlite_master`` rows with ``type='table'`` and a name NOT LIKE
  ``'sqlite_%'`` — ``sqlite_sequence`` & friends never count.
- **Columns via ``PRAGMA table_info``**, names matched against the
  schema by the shared ``analyze_header`` (case-sensitive exact match
  after edge-whitespace trim; ``TOO_MANY_COLUMNS``; missing/extra/
  duplicate reported). Data is then read with a server-generated
  ``SELECT`` of the table's own columns — a full scan, streaming row
  by row through cursor iteration (no LIMIT/OFFSET re-querying), under
  the row cap and the cooperative timeout.
- **Value-level typing (SQLite is dynamically typed).** Classification
  follows the VALUE's storage class, not the column's declared
  affinity, mapped onto the five DSL types:

  ============== ========= ============================================
  storage class  Python    rule
  ============== ========= ============================================
  NULL           None      the null path (never a type error)
  TEXT           str       the shared text contract from common.py,
                          on the edge-trimmed value (whitespace-only
                          TEXT is a NULL, as in CSV/XLSX)
  INTEGER        int       integer/number: any; boolean: only 0/1
                          (SQLite's convention); string: any; datetime:
                          never (no epoch unit convention in the DSL)
  REAL           float     number: finite; integer: finite and integral
                          (10.0 IS the integer 10); string: any;
                          boolean/datetime: never
  BLOB           bytes     never valid for any DSL type
  ============== ========= ============================================

  The row scan is therefore a SQLite-specific loop (the shared
  ``check_row`` is text-shaped: rendering values to strings would
  erase exactly the distinctions above — 10.0 vs "10.0", BLOB vs
  TEXT, 0/1 booleans). The format-agnostic pieces are still shared:
  ``analyze_header`` for the column contract and ``finalize_scan``
  for the end-of-scan verdicts (min/max rows, null ratios,
  duplicate counts, the ``scan_incomplete`` contract).
- **Uniqueness mirrors SQLite index comparison:** the tracked key is
  ``("num", Decimal(value))`` for INTEGER/REAL (so 1 and 1.0 collide),
  ``("text", trimmed)`` for TEXT (so numerics and text stay distinct,
  as they are in a SQLite index), ``("blob", bytes)`` for BLOB; NULLs
  are never tracked. Bounded by the shared ``UNIQUE_TRACKING_CAP``
  with the same degraded-mode warning.
- **Bounds (§14):** the hard row stop is ``min(limits.row_cap,
  schema.max_rows + 1)`` — one row past ``max_rows`` proves the
  verdict; TEXT length is measured in characters, BLOB in bytes, both
  against ``max_cell_length`` (checked before the type check, like
  the shared row scan). The total time budget is cooperative twice
  over: a SQLite progress handler checks the injected clock every
  2000 VM instructions (aborting the statement with an "interrupted"
  ``OperationalError``), and the fetch loop re-checks every 1024 rows
  (the shared pattern). Either trip records ``TIMEOUT`` and marks the
  scan incomplete.
- **Failure surface:** every ``sqlite3.Error`` becomes a validation
  outcome — ``TIMEOUT`` when the budget tripped, ``INVALID_ENCODING``
  for TEXT that does not decode as UTF-8 (SQLite surfaces this as
  "Could not decode to UTF-8" from the fetch), ``MALFORMED_SQLITE``
  for everything else (corrupt pages, truncated file, not-a-database)
  — with mid-scan failures keeping the counts so far and the
  ``ROW_COUNT_TRUNCATED`` warning, mirroring the CSV/XLSX contracts.
  ``OSError`` is deliberately NOT converted.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import time
from collections.abc import Callable
from decimal import Decimal
from os import PathLike
from urllib.parse import quote

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import ColumnRule, ColumnType, SubmissionSchema

from .common import (
    UNIQUE_TRACKING_CAP,
    ScanAggregates,
    ValidationCode,
    ValidationLimits,
    ValidationReport,
    ValidationReportBuilder,
    analyze_header,
    check_cell_type,
    finalize_scan,
)

__all__ = ["PARSER_VERSION", "validate_sqlite"]

#: Recorded in the §12.4 report's parser_version; bump on any change
#: to this module's parsing or classification semantics.
PARSER_VERSION = "sqlite-1"

#: The 16-byte magic every real SQLite 3 file starts with.
SQLITE_MAGIC = b"SQLite format 3\x00"

#: The fixed statement listing candidate tables. The sqlite_% prefix
#: filter runs Python-side (see ``_is_system_table``): SQL ``LIKE``
#: would reach the authorizer as a ``SQLITE_FUNCTION`` invocation,
#: and keeping the function deny absolute beats widening it.
_LIST_TABLES_SQL = "SELECT name FROM sqlite_master WHERE type = 'table'"

#: How often the progress handler checks the clock (SQLite VM
#: instructions) and how often the fetch loop does (rows).
_PROGRESS_INSTRUCTIONS = 2000
_TIMEOUT_CHECK_ROWS = 1024

#: Identifier sanity bound (defense in depth before quoting; real
#: names are far shorter and SQLite identifiers cannot hold NUL).
_MAX_IDENTIFIER = 1024

#: How many available table names the failure messages list.
_LISTED_TABLES = 10

_NO_DATA_MESSAGE = "SQLite 表只有列定义，没有数据行"
_NOT_SQLITE_MESSAGE = "文件不是有效的 SQLite 3 数据库（header 校验失败）"
_UNOPENABLE_MESSAGE = "SQLite 数据库无法只读打开（文件损坏或不是有效数据库）"
_CORRUPT_MESSAGE = "SQLite 数据库读取失败（文件损坏或不是有效数据库）"
_ENCODING_MESSAGE = "TEXT 列中存在无法按 UTF-8 解码的内容"

Source = str | PathLike[str]


class _Budget:
    """Cooperative time budget shared by both timeout checkpoints."""

    __slots__ = ("_clock", "_started", "_timeout", "timed_out")

    def __init__(
        self, clock: Callable[[], float], started: float, timeout: float
    ) -> None:
        self._clock = clock
        self._started = started
        self._timeout = timeout
        self.timed_out = False

    def exceeded(self) -> bool:
        """Has the budget been spent? Latching: once tripped, always
        tripped, so the abort and the TIMEOUT verdict cannot diverge."""
        if self.timed_out:
            return True
        if self._clock() - self._started > self._timeout:
            self.timed_out = True
        return self.timed_out


def validate_sqlite(
    path: Source,
    schema: SubmissionSchema,
    limits: ValidationLimits | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ValidationReport:
    """Validate a SQLite file against a parsed submission schema.

    The file is opened read-only/immutable and scanned to (at most)
    the configured row budget; every SQLite-level failure comes back
    inside the report (backend-engineering §14). ``OSError`` from
    storage propagates deliberately.
    """
    effective_limits = ValidationLimits() if limits is None else limits
    started = clock()
    builder = ValidationReportBuilder(
        parser_version=PARSER_VERSION, file_type=FileType.SQLITE
    )
    aggregates = _run(path, schema, effective_limits, builder, clock, started)
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
    path: Source,
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    clock: Callable[[], float],
    started: float,
) -> ScanAggregates:
    """Header gate, then the guarded read-only run."""
    # OSError (missing path, unreadable storage) propagates: the
    # pre-read is what makes a nonexistent file an infrastructure
    # error rather than a sqlite3 "unable to open" misclassification.
    with open(path, "rb") as handle:
        head = handle.read(len(SQLITE_MAGIC))
    if not head:
        builder.add_error(ValidationCode.EMPTY_FILE, "SQLite 文件为空")
        return ScanAggregates()
    if head != SQLITE_MAGIC:
        builder.add_error(ValidationCode.MALFORMED_SQLITE, _NOT_SQLITE_MESSAGE)
        return ScanAggregates()

    budget = _Budget(clock, started, limits.timeout_seconds)
    try:
        connection = _open_read_only(path, limits.timeout_seconds)
    except sqlite3.Error:
        builder.add_error(ValidationCode.MALFORMED_SQLITE, _UNOPENABLE_MESSAGE)
        return ScanAggregates()
    try:
        connection.set_progress_handler(
            lambda: 1 if budget.exceeded() else 0, _PROGRESS_INSTRUCTIONS
        )
        return _inspect_and_scan(connection, schema, limits, builder, budget)
    except sqlite3.Error:
        if budget.timed_out:
            builder.add_error(
                ValidationCode.TIMEOUT,
                f"解析超过时间上限 {limits.timeout_seconds} 秒，已停止",
            )
        else:
            builder.add_error(ValidationCode.MALFORMED_SQLITE, _CORRUPT_MESSAGE)
        return ScanAggregates()
    finally:
        connection.close()


def _open_read_only(path: Source, timeout_seconds: float) -> sqlite3.Connection:
    """The hardened connection: URI mode=ro + immutable, busy timeout,
    query_only, extension loading off, default-deny authorizer.

    The authorizer's allowlist starts at ``sqlite_master`` only; the
    scan phase reinstalls it with the chosen table added.
    """
    uri = "file:" + quote(str(path)) + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    # A build without extension support raises AttributeError here —
    # which means loading extensions is impossible anyway.
    with contextlib.suppress(AttributeError):
        connection.enable_load_extension(False)
    connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
    connection.execute("PRAGMA query_only = ON")
    connection.set_authorizer(_make_authorizer({"sqlite_master"}))
    return connection


def _make_authorizer(
    allowed_tables: set[str],
) -> Callable[[int, str | None, str | None, str | None, str | None], int]:
    """Default-deny authorizer; see the module docstring's policy
    table. Reads are confined to the allowlist; ``table_info`` is the
    only admitted pragma; everything else — ATTACH/DETACH, all DML and
    DDL, transactions, function invocations, recursion — is denied."""

    def authorizer(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and arg1 in allowed_tables:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "table_info":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorizer


def _quote_identifier(name: str) -> str | None:
    """Double-quoted identifier, or ``None`` when the name is not sane
    (empty, NUL, absurdly long) — defense in depth before quoting."""
    if not name or "\x00" in name or len(name) > _MAX_IDENTIFIER:
        return None
    return '"' + name.replace('"', '""') + '"'


def _inspect_and_scan(
    connection: sqlite3.Connection,
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    budget: _Budget,
) -> ScanAggregates:
    """Choose the table, analyze its columns, scan its rows."""
    table = _choose_table(connection, schema, builder)
    if table is None:
        return ScanAggregates()
    quoted_table = _quote_identifier(table)
    if quoted_table is None:
        builder.add_error(ValidationCode.MALFORMED_SQLITE, _CORRUPT_MESSAGE)
        return ScanAggregates()
    # The scan's reads are confined to exactly the chosen table.
    connection.set_authorizer(_make_authorizer({"sqlite_master", table}))

    raw_names = [
        row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")
    ]
    if not raw_names or any(_quote_identifier(name) is None for name in raw_names):
        builder.add_error(ValidationCode.MALFORMED_SQLITE, _CORRUPT_MESSAGE)
        return ScanAggregates()
    header_cells = [name.strip() for name in raw_names]
    analysis = analyze_header(header_cells, schema, limits, builder)
    if analysis is None:
        return ScanAggregates(
            detected_columns=tuple(header_cells[: limits.max_columns])
        )
    plan, missing, extra = analysis
    quoted_columns = ", ".join(_quote_identifier(name) or "''" for name in raw_names)
    return _scan_table(
        connection,
        f"SELECT {quoted_columns} FROM {quoted_table}",
        header_cells,
        plan,
        missing,
        extra,
        schema,
        limits,
        builder,
        budget,
    )


def _is_system_table(name: str) -> bool:
    """``name NOT LIKE 'sqlite_%'`` from spec §12.3, evaluated exactly
    as SQLite would: the pattern is 'sqlite' + one wildcard char +
    anything, ASCII case-insensitive — so ``sqlite_sequence`` and any
    ``sqliteX`` are system tables, a bare ``sqlite`` is not."""
    return len(name) > 6 and name[:6].lower() == "sqlite"


def _choose_table(
    connection: sqlite3.Connection,
    schema: SubmissionSchema,
    builder: ValidationReportBuilder,
) -> str | None:
    """Spec §12.3 table selection; ``None`` means a recorded failure."""
    names = [
        row[0]
        for row in connection.execute(_LIST_TABLES_SQL)
        if isinstance(row[0], str) and not _is_system_table(row[0])
    ]
    selector = (
        schema.source_selector.table_name
        if schema.source_selector is not None
        else None
    )
    if selector is not None:
        if selector not in names:  # exact, case-sensitive
            builder.add_error(
                ValidationCode.TABLE_NOT_FOUND,
                f"找不到名为 {selector!r} 的用户表；可用用户表: {_listed(names)}",
            )
            return None
        return selector
    if not names:
        builder.add_error(
            ValidationCode.TABLE_NOT_FOUND,
            "数据库中没有用户表（sqlite_% 系统表不算用户表）",
        )
        return None
    if len(names) > 1:
        builder.add_error(
            ValidationCode.AMBIGUOUS_TABLE,
            f"数据库中有 {len(names)} 个用户表且 schema 未指定 "
            f"source_selector.table_name；可用用户表: {_listed(names)}",
        )
        return None
    return names[0]


def _listed(names: list[str]) -> str:
    """Up to ten names for a failure message, in sqlite_master order."""
    shown = ", ".join(names[:_LISTED_TABLES])
    if len(names) > _LISTED_TABLES:
        shown += ", …"
    return shown


def _scan_table(
    connection: sqlite3.Connection,
    sql: str,
    header_cells: list[str],
    plan: list[tuple[int, ColumnRule]],
    missing: list[str],
    extra: list[str],
    schema: SubmissionSchema,
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    budget: _Budget,
) -> ScanAggregates:
    """Stream the table's rows; every failure keeps the counts so far."""
    row_count = 0
    scan_incomplete = False
    null_counts: dict[str, int] = {}
    type_errors: dict[str, int] = {}
    duplicate_counts: dict[str, int] = {}
    unique_seen: dict[str, set[object]] = {}
    degraded_columns: set[str] = set()
    hard_cap = (
        limits.row_cap
        if schema.max_rows is None
        else min(limits.row_cap, schema.max_rows + 1)
    )

    try:
        for values in connection.execute(sql):
            row_count += 1
            if row_count > hard_cap:
                scan_incomplete = True
                break
            if row_count % _TIMEOUT_CHECK_ROWS == 0 and budget.exceeded():
                builder.add_error(
                    ValidationCode.TIMEOUT,
                    f"解析超过时间上限 {limits.timeout_seconds} 秒，已停止",
                )
                scan_incomplete = True
                break
            _check_row_values(
                values,
                row_count,
                plan,
                limits,
                builder,
                null_counts,
                type_errors,
                duplicate_counts,
                unique_seen,
                degraded_columns,
            )
    except sqlite3.Error as exc:
        if budget.timed_out:
            builder.add_error(
                ValidationCode.TIMEOUT,
                f"解析超过时间上限 {limits.timeout_seconds} 秒，已停止",
            )
        elif "Could not decode to UTF-8" in str(exc):
            builder.add_error(ValidationCode.INVALID_ENCODING, _ENCODING_MESSAGE)
        else:
            builder.add_error(ValidationCode.MALFORMED_SQLITE, _CORRUPT_MESSAGE)
        scan_incomplete = True

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


def _check_row_values(
    values: tuple[object, ...],
    row_number: int,
    plan: list[tuple[int, ColumnRule]],
    limits: ValidationLimits,
    builder: ValidationReportBuilder,
    null_counts: dict[str, int],
    type_errors: dict[str, int],
    duplicate_counts: dict[str, int],
    unique_seen: dict[str, set[object]],
    degraded_columns: set[str],
) -> None:
    """One row of storage-classed values through the §12.4 findings.

    The SQLite-specific counterpart of common.check_row: same finding
    order (length, null, type, uniqueness) and same bounded samples,
    but classification is value-level (see the module docstring's
    affinity table) — rows are always exactly the table's width, so
    no shape check exists.
    """
    for index, rule in plan:
        value = values[index]
        if isinstance(value, (str, bytes)) and len(value) > limits.max_cell_length:
            builder.add_error(
                ValidationCode.CELL_TOO_LONG,
                f"第 {row_number} 行列 {rule.name} 的单元格超过最大长度"
                f" {limits.max_cell_length}",
                row=row_number,
                column=rule.name,
            )
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            null_counts[rule.name] = null_counts.get(rule.name, 0) + 1
            if not rule.nullable:
                builder.add_error(
                    ValidationCode.NULL_VIOLATION,
                    f"第 {row_number} 行列 {rule.name} 不允许为空",
                    row=row_number,
                    column=rule.name,
                )
            continue
        if not _value_matches(value, rule.type):
            type_errors[rule.name] = type_errors.get(rule.name, 0) + 1
            builder.add_error(
                ValidationCode.TYPE_ERROR,
                f"第 {row_number} 行列 {rule.name} 的值不符合 {rule.type.value} 类型",
                row=row_number,
                column=rule.name,
                value=_display(value),
            )
        if rule.unique:
            key = _unique_key(value)
            seen_values = unique_seen.setdefault(rule.name, set())
            if key in seen_values:
                duplicate_counts[rule.name] = duplicate_counts.get(rule.name, 0) + 1
                builder.add_error(
                    ValidationCode.DUPLICATE_VALUE,
                    f"第 {row_number} 行列 {rule.name} 的值重复",
                    row=row_number,
                    column=rule.name,
                    value=_display(value),
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
                    seen_values.add(key)


def _value_matches(value: object, column_type: ColumnType) -> bool:
    """The value-level affinity mapping (module docstring table)."""
    if isinstance(value, bytes):  # BLOB: never a valid DSL scalar
        return False
    if isinstance(value, int):  # INTEGER storage class (bool never occurs)
        match column_type:
            case ColumnType.STRING | ColumnType.INTEGER | ColumnType.NUMBER:
                return True
            case ColumnType.BOOLEAN:
                return value in (0, 1)
            case ColumnType.DATETIME:
                return False
    if isinstance(value, float):  # REAL storage class
        match column_type:
            case ColumnType.STRING:
                return True
            case ColumnType.NUMBER:
                return math.isfinite(value)
            case ColumnType.INTEGER:
                return math.isfinite(value) and value.is_integer()
            case ColumnType.BOOLEAN | ColumnType.DATETIME:
                return False
    if isinstance(value, str):  # TEXT: the shared text contract
        return check_cell_type(value.strip(), column_type)
    return False  # unknown Python type from sqlite3: fail closed


def _unique_key(value: object) -> object:
    """Hashable key mirroring SQLite index comparison: numerics by
    value (1 == 1.0), TEXT trimmed, BLOB by bytes, classes distinct."""
    if isinstance(value, bool):  # defensive; sqlite3 never returns bool
        return ("num", int(value))
    if isinstance(value, int):
        return ("num", Decimal(value))
    if isinstance(value, float):
        return ("num", Decimal(str(value)))
    if isinstance(value, str):
        return ("text", value.strip())
    return ("blob", value)


def _display(value: object) -> str:
    """A value echoed into a finding (bounded by truncate_for_report)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value.strip()
    return str(value)
