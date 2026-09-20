# backend/tests/unit/submissions/test_csv_validator.py
"""Unit tests for the streaming CSV submission validator (spec §12.1,
§12.4; backend-engineering §14: bounded rows/cells/columns/time,
streaming, parser exceptions -> validation outcomes).

The validator consumes a binary stream and produces the structured
§12.4 report. Everything here is pure unit work: in-memory byte
fixtures, injected clock for the cooperative timeout, no database and
no services.

Coverage matrix:

- the 11 fixture cases from the task brief: valid UTF-8; UTF-8 BOM;
  empty file; header only; missing required field; duplicate unique
  URL; min_rows-1; exactly min_rows; max_rows+1; invalid datetime;
  extremely long cell; binary masquerading as CSV (NUL bytes and a
  decode-failing binary header are distinct stable codes)
- resource bounds: cooperative timeout trips via a fake clock;
  row-cap abort reports ROW_LIMIT_EXCEEDED with a truncated row count;
  the row cap wins over a larger schema max_rows; header wider than
  max_columns aborts; a read-probe stream proves chunked (streaming)
  reads — no whole-file read even for a ~500 KB payload
- encoding policy (V1: reject non-UTF-8): GB18030 bytes and mid-stream
  invalid UTF-8 both fail with INVALID_ENCODING and a message naming
  UTF-8; the BOM is accepted
- dialect: comma/semicolon/tab sniffed; undialectable content and an
  unterminated quote fail as MALFORMED_CSV without escaping; CRLF and
  quoted cells (embedded comma + newline) parse correctly
- header semantics (schema.py convention carry): case-sensitive
  exact-match after edge-whitespace trim; missing required /
  extra columns with the allow_extra_columns gate; duplicate header
  names error
- per-row semantics: nullable violations, null-ratio accumulation and
  the max_null_ratio boundary (ratio == cap passes, > cap fails),
  row-shape mismatch, blank lines skipped, unique-set overflow
  degrades to sampled duplicate detection with a warning while
  duplicate_counts stays a lower bound
- findings bounded: 150 bad rows keep exact counters but cap the error
  sample list and append a truncation warning
- shared type helpers: integer/number/boolean strict closed literal
  sets (full-width digits, underscores, inf/nan, 1e3-as-integer all
  rejected), datetime via the documented fromisoformat tolerant set,
  number/boolean end-to-end through a schema
- surface: ValidationLimits defaults/positivity/frozen; report frozen
  with the passed property; ValidationCode values equal their names
"""

from __future__ import annotations

import dataclasses
import io
from typing import Any

import pytest

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import (
    MAX_ROWS_CEILING,
    SubmissionSchema,
)
from app.modules.submissions.validators import csv_validator
from app.modules.submissions.validators.common import (
    ValidationCode,
    ValidationLimits,
    ValidationReport,
    check_cell_type,
    is_valid_datetime,
)
from app.modules.submissions.validators.csv_validator import (
    PARSER_VERSION,
    validate_csv,
)

HEADER = "url,title,publish_time,likes"
R1 = "https://a.example/1,First,2026-01-02T03:04:05,10"
R1_WITH_NOTE = "https://a.example/1,First,2026-01-02T03:04:05,10,a note"
R2 = "https://a.example/2,Second,2026-01-03T03:04:05,"
R3 = "https://a.example/3,Third,2026-01-04,0"


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


