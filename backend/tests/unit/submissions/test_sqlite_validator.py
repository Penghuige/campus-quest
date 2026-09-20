# backend/tests/unit/submissions/test_sqlite_validator.py
"""Unit tests for the SQLite submission validator (spec §12.3, §12.4;
backend-engineering §14: real-file magic gate, read-only/query-only
connection, authorizer default-deny, server-generated SQL only,
bounded rows/cells/time, sqlite3 failures -> validation outcomes).

Fixtures are real SQLite 3 files built programmatically through the
sqlite3 module (plus raw-byte fakes for the header gate) — the same
library the validator reads them with, so storage classes, identifier
quoting, WAL checkpoints, and AUTOINCREMENT system tables behave
exactly as production sees them. Columns are created without declared
types (BLOB affinity: values keep the storage class they were inserted
with), which is what makes the value-level affinity matrix precise.

Coverage matrix:

- the brief's six fixture cases: valid header + one user table; fake
  DB file; multiple user tables without selector; selected table
  missing; schema mismatch; row-limit enforcement
- read-only hardening (spec §12.3): header magic checked BEFORE any
  open; mode=ro + immutable=1 URI open (no sidecar files beside the
  upload, file bytes unchanged, no directory write access needed);
  PRAGMA query_only; authorizer default-deny — ATTACH / DETACH /
  INSERT / CREATE / pragma writes / SQL function use all denied,
  pinned both end-to-end on a live connection and as a direct callback
  matrix; extension loading re-disabled explicitly
- table selection: exact case-sensitive selector (schema.py
  convention); sheet_name-only selectors ignored (each validator
  reads only its own field); no selector -> exactly-one rule (zero or
  many -> stable codes with the available names listed); sqlite_%
  system tables never count as user tables
- type affinity mapping (value-level, documented in the validator):
  TEXT classified by the shared text grammar, INTEGER/REAL by value,
  BLOB never valid, NULL the null path — parametrized matrix over all
  five DSL types
- unique semantics mirroring SQLite index comparison: 1 collides with
  1.0 but not '1'; edge-trimmed TEXT collides; NULLs never tracked
- nulls: SQL NULL and whitespace-only TEXT both the null path (the
  shared contract), null ratios, NULL_VIOLATION, max_null_ratio
- resource bounds: row cap with scan_incomplete semantics; cooperative
  timeout (progress handler + the shared every-1024-rows check) via
  scripted clocks; cell length cap on TEXT and BLOB
- failure surface: non-UTF8 TEXT -> INVALID_ENCODING mid-scan with
  partial counts; corrupt tail after a valid magic -> MALFORMED_SQLITE;
  a missing path re-raises OSError (infrastructure, worker retry owns)
- surface: path with spaces / unicode / '?' opens (URI quoting),
  embedded-quote identifiers quoted by doubling, parser_version /
  file_type / duration_ms fields
"""

from __future__ import annotations

import hashlib
import sqlite3
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
from app.modules.submissions.validators.sqlite_validator import (
    PARSER_VERSION,
    validate_sqlite,
)

HEADER = ("url", "title", "publish_time", "likes")
R1 = ("https://a.example/1", "First", "2026-01-02T03:04:05", 10)
R2 = ("https://a.example/2", "Second", "2026-01-03", None)
R3 = ("https://a.example/3", "Third", "2026-01-04", 0)
DATA_ROWS = (R1, R2, R3)

