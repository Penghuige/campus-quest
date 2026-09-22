# backend/tests/unit/submissions/test_report_preview.py
"""Unit tests for the §12.4 report preview rows and the report JSON
wire format (plan 04 task 7 steps 1/3; spec §12.4: 前 N 行安全预览 —
plain data only, bounded count, truncated values; backend-engineering
§14 safe preview truncation).

The preview is collected INSIDE the format validators (they already
stream the rows; a second preview pass would duplicate parsing and its
bounds): each validator hands its first N data rows to the shared
report builder, which truncates every cell to the configured length.
The JSON round-trip (``report_to_json`` / ``report_from_json``) is the
child->parent wire format of the sandboxed validator and the shape
persisted into ``submissions.validation_report`` /
``submission_validations.report`` — it must carry the full §12.4 field
list plus ``preview_rows`` and nothing else (no object keys, no parser
internals).

Pure unit work: in-memory/temp-file fixtures, no database.
"""

from __future__ import annotations

import sqlite3
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import SubmissionSchema
from app.modules.submissions.validators.common import (
    PreviewSpec,
    ValidationCode,
    ValidationFinding,
    ValidationReport,
    ValidationReportBuilder,
    report_from_json,
    report_to_json,
)
from app.modules.submissions.validators.csv_validator import validate_csv
from app.modules.submissions.validators.sqlite_validator import validate_sqlite
from app.modules.submissions.validators.xlsx_validator import validate_xlsx

_SCHEMA = SubmissionSchema.parse(
    {
        "required_columns": [
            {"name": "url", "type": "string", "unique": True},
            {"name": "title", "type": "string"},
        ]
    }
)


def _csv_report(content: bytes, preview: PreviewSpec | None):
    return validate_csv(BytesIO(content), _SCHEMA, preview=preview)


# --- preview collection through each validator -------------------------------------


def test_csv_preview_capped_at_configured_row_count() -> None:
    preview = PreviewSpec(max_rows=10, max_value_length=200)
    rows = "".join(f"https://r{i}.com,t{i}\n" for i in range(25))
    report = _csv_report(("url,title\n" + rows).encode(), preview)
    assert len(report.preview_rows) == 10
    assert report.preview_rows[0] == ("https://r0.com", "t0")
    assert report.preview_rows[9] == ("https://r9.com", "t9")
    assert report.row_count == 25  # the count stays exact


def test_csv_preview_values_truncated_to_configured_length() -> None:
    preview = PreviewSpec(max_rows=3, max_value_length=50)
    long_value = "x" * 500
    report = _csv_report(
        ("url,title\n" + f"https://a.com,{long_value}\n").encode(), preview
    )
    (cell,) = report.preview_rows[0][1:]
    assert cell == "x" * 50 + "…"
    assert len(cell) == 51


def test_csv_preview_defaults_to_none_when_spec_omitted() -> None:
    report = _csv_report(b"url,title\nhttps://a.com,t\n", None)
    assert report.preview_rows == ()


def test_csv_preview_omits_header_and_blank_lines() -> None:
    preview = PreviewSpec(max_rows=10, max_value_length=200)
    content = b"url,title\n\nhttps://a.com,t\n\n\nhttps://b.com,t2\n"
    report = _csv_report(content, preview)
    assert report.preview_rows == (("https://a.com", "t"), ("https://b.com", "t2"))


def test_csv_preview_rows_stop_collecting_after_the_cap() -> None:
    # Bounded memory: rows beyond the cap are streamed past, not stored.
    preview = PreviewSpec(max_rows=1, max_value_length=200)
    rows = "".join(f"https://r{i}.com,t{i}\n" for i in range(500))
    report = _csv_report(("url,title\n" + rows).encode(), preview)
    assert report.preview_rows == (("https://r0.com", "t0"),)