def csv_bytes(*rows: str, header: str = HEADER) -> bytes:
    lines = [header, *rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def run(
    data: bytes,
    schema: SubmissionSchema | None = None,
    limits: ValidationLimits | None = None,
    clock: Any = None,
) -> ValidationReport:
    stream = io.BytesIO(data)
    if clock is None:
        report = validate_csv(stream, schema or make_schema(), limits)
    else:
        report = validate_csv(stream, schema or make_schema(), limits, clock=clock)
    # The validator consumes the stream but never closes the caller's.
    assert not stream.closed
    return report


def error_codes(report: ValidationReport) -> set[ValidationCode]:
    return {finding.code for finding in report.errors}


def warning_codes(report: ValidationReport) -> set[ValidationCode]:
    return {finding.code for finding in report.warnings}


def first_error(report: ValidationReport, code: ValidationCode) -> Any:
    return next(f for f in report.errors if f.code == code)


class FakeClock:
    """Monotonic-callable stub: every call advances time by `step`."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 100.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class ReadProbe(io.RawIOBase):
    """Byte stream that records the largest single read() request."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.max_request = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 10**9
        self.max_request = max(self.max_request, size)
        part, self._data = self._data[:size], self._data[size:]
        return part


# --- the 11 fixture cases -------------------------------------------------


def test_valid_utf8_csv_passes() -> None:
    report = run(csv_bytes(R1, R2, R3))

    assert report.passed is True
    assert report.errors == ()
    assert report.warnings == ()
    assert report.parser_version == PARSER_VERSION
    assert report.file_type is FileType.CSV
    assert report.row_count == 3
    assert report.detected_columns == ("url", "title", "publish_time", "likes")
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


def test_utf8_bom_accepted() -> None:
    report = run(b"\xef\xbb\xbf" + csv_bytes(R1, R2, R3))

    assert report.passed is True
    assert report.detected_columns == ("url", "title", "publish_time", "likes")


def test_empty_file_rejected() -> None:
    report = run(b"")

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.EMPTY_FILE}
    assert report.row_count == 0
    assert report.detected_columns == ()


def test_header_only_rejected_no_data_rows() -> None:
    report = run(csv_bytes())

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.NO_DATA_ROWS}
    assert report.row_count == 0
    assert report.detected_columns == ("url", "title", "publish_time", "likes")


def test_header_only_with_min_rows_reports_both() -> None:
    report = run(csv_bytes(), make_schema(min_rows=500))

    assert error_codes(report) == {
        ValidationCode.NO_DATA_ROWS,
        ValidationCode.MIN_ROWS_NOT_MET,
    }


def test_missing_required_column() -> None:
    report = run(csv_bytes(R1, header="url,title,likes"))

    assert ValidationCode.MISSING_REQUIRED_COLUMN in error_codes(report)
    assert report.missing_required_columns == ("publish_time",)
    finding = first_error(report, ValidationCode.MISSING_REQUIRED_COLUMN)
    assert finding.column == "publish_time"


def test_duplicate_unique_url() -> None:
    report = run(csv_bytes(R1, R1, R3))

    assert ValidationCode.DUPLICATE_VALUE in error_codes(report)
    finding = first_error(report, ValidationCode.DUPLICATE_VALUE)
    assert finding.row == 2
    assert finding.column == "url"
    assert finding.value == "https://a.example/1"
    assert report.duplicate_counts == {"url": 1}
    assert report.row_count == 3


def test_below_min_rows() -> None:
    report = run(csv_bytes(R1, R2), make_schema(min_rows=3))

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.MIN_ROWS_NOT_MET}
    assert report.row_count == 2


def test_exactly_min_rows_passes() -> None:
    report = run(csv_bytes(R1, R2, R3), make_schema(min_rows=3))

    assert report.passed is True
    assert report.row_count == 3


def test_above_max_rows() -> None:
    extra = R1.replace("a.example/1", "a.example/9")
    report = run(csv_bytes(R1, R2, R3, extra), make_schema(max_rows=3))

    assert report.passed is False
    assert error_codes(report) == {ValidationCode.MAX_ROWS_EXCEEDED}
    assert report.row_count == 4
    assert warning_codes(report) == set()


def test_invalid_datetime_type_error() -> None:
    bad = "https://a.example/1,First,not-a-date,10"
    report = run(csv_bytes(bad))

    assert error_codes(report) == {ValidationCode.TYPE_ERROR}
    finding = first_error(report, ValidationCode.TYPE_ERROR)
    assert finding.row == 1
    assert finding.column == "publish_time"
    assert finding.value == "not-a-date"
    assert report.type_error_counts == {
        "url": 0,
        "title": 0,
        "publish_time": 1,
        "likes": 0,
    }