SQLITE_MAGIC = b"SQLite format 3\x00"


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


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def build_db(
    path: Path,
    *,
    table: str = "data",
    header: tuple[str, ...] | list[str] = HEADER,
    rows: list[tuple[Any, ...]] | None = None,
    extra_tables: tuple[tuple[str, list[str], list[tuple[Any, ...]]], ...] = (),
    autoincrement_pk: bool = False,
    wal: bool = False,
) -> Path:
    """A real SQLite 3 file; columns without declared types keep the
    storage classes the rows were inserted with.

    ``autoincrement_pk`` prepends an ``id INTEGER PRIMARY KEY
    AUTOINCREMENT`` column (which makes SQLite maintain the
    sqlite_sequence system table once a row is inserted); the id is
    auto-assigned, rows carry only the ``header`` columns.
    """
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=wal")
    data_rows = list(DATA_ROWS) if rows is None else rows
    columns = ", ".join(_q(c) for c in header)
    if autoincrement_pk:
        columns = "id INTEGER PRIMARY KEY AUTOINCREMENT, " + columns
    conn.execute(f"CREATE TABLE {_q(table)} ({columns})")
    if autoincrement_pk:
        names = ", ".join(_q(c) for c in header)
        conn.executemany(
            f"INSERT INTO {_q(table)} ({names}) "
            f"VALUES ({', '.join('?' * len(header))})",
            data_rows,
        )
    else:
        conn.executemany(
            f"INSERT INTO {_q(table)} VALUES ({', '.join('?' * len(header))})",
            data_rows,
        )
    for name, other_header, other_rows in extra_tables:
        conn.execute(
            f"CREATE TABLE {_q(name)} ({', '.join(_q(c) for c in other_header)})"
        )
        conn.executemany(
            f"INSERT INTO {_q(name)} VALUES ({', '.join('?' * len(other_header))})",
            other_rows,
        )
    conn.commit()
    conn.close()
    return path