def test_xlsx_preview_capped_and_truncated(tmp_path: Path) -> None:
    path = tmp_path / "sheet.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(("url", "title"))
    for index in range(15):
        sheet.append((f"https://r{index}.com", "y" * 300))
    workbook.save(path)

    preview = PreviewSpec(max_rows=10, max_value_length=200)
    report = validate_xlsx(path, _SCHEMA, preview=preview)
    assert len(report.preview_rows) == 10
    assert report.preview_rows[0][0] == "https://r0.com"
    assert report.preview_rows[0][1] == "y" * 200 + "…"
    assert report.row_count == 15


def test_sqlite_preview_renders_storage_classes_as_plain_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE data (url TEXT, title TEXT, likes INTEGER)")
        connection.executemany(
            "INSERT INTO data VALUES (?, ?, ?)",
            [
                ("https://a.com", "t1", 10),
                ("https://b.com", "t2", None),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    schema = SubmissionSchema.parse(
        {
            "source_selector": {"table_name": "data"},
            "required_columns": [
                {"name": "url", "type": "string", "unique": True},
                {"name": "title", "type": "string"},
                {"name": "likes", "type": "integer", "nullable": True},
            ],
        }
    )
    preview = PreviewSpec(max_rows=10, max_value_length=200)
    report = validate_sqlite(path, schema, preview=preview)
    assert report.preview_rows == (
        ("https://a.com", "t1", "10"),
        ("https://b.com", "t2", ""),
    )


# --- the JSON wire format -----------------------------------------------------------


def _sample_report() -> ValidationReport:
    builder = ValidationReportBuilder(
        parser_version="csv-1",
        file_type=FileType.CSV,
        preview=PreviewSpec(max_rows=10, max_value_length=200),
    )
    builder.add_preview_row(("https://a.com", "t"))
    builder.add_error(
        ValidationCode.TYPE_ERROR,
        "第 2 行列 likes 的值不符合 integer 类型",
        row=2,
        column="likes",
        value="many",
    )
    builder.add_warning(ValidationCode.ROW_COUNT_TRUNCATED, "行数统计提前停止")
    return builder.build(
        row_count=7,
        detected_columns=["url", "title", "likes"],
        missing_required_columns=[],
        extra_columns=["extra"],
        type_error_counts={"likes": 1},
        null_ratios={"url": 0.0, "title": 0.1, "likes": 0.0},
        duplicate_counts={"url": 0},
        duration_ms=12.5,
    )


def test_report_to_json_carries_the_full_124_field_list_plus_preview() -> None:
    data = report_to_json(_sample_report())
    assert set(data) == {
        "parser_version",
        "file_type",
        "row_count",
        "detected_columns",
        "missing_required_columns",
        "extra_columns",
        "type_error_counts",
        "null_ratios",
        "duplicate_counts",
        "warnings",
        "errors",
        "duration_ms",
        "preview_rows",
    }
    assert data["parser_version"] == "csv-1"
    assert data["file_type"] == "CSV"
    assert data["errors"] == [
        {
            "code": "TYPE_ERROR",
            "message": "第 2 行列 likes 的值不符合 integer 类型",
            "row": 2,
            "column": "likes",
            "value": "many",
        }
    ]
    assert data["warnings"][0]["code"] == "ROW_COUNT_TRUNCATED"
    assert data["preview_rows"] == [["https://a.com", "t"]]
    # No object keys, no parser internals, no storage paths anywhere.
    serialized = repr(data)
    for forbidden in ("object_key", "submissions/", "path", "argv"):
        assert forbidden not in serialized


def test_report_json_round_trip_is_lossless() -> None:
    report = _sample_report()
    data = report_to_json(report)
    restored = report_from_json(data)
    assert restored == report


def test_report_from_json_is_json_load_compatible() -> None:
    # The child prints this through json.dumps; the parent parses it
    # with json.loads before report_from_json sees it.
    import json

    restored = report_from_json(
        json.loads(json.dumps(report_to_json(_sample_report())))
    )
    assert restored.errors[0] == ValidationFinding(
        ValidationCode.TYPE_ERROR,
        "第 2 行列 likes 的值不符合 integer 类型",
        row=2,
        column="likes",
        value="many",
    )
    assert restored.preview_rows == (("https://a.com", "t"),)