def test_extremely_long_cell_is_row_level_error() -> None:
    long_cell = "u" * 65_537  # one past the default max_cell_length
    row = f"https://a.example/1,{long_cell},2026-01-02,10"
    report = run(csv_bytes(row))

    assert error_codes(report) == {ValidationCode.CELL_TOO_LONG}
    finding = first_error(report, ValidationCode.CELL_TOO_LONG)
    assert finding.row == 1
    assert finding.column == "title"
    # Bounded echo: the giant cell is not reproduced in the report.
    assert finding.value is None
    assert report.row_count == 1


def test_binary_nul_bytes_rejected() -> None:
    report = run(b"\x00\x01\x02\x03url,title\nhttps://a,First\n")

    assert error_codes(report) == {ValidationCode.BINARY_CONTENT}
    assert report.row_count == 0


def test_binary_header_rejected_invalid_encoding() -> None:
    report = run(b"\x89PNG\r\n\x1a\nIHDR\x12\x34\x56")

    assert error_codes(report) == {ValidationCode.INVALID_ENCODING}
    assert "UTF-8" in first_error(report, ValidationCode.INVALID_ENCODING).message


# --- resource bounds -------------------------------------------------------


def test_timeout_trips_with_fake_clock() -> None:
    rows = [f"https://a.example/{i},T,2026-01-02," for i in range(5000)]
    limits = ValidationLimits(timeout_seconds=1.0)
    report = run(csv_bytes(*rows), limits=limits, clock=FakeClock(step=10.0))

    assert error_codes(report) == {ValidationCode.TIMEOUT}
    assert report.passed is False
    # The cooperative check runs every 4096 data rows: rows 1..4096 were
    # processed, then the elapsed check aborted the scan.
    assert report.row_count == 4096
    # A timed-out scan is incomplete: the count is a lower bound.
    assert warning_codes(report) == {ValidationCode.ROW_COUNT_TRUNCATED}
    assert report.duration_ms == 20000.0


def test_row_cap_aborts() -> None:
    rows = [f"https://a.example/{i},T,2026-01-02," for i in range(10)]
    report = run(csv_bytes(*rows), limits=ValidationLimits(row_cap=5))

    assert error_codes(report) == {ValidationCode.ROW_LIMIT_EXCEEDED}
    assert report.row_count == 6  # counted one past the cap, then stopped
    assert ValidationCode.ROW_COUNT_TRUNCATED in warning_codes(report)


def test_row_cap_wins_over_larger_schema_max_rows() -> None:
    rows = [f"https://a.example/{i},T,2026-01-02," for i in range(20)]
    # min_rows sits ABOVE what the budget could ever count: an
    # incomplete scan must not conclude MIN_ROWS_NOT_MET from a
    # partial count.
    report = run(
        csv_bytes(*rows),
        make_schema(min_rows=10, max_rows=10),
        ValidationLimits(row_cap=5),
    )

    assert error_codes(report) == {ValidationCode.ROW_LIMIT_EXCEEDED}
    assert report.row_count == 6
    assert warning_codes(report) == {ValidationCode.ROW_COUNT_TRUNCATED}


def test_too_many_columns_aborts() -> None:
    report = run(
        csv_bytes(R1, header="url,title,publish_time,likes,notes,extra"),
        limits=ValidationLimits(max_columns=5),
    )

    assert error_codes(report) == {ValidationCode.TOO_MANY_COLUMNS}
    assert report.row_count == 0


def test_streaming_never_reads_whole_file() -> None:
    payload = csv_bytes(*[f"https://a.example/{i},T,2026-01-02," for i in range(20000)])
    assert len(payload) > 400_000  # the fixture really is multi-chunk
    probe = ReadProbe(payload)
    report = validate_csv(probe, make_schema())

    assert report.passed is True
    assert report.row_count == 20000
    # Every single read stayed at chunk size: no whole-file materialization.
    assert probe.max_request <= 16384
    assert probe.max_request < len(payload)