def build_empty_db(path: Path) -> Path:
    """A valid SQLite file with schema initialized and zero tables."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    return path


def build_bad_utf8_db(path: Path) -> Path:
    """A valid file whose second row carries TEXT bytes that are not
    UTF-8 (TEXT storage class, invalid encoding)."""
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE data ({', '.join(_q(c) for c in HEADER)})")
    conn.execute(
        "INSERT INTO data VALUES (?, ?, ?, ?)",
        ("https://a.example/1", "First", "2026-01-02", 10),
    )
    conn.execute(
        "INSERT INTO data VALUES "
        "('https://a.example/2', CAST(x'82acd8ab' AS TEXT), '2026-01-03', NULL)"
    )
    conn.execute(
        "INSERT INTO data VALUES (?, ?, ?, ?)",
        ("https://a.example/3", "Third", "2026-01-04", 0),
    )
    conn.commit()
    conn.close()
    return path


def single_column_db(path: Path, values: list[Any]) -> Path:
    """One user table with a single no-affinity column 'v'."""
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE data ("v")')
    conn.executemany("INSERT INTO data VALUES (?)", [(value,) for value in values])
    conn.commit()
    conn.close()
    return path


def run(
    path: Path,
    schema: SubmissionSchema | None = None,
    limits: ValidationLimits | None = None,
    clock: Any = None,
) -> ValidationReport:
    if clock is None:
        return validate_sqlite(path, schema or make_schema(), limits)
    return validate_sqlite(path, schema or make_schema(), limits, clock=clock)


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


class DelayedClock:
    """Flat for the first `flat_calls` calls, then jumps by `jump` each."""

    def __init__(self, flat_calls: int, jump: float) -> None:
        self.remaining_flat = flat_calls
        self.jump = jump
        self.now = 100.0

    def __call__(self) -> float:
        if self.remaining_flat > 0:
            self.remaining_flat -= 1
        else:
            self.now += self.jump
        return self.now


# --- the brief's fixture cases ------------------------------------------------


def test_valid_single_table_passes_with_full_report(tmp_path: Path) -> None:
    path = build_db(tmp_path / "upload.sqlite")

    report = run(path)

    assert report.passed is True
    assert report.errors == ()
    assert report.warnings == ()
    assert report.parser_version == PARSER_VERSION
    assert report.file_type is FileType.SQLITE
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


def test_fake_db_file_rejected_by_header_gate(tmp_path: Path) -> None:
    path = tmp_path / "fake.sqlite"
    path.write_bytes(b"this is definitely not a database file ......" * 4)

    report = run(path)

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.MALFORMED_SQLITE}
    assert report.row_count == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"SQLite format 3",  # truncated right before the NUL
        SQLITE_MAGIC,  # magic and nothing after it
        SQLITE_MAGIC + b"\x00" * 64,  # zero tail: not a page-1 layout
        SQLITE_MAGIC + b"garbage tail that never parses" * 8,
        b"\x89PNG\r\n\x1a\nIHDR\x12\x34\x56\x78\x9a",
    ],
)
def test_bad_files_are_malformed_not_crashes(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "bad.sqlite"
    path.write_bytes(payload)

    report = run(path)

    assert error_codes(report) == {ValidationCode.MALFORMED_SQLITE}
    assert report.row_count == 0
    assert report.detected_columns == ()


def test_empty_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    path.write_bytes(b"")

    report = run(path)

    assert error_codes(report) == {ValidationCode.EMPTY_FILE}
    assert report.row_count == 0


def test_multiple_tables_without_selector_fail(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "multi.sqlite",
        extra_tables=(("other", ["a", "b"], [(1, 2)]),),
    )

    report = run(path)

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.AMBIGUOUS_TABLE}
    finding = first_finding(report, ValidationCode.AMBIGUOUS_TABLE)
    assert "table_name" in finding.message
    assert "data" in finding.message and "other" in finding.message
    assert report.row_count == 0


def test_selector_picks_named_table(tmp_path: Path) -> None:
    # The unnamed default table holds garbage; only the named one is valid.
    path = build_db(
        tmp_path / "multi.sqlite",
        header=("junk", "junk2", "junk3", "junk4"),
        rows=[("x", "y", "z", 1)],
        extra_tables=(("good", list(HEADER), list(DATA_ROWS)),),
    )
    report = run(path, make_schema(source_selector={"table_name": "good"}))

    assert report.passed is True
    assert report.row_count == 3
    assert report.detected_columns == HEADER


def test_selected_table_missing_fails(tmp_path: Path) -> None:
    path = build_db(tmp_path / "db.sqlite")

    report = run(path, make_schema(source_selector={"table_name": "Nope"}))

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.TABLE_NOT_FOUND}
    finding = first_finding(report, ValidationCode.TABLE_NOT_FOUND)
    assert "Nope" in finding.message
    assert "data" in finding.message  # available names are listed
    assert report.row_count == 0


def test_selector_match_is_case_sensitive(tmp_path: Path) -> None:
    path = build_db(tmp_path / "db.sqlite")

    assert run(path, make_schema(source_selector={"table_name": "data"})).passed
    wrong = run(path, make_schema(source_selector={"table_name": "Data"}))
    assert error_codes(wrong) == {ValidationCode.TABLE_NOT_FOUND}


def test_sheet_name_only_selector_is_ignored(tmp_path: Path) -> None:
    # A multi-format schema may carry sheet_name; the SQLite validator
    # reads only its own table_name field.
    path = build_db(
        tmp_path / "multi.sqlite",
        extra_tables=(("other", ["a"], [(1,)]),),
    )

    report = run(path, make_schema(source_selector={"sheet_name": "S"}))

    assert error_codes(report) == {ValidationCode.AMBIGUOUS_TABLE}


def test_database_without_user_tables_fails(tmp_path: Path) -> None:
    path = build_empty_db(tmp_path / "empty.sqlite")

    report = run(path)

    assert error_codes(report) == {ValidationCode.TABLE_NOT_FOUND}
    assert "用户表" in first_finding(report, ValidationCode.TABLE_NOT_FOUND).message


def test_system_tables_do_not_count(tmp_path: Path) -> None:
    # The table's AUTOINCREMENT pk makes SQLite maintain
    # sqlite_sequence; it must not count as a user table, so the
    # no-selector exactly-one rule still picks 'data'.
    path = build_db(tmp_path / "seq.sqlite", autoincrement_pk=True)

    with sqlite3.connect(path) as check:
        names = {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "sqlite_sequence" in names  # the fixture really has it

    schema = make_schema(
        optional_columns=[
            {"name": "likes", "type": "integer", "nullable": True},
            {"name": "id", "type": "integer", "nullable": True},
        ]
    )
    report = run(path, schema)

    assert report.passed is True
    assert report.row_count == 3


def test_schema_mismatch_fails(tmp_path: Path) -> None:
    path = single_column_db(tmp_path / "db.sqlite", ["not-a-number"])

    report = run(path, make_schema())

    assert report.passed is False
    assert report.missing_required_columns == ("url", "title", "publish_time")


def test_type_mismatch_end_to_end(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "db.sqlite",
        rows=[
            ("https://a.example/1", "First", "2026-01-02", "abc"),
            ("https://a.example/2", "Second", "not-a-date", 5),
        ],
    )

    report = run(path)

    assert report.type_error_counts == {
        "url": 0,
        "title": 0,
        "publish_time": 1,
        "likes": 1,
    }
    findings = [f for f in report.errors if f.code == ValidationCode.TYPE_ERROR]
    assert {f.column for f in findings} == {"publish_time", "likes"}
    assert report.passed is False


def test_row_cap_enforcement_with_scan_incomplete_semantics(
    tmp_path: Path,
) -> None:
    rows = [(f"https://a.example/{i}", "T", "2026-01-02", None) for i in range(10)]
    path = build_db(tmp_path / "db.sqlite", rows=rows)
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

    report = run(path, schema, ValidationLimits(row_cap=5))

    assert ValidationCode.ROW_LIMIT_EXCEEDED in error_codes(report)
    assert report.row_count == 6  # counted one past the cap, then stopped
    assert ValidationCode.ROW_COUNT_TRUNCATED in warning_codes(report)
    # No conclusion from truncated counts: neither min_rows...
    assert ValidationCode.MIN_ROWS_NOT_MET not in error_codes(report)
    # ...nor the null-ratio verdict.
    assert ValidationCode.NULL_RATIO_EXCEEDED not in error_codes(report)


# --- read-only hardening (spec §12.3) -----------------------------------------


def test_connection_policy_blocks_every_write(tmp_path: Path) -> None:
    from app.modules.submissions.validators import sqlite_validator

    path = build_db(tmp_path / "db.sqlite")
    conn = sqlite_validator._open_read_only(path, 30.0)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("INSERT INTO data VALUES ('u', 't', '2026-01-02', 1)")
        with pytest.raises(sqlite3.Error):
            conn.execute("CREATE TABLE z (a)")
        with pytest.raises(sqlite3.Error):
            conn.execute("ATTACH DATABASE ':memory:' AS z")
        with pytest.raises(sqlite3.Error):
            conn.execute("PRAGMA user_version = 5")
        with pytest.raises(sqlite3.Error):
            conn.execute("SELECT abs(1)")  # even function invocations
        # Reads of an allowed table still work after the table joins
        # the allowlist (what the scan phase does).
        conn.set_authorizer(
            sqlite_validator._make_authorizer({"sqlite_master", "data"})
        )
        assert conn.execute('SELECT "url" FROM "data"').fetchall()
    finally:
        conn.close()


def test_authorizer_callback_policy_matrix() -> None:
    from app.modules.submissions.validators import sqlite_validator

    authorizer = sqlite_validator._make_authorizer({"sqlite_master", "data"})

    ok = sqlite3.SQLITE_OK
    deny = sqlite3.SQLITE_DENY
    assert authorizer(sqlite3.SQLITE_SELECT, None, None, None, None) == ok
    assert authorizer(sqlite3.SQLITE_READ, "data", "url", "main", None) == ok
    assert authorizer(sqlite3.SQLITE_READ, "sqlite_master", "name", "main", None) == ok
    assert authorizer(sqlite3.SQLITE_PRAGMA, "table_info", '"data"', None, None) == ok
    # Reads of anything not in the allowlist are denied.
    assert authorizer(sqlite3.SQLITE_READ, "other", "x", "main", None) == deny
    # Every pragma except table_info — reads and writes alike.
    assert authorizer(sqlite3.SQLITE_PRAGMA, "query_only", "ON", None, None) == deny
    assert authorizer(sqlite3.SQLITE_PRAGMA, "journal_mode", "wal", None, None) == deny
    # Attach/detach, DML/DDL, transactions, functions, recursion.
    for action in (
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    ):
        assert authorizer(action, None, None, None, None) == deny, action


def test_validation_never_touches_the_file(tmp_path: Path) -> None:
    path = build_db(tmp_path / "db.sqlite")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    report = run(path)

    assert report.passed is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    # immutable=1: no -journal / -wal / -shm sidecar is ever created.
    assert [entry.name for entry in tmp_path.iterdir()] == ["db.sqlite"]


def test_wal_mode_database_validates_without_sidecars(tmp_path: Path) -> None:
    # A WAL-mode upload (cleanly closed, so checkpointed into the main
    # file) validates under the immutable open, which creates none of
    # the -wal / -shm sidecars a plain read-only open would need.
    path = build_db(tmp_path / "wal.sqlite", wal=True)
    assert [entry.name for entry in tmp_path.iterdir()] == ["wal.sqlite"]

    report = run(path)

    assert report.passed is True
    assert report.row_count == 3
    assert [entry.name for entry in tmp_path.iterdir()] == ["wal.sqlite"]


def test_read_only_directory_is_enough(tmp_path: Path) -> None:
    path = build_db(tmp_path / "db.sqlite")
    tmp_path.chmod(0o500)
    try:
        report = run(path)
    finally:
        tmp_path.chmod(0o700)

    assert report.passed is True
    assert report.row_count == 3


# --- value-level type affinity matrix ------------------------------------------


@pytest.mark.parametrize(
    ("column_type", "value", "ok"),
    [
        # string: any scalar text/numeric value; BLOB never.
        ("string", "text", True),
        ("string", 12, True),
        ("string", 1.5, True),
        ("string", b"\x00\x01", False),
        # integer: INTEGER storage, integral REAL, and TEXT in the
        # shared integer grammar.
        ("integer", 12, True),
        ("integer", -7, True),
        ("integer", 12.0, True),
        ("integer", 12.5, False),
        ("integer", "123", True),
        ("integer", "007", True),
        ("integer", "12.5", False),
        ("integer", b"\x01", False),
        # number: INTEGER, finite REAL, TEXT in the number grammar.
        ("number", 12, True),
        ("number", 1.5, True),
        ("number", "1.5e3", True),
        ("number", "abc", False),
        ("number", float("inf"), False),
        # boolean: INTEGER 0/1 (SQLite convention) plus the shared
        # TEXT vocabulary; other numerics are not booleans.
        ("boolean", 0, True),
        ("boolean", 1, True),
        ("boolean", 2, False),
        ("boolean", "true", True),
        ("boolean", "yes", True),
        ("boolean", 1.0, False),
        # datetime: TEXT in the shared ISO grammar only — integer
        # epochs are rejected (no unit convention in the DSL).
        ("datetime", "2026-01-02", True),
        ("datetime", "2026-01-02 03:04:05", True),
        ("datetime", 1767000000, False),
        ("datetime", b"2026-01-02", False),
    ],
)
def test_storage_class_affinity_matrix(
    tmp_path: Path, column_type: str, value: Any, ok: bool
) -> None:
    path = single_column_db(tmp_path / "db.sqlite", [value])
    schema = SubmissionSchema.parse(
        {"required_columns": [{"name": "v", "type": column_type}]}
    )

    report = run(path, schema)

    assert report.passed is ok, report.errors
    assert report.type_error_counts["v"] == (0 if ok else 1)


def test_non_utf8_text_fails_as_invalid_encoding(tmp_path: Path) -> None:
    path = build_bad_utf8_db(tmp_path / "db.sqlite")

    report = run(path)

    assert error_codes(report) == {ValidationCode.INVALID_ENCODING}
    assert report.row_count == 1  # the row before the bad fetch counted
    assert ValidationCode.ROW_COUNT_TRUNCATED in warning_codes(report)


def test_unique_semantics_mirror_sqlite_index_comparison(tmp_path: Path) -> None:
    # 1 and 1.0 collide (numeric comparison); 1 and '1' do not (text
    # and numeric are distinct storage classes); edge-trimmed text
    # collides; NULLs are never tracked.
    path = single_column_db(
        tmp_path / "db.sqlite",
        ["a", "a ", 1, "1", 1.0, None, None],
    )
    schema = SubmissionSchema.parse(
        {
            "required_columns": [
                {"name": "v", "type": "string", "nullable": True, "unique": True},
            ],
        }
    )

    report = run(path, schema)

    assert report.passed is False
    findings = [f for f in report.errors if f.code == ValidationCode.DUPLICATE_VALUE]
    assert {f.row for f in findings} == {2, 5}
    assert report.duplicate_counts == {"v": 2}


def test_null_semantics_shared_contract(tmp_path: Path) -> None:
    # SQL NULL and whitespace-only TEXT are both the null path; null
    # ratios, NULL_VIOLATION, and max_null_ratio all follow.
    path = build_db(
        tmp_path / "db.sqlite",
        header=("url", "title"),
        rows=[("u1", "t1"), (None, "  "), ("u3", None)],
    )
    schema = SubmissionSchema.parse(
        {
            "required_columns": [
                {"name": "url", "type": "string"},
                {
                    "name": "title",
                    "type": "string",
                    "nullable": True,
                    "max_null_ratio": 0.25,
                },
            ],
        }
    )

    report = run(path, schema)

    assert report.null_ratios == {"url": round(1 / 3, 6), "title": round(2 / 3, 6)}
    violations = [f for f in report.errors if f.code == ValidationCode.NULL_VIOLATION]
    assert len(violations) == 1 and violations[0].row == 2
    ratio = first_finding(report, ValidationCode.NULL_RATIO_EXCEEDED)
    assert ratio.column == "title"


# --- resource bounds ------------------------------------------------------------


def test_timeout_trips_mid_scan(tmp_path: Path) -> None:
    rows = [(f"https://a.example/{i}", "T", "2026-01-02", None) for i in range(20000)]
    path = build_db(tmp_path / "db.sqlite", rows=rows)
    schema = make_schema(
        min_rows=30000,
        optional_columns=[
            {
                "name": "likes",
                "type": "integer",
                "nullable": True,
                "max_null_ratio": 0.05,
            },
        ],
    )

    report = run(
        path,
        schema,
        ValidationLimits(timeout_seconds=1.0),
        clock=DelayedClock(flat_calls=8, jump=1000.0),
    )

    assert ValidationCode.TIMEOUT in error_codes(report)
    assert report.passed is False
    assert 0 <= report.row_count < 20000
    assert ValidationCode.ROW_COUNT_TRUNCATED in warning_codes(report)
    # No verdicts from a truncated scan.
    assert ValidationCode.MIN_ROWS_NOT_MET not in error_codes(report)
    assert ValidationCode.NULL_RATIO_EXCEEDED not in error_codes(report)


def test_timeout_trips_immediately(tmp_path: Path) -> None:
    # Enough rows to guarantee a budget checkpoint (the tiny fixtures
    # never reach one — the point of the immediate test is that the
    # FIRST checkpoint aborts, wherever it lands).
    rows = [(f"https://a.example/{i}", "T", "2026-01-02", None) for i in range(2000)]
    path = build_db(tmp_path / "db.sqlite", rows=rows)

    report = run(
        path,
        limits=ValidationLimits(timeout_seconds=1.0),
        clock=FakeClock(step=1000.0),
    )

    assert ValidationCode.TIMEOUT in error_codes(report)
    assert report.passed is False
    assert report.row_count <= 1024  # at most one cooperative-check window


def test_cell_length_cap_on_text_and_blob(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "db.sqlite",
        header=("v",),
        rows=[("x" * 65_537,), (b"\x00" * 65_537,)],
    )
    schema = SubmissionSchema.parse(
        {"required_columns": [{"name": "v", "type": "string"}]}
    )

    report = run(path, schema)

    findings = [f for f in report.errors if f.code == ValidationCode.CELL_TOO_LONG]
    assert len(findings) == 2
    assert {f.row for f in findings} == {1, 2}
    assert all(f.value is None for f in findings)  # never echoed
    assert ValidationCode.TYPE_ERROR not in error_codes(report)


def test_too_many_columns_aborts(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "db.sqlite",
        header=tuple(f"c{index}" for index in range(6)),
        rows=[("x",) * 6],
    )

    report = run(path, limits=ValidationLimits(max_columns=5))

    assert error_codes(report) == {ValidationCode.TOO_MANY_COLUMNS}
    assert report.row_count == 0
    assert report.detected_columns == tuple(f"c{index}" for index in range(5))


def test_empty_table_min_rows(tmp_path: Path) -> None:
    path = build_db(tmp_path / "db.sqlite", rows=[])

    report = run(path, make_schema(min_rows=5))

    assert ValidationCode.NO_DATA_ROWS in error_codes(report)
    assert ValidationCode.MIN_ROWS_NOT_MET in error_codes(report)
    assert report.row_count == 0
    assert report.detected_columns == HEADER


# --- header semantics (schema.py convention carry) -------------------------------


def test_column_match_is_case_sensitive(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "db.sqlite",
        header=("URL", "title", "publish_time", "likes"),
    )

    report = run(path)

    assert report.missing_required_columns == ("url",)
    assert report.extra_columns == ("URL",)


def test_column_edge_whitespace_trimmed(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "db.sqlite",
        header=(" url ", "title", "publish_time ", "likes"),
    )

    report = run(path)

    assert report.passed is True
    assert report.detected_columns == HEADER


def test_extra_column_rejected_and_allowed(tmp_path: Path) -> None:
    path = build_db(
        tmp_path / "db.sqlite",
        header=("url", "title", "publish_time", "likes", "notes"),
        rows=[("https://a.example/1", "First", "2026-01-02", 10, "n")],
    )

    strict = run(path)
    assert ValidationCode.EXTRA_COLUMN in error_codes(strict)
    assert strict.extra_columns == ("notes",)

    permissive = run(path, make_schema(allow_extra_columns=True))
    assert permissive.passed is True
    assert permissive.extra_columns == ("notes",)


def test_embedded_quote_identifiers_are_doubled(tmp_path: Path) -> None:
    path = build_db(tmp_path / "quote.sqlite", table='we"ird')

    report = run(path, make_schema(source_selector={"table_name": 'we"ird'}))

    assert report.passed is True
    assert report.row_count == 3


# --- surface -----------------------------------------------------------------------


def test_path_with_spaces_unicode_and_question_mark(tmp_path: Path) -> None:
    path = build_db(tmp_path / "up load?é.sqlite")

    report = run(path)

    assert report.passed is True
    assert report.row_count == 3


def test_missing_path_propagates_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        validate_sqlite(tmp_path / "nope.sqlite", make_schema())


def test_duration_ms_from_fake_clock(tmp_path: Path) -> None:
    path = build_db(tmp_path / "db.sqlite")

    report = run(path, clock=FakeClock(step=0.0))

    assert report.duration_ms == 0.0