# --- encoding and dialect ---------------------------------------------------


def test_non_utf8_gb18030_rejected_with_clear_prompt() -> None:
    report = run("网址,标题\nhttps://a,第一\n".encode("gb18030"))

    assert error_codes(report) == {ValidationCode.INVALID_ENCODING}
    assert "UTF-8" in first_error(report, ValidationCode.INVALID_ENCODING).message


def test_mid_stream_invalid_utf8_rejected() -> None:
    # The first sniff chunk (8 KiB) is valid; the invalid byte appears later.
    data = csv_bytes(*[f"https://a.example/{i},T,2026-01-02," for i in range(700)])
    assert len(data) > 8192
    report = run(data + b"\xffgarbage")

    assert ValidationCode.INVALID_ENCODING in error_codes(report)
    assert report.row_count > 0


def test_semicolon_delimiter_sniffed() -> None:
    data = b"url;title;publish_time;likes\nhttps://a;First;2026-01-02;10\n"
    report = run(data)

    assert report.passed is True
    assert report.row_count == 1


def test_tab_delimiter_sniffed() -> None:
    data = b"url\ttitle\tpublish_time\tlikes\nhttps://a\tFirst\t2026-01-02\t10\n"
    report = run(data)

    assert report.passed is True
    assert report.row_count == 1


def test_undialectable_content_rejected() -> None:
    report = run(b"%%%\n")

    assert error_codes(report) == {ValidationCode.MALFORMED_CSV}
    assert report.row_count == 0


def test_unterminated_quote_rejected_not_raised() -> None:
    report = run(b'url,title,publish_time,likes\n"open-quote,never-closed\n')

    assert error_codes(report) == {ValidationCode.MALFORMED_CSV}


def test_crlf_line_endings_parse_cleanly() -> None:
    data = b"url,title,publish_time,likes\r\nhttps://a,First,2026-01-02,10\r\n"
    report = run(data)

    assert report.passed is True
    assert report.detected_columns == ("url", "title", "publish_time", "likes")
    assert report.row_count == 1


def test_quoted_cells_with_comma_and_embedded_newline() -> None:
    row = '"https://a.example/1?p=1,2","multi\nline title",2026-01-02,10'
    report = run(csv_bytes(row))

    assert report.passed is True
    assert report.row_count == 1


# --- header semantics -------------------------------------------------------


def test_header_match_is_case_sensitive() -> None:
    report = run(csv_bytes(R1, header="URL,title,publish_time,likes"))

    assert report.missing_required_columns == ("url",)
    assert report.extra_columns == ("URL",)
    assert ValidationCode.MISSING_REQUIRED_COLUMN in error_codes(report)
    assert ValidationCode.EXTRA_COLUMN in error_codes(report)


def test_header_edge_whitespace_trimmed() -> None:
    report = run(csv_bytes(R1, header=" url ,title,publish_time ,likes"))

    assert report.passed is True
    assert report.detected_columns == ("url", "title", "publish_time", "likes")


def test_duplicate_header_names_error() -> None:
    report = run(csv_bytes(R1, header="url,url,title,publish_time,likes"))

    assert ValidationCode.DUPLICATE_HEADER in error_codes(report)
    finding = first_error(report, ValidationCode.DUPLICATE_HEADER)
    assert "url" in finding.message


def test_extra_column_rejected_by_default() -> None:
    report = run(csv_bytes(R1_WITH_NOTE, header="url,title,publish_time,likes,notes"))

    assert error_codes(report) == {ValidationCode.EXTRA_COLUMN}
    assert report.extra_columns == ("notes",)
    assert report.passed is False


def test_extra_column_allowed_when_configured() -> None:
    report = run(
        csv_bytes(R1_WITH_NOTE, header="url,title,publish_time,likes,notes"),
        make_schema(allow_extra_columns=True),
    )

    assert report.passed is True
    assert report.extra_columns == ("notes",)


# --- per-row semantics ------------------------------------------------------


def test_nullable_violation_is_row_level_error() -> None:
    report = run(csv_bytes("https://a.example/1,,2026-01-02,10"))

    assert error_codes(report) == {ValidationCode.NULL_VIOLATION}
    finding = first_error(report, ValidationCode.NULL_VIOLATION)
    assert finding.row == 1
    assert finding.column == "title"
    assert report.null_ratios["title"] == 1.0


def test_null_ratio_at_cap_passes() -> None:
    schema = make_schema(
        optional_columns=[
            {
                "name": "likes",
                "type": "integer",
                "nullable": True,
                "max_null_ratio": 0.5,
            },
        ],
    )
    report = run(csv_bytes(R1, R2), schema)  # 1 of 2 likes cells empty

    assert report.passed is True
    assert report.null_ratios["likes"] == 0.5


def test_null_ratio_above_cap_fails() -> None:
    schema = make_schema(
        optional_columns=[
            {
                "name": "likes",
                "type": "integer",
                "nullable": True,
                "max_null_ratio": 0.5,
            },
        ],
    )
    report = run(csv_bytes(R1, R2, R3.replace(",0", ",")), schema)  # 2 of 3 empty

    assert ValidationCode.NULL_RATIO_EXCEEDED in error_codes(report)
    finding = first_error(report, ValidationCode.NULL_RATIO_EXCEEDED)
    assert finding.column == "likes"
    assert report.null_ratios["likes"] == round(2 / 3, 6)


def test_row_shape_mismatch_is_row_level_error() -> None:
    report = run(csv_bytes("https://a.example/1,First"))

    assert ValidationCode.ROW_SHAPE_MISMATCH in error_codes(report)
    finding = first_error(report, ValidationCode.ROW_SHAPE_MISMATCH)
    assert finding.row == 1
    # Overlapping positions were still checked: no phantom type errors.
    assert report.type_error_counts == {
        "url": 0,
        "title": 0,
        "publish_time": 0,
        "likes": 0,
    }


def test_blank_lines_skipped() -> None:
    data = ("\n".join([HEADER, R1, "", R2, "   ", R3, ""]) + "\n").encode()
    report = run(data)

    assert report.passed is True
    assert report.row_count == 3


def test_unique_set_overflow_degrades_to_sampled_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(csv_validator, "UNIQUE_TRACKING_CAP", 3)
    rows = [
        "https://a/1,T,2026-01-02,",
        "https://a/2,T,2026-01-02,",
        "https://a/3,T,2026-01-02,",
        "https://a/4,T,2026-01-02,",  # cap reached: tracking degrades
        "https://a/5,T,2026-01-02,",  # unseen after freeze: missed
        "https://a/5,T,2026-01-02,",  # duplicate NOT detectable anymore
        "https://a/1,T,2026-01-02,",  # frozen-set member: detected
    ]
    report = run(csv_bytes(*rows))

    assert ValidationCode.UNIQUE_TRACKING_DEGRADED in warning_codes(report)
    degraded = next(
        w for w in report.warnings if w.code == ValidationCode.UNIQUE_TRACKING_DEGRADED
    )
    assert degraded.column == "url"
    # duplicate_counts is a lower bound once degraded: the a/5 repeat is
    # invisible, the a/1 repeat is still caught.
    assert report.duplicate_counts == {"url": 1}
    dup_errors = [f for f in report.errors if f.code == ValidationCode.DUPLICATE_VALUE]
    assert len(dup_errors) == 1
    assert dup_errors[0].row == 7


def test_findings_capped_but_counts_exact() -> None:
    rows = [f"https://a.example/{i},T,not-a-date," for i in range(150)]
    report = run(csv_bytes(*rows))

    assert len(report.errors) == 100
    assert ValidationCode.FINDINGS_TRUNCATED in warning_codes(report)
    assert report.type_error_counts["publish_time"] == 150


# --- shared type helpers ----------------------------------------------------


def test_integer_literals() -> None:
    accepted = ["0", "42", "+7", "-7", "007"]
    rejected = ["1.0", "1e3", "１", "1_0", "", " 42", "42 ", "nan", "1,000"]
    from app.modules.submissions.schema import ColumnType

    for value in accepted:
        assert check_cell_type(value, ColumnType.INTEGER), value
    for value in rejected:
        assert not check_cell_type(value, ColumnType.INTEGER), value


def test_number_literals() -> None:
    accepted = ["1", "1.", ".5", "-1.5E-3", "+0", "1e3", "3.14"]
    rejected = ["inf", "nan", "1,5", "1_0", "", ".", "1e", "0x10"]
    from app.modules.submissions.schema import ColumnType

    for value in accepted:
        assert check_cell_type(value, ColumnType.NUMBER), value
    for value in rejected:
        assert not check_cell_type(value, ColumnType.NUMBER), value


def test_boolean_literals() -> None:
    accepted = ["true", "FALSE", "Yes", "no", "1", "0", "True"]
    rejected = ["2", "on", "off", "", "true yes", "y"]
    from app.modules.submissions.schema import ColumnType

    for value in accepted:
        assert check_cell_type(value, ColumnType.BOOLEAN), value
    for value in rejected:
        assert not check_cell_type(value, ColumnType.BOOLEAN), value


def test_datetime_tolerant_set() -> None:
    accepted = [
        "2026-01-02",
        "2026-01-02T03:04:05",
        "2026-01-02 03:04:05",
        "2026-01-02T03:04:05Z",
        "2026-01-02T03:04:05+08:00",
        "2026-01-02T03:04:05.123456",
        "2026-01-02 03:04",
        "20260102",
    ]
    rejected = [
        "2026/01/02",
        "2026-1-2",
        "not-a-date",
        "2026-13-45",
        "",
        "2026-01-02T99:99:99",
    ]
    for value in accepted:
        assert is_valid_datetime(value), value
    for value in rejected:
        assert not is_valid_datetime(value), value


def test_string_type_accepts_any_value() -> None:
    from app.modules.submissions.schema import ColumnType

    assert check_cell_type("anything 42!?", ColumnType.STRING)
    assert check_cell_type("  ", ColumnType.STRING)


def test_number_and_boolean_columns_end_to_end() -> None:
    schema = SubmissionSchema.parse(
        {
            "required_columns": [
                {"name": "score", "type": "number"},
                {"name": "verified", "type": "boolean"},
            ],
        }
    )
    data = "score,verified\n1.5,true\nabc,2\n"
    report = run(data.encode(), schema)

    assert report.type_error_counts == {"score": 1, "verified": 1}
    assert report.row_count == 2


# --- limits / report surface ------------------------------------------------


def test_validation_limits_defaults() -> None:
    limits = ValidationLimits()

    assert limits.max_columns == 1024
    assert limits.max_cell_length == 65_536
    assert limits.row_cap == MAX_ROWS_CEILING
    assert limits.timeout_seconds == 30.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_columns": 0},
        {"max_cell_length": 0},
        {"row_cap": 0},
        {"timeout_seconds": 0.0},
        {"timeout_seconds": -1.0},
        {"max_columns": True},
        {"timeout_seconds": True},
        {"row_cap": "many"},
    ],
)
def test_validation_limits_rejects_non_positive(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ValidationLimits(**kwargs)


def test_validation_limits_frozen() -> None:
    limits = ValidationLimits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.row_cap = 1  # type: ignore[misc]


def test_report_frozen_with_passed_property() -> None:
    report = run(csv_bytes(R1, R2, R3))
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.row_count = 99  # type: ignore[misc]
    assert report.passed is True
    assert run(csv_bytes()).passed is False


def test_validation_code_values_equal_names() -> None:
    for code in ValidationCode:
        assert code.value == code.name


def test_duration_ms_from_fake_clock() -> None:
    report = run(csv_bytes(R1, R2, R3), clock=FakeClock(step=0.0))

    assert report.duration_ms == 0.0
