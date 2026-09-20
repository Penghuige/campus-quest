# backend/app/modules/submissions/validators/worker_entry.py
"""The sandboxed validator child (plan 04 task 7; spec §33.3).

This module is executed as ``python -m
app.modules.submissions.validators.worker_entry <path>`` by
``validation_runner.ValidatorSandbox`` — never imported by the
application. It imports ONLY the validator stack (no database, no
services, no clock injection: the parent's hard limits are the
authority on time and memory here).

Wire protocol (stdout is exactly one JSON object, nothing else):

- argv[1]: absolute path of the downloaded upload;
- stdin: the JSON request
  ``{"schema": <raw DSL mapping>, "preview": {...}|absent,
  "limits": {...}|absent}``;
- stdout: ``{"detected_type": "CSV"|"XLSX"|"SQLITE"|null,
  "report": <§12.4 JSON>|null}``.

``detected_type`` null means the content matched none of the three
uploadable types — detection itself runs in this child (inside the
resource limits) precisely because a hostile archive can turn the
manifest listing into a memory bomb; the parent then fails the
submission with ``FILE_TYPE_NOT_ALLOWED`` without any parser having
run.

Failure taxonomy inside the child: an ``OSError`` escaping a
validator's execution (the T5 carry shape: openpyxl answers
``OSError('File contains no valid workbook part')`` on a
manifest-valid archive with no workbook) is CONVERTED HERE into a
structured exit-0 ``MALFORMED_*`` report — by the time this child
runs, the file is a local temp the parent already downloaded and
detection already read, so an I/O-level failure during parsing is a
property of the untrusted content, and a terminal verdict is the
honest answer (the parent's crash class cannot promise a retry the
terminal replay path would never deliver). The crash class is
reserved for actual crashes: any OTHER uncaught exception leaves a
stderr traceback and a non-zero exit, which the parent answers with
``VALIDATION_WORKER_CRASHED``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import SubmissionSchema
from app.modules.submissions.validators.common import (
    PreviewSpec,
    ValidationCode,
    ValidationLimits,
    ValidationReportBuilder,
    report_to_json,
)
from app.modules.submissions.validators.csv_validator import (
    PARSER_VERSION as CSV_PARSER_VERSION,
)
from app.modules.submissions.validators.csv_validator import validate_csv
from app.modules.submissions.validators.detect import detect_file_type
from app.modules.submissions.validators.sqlite_validator import (
    PARSER_VERSION as SQLITE_PARSER_VERSION,
)
from app.modules.submissions.validators.sqlite_validator import validate_sqlite
from app.modules.submissions.validators.xlsx_validator import (
    PARSER_VERSION as XLSX_PARSER_VERSION,
)
from app.modules.submissions.validators.xlsx_validator import validate_xlsx

#: Per detected type: the parser whose verdict it is, and the message
#: of the malformed-file outcome an escaping ``OSError`` converts into
#: (mirrors each validator's own unparseable wording).
_MALFORMED_BY_TYPE: dict[FileType, tuple[str, ValidationCode, str]] = {
    FileType.CSV: (
        CSV_PARSER_VERSION,
        ValidationCode.MALFORMED_CSV,
        "文件无法读取或结构损坏，无法完成 CSV 解析",
    ),
    FileType.XLSX: (
        XLSX_PARSER_VERSION,
        ValidationCode.MALFORMED_XLSX,
        "工作簿无法解析（结构损坏或不是有效的 XLSX）",
    ),
    FileType.SQLITE: (
        SQLITE_PARSER_VERSION,
        ValidationCode.MALFORMED_SQLITE,
        "数据库无法只读打开（文件损坏或不是有效数据库）",
    ),
}


def _emit(detected: FileType | None, report: Any) -> None:
    print(
        json.dumps(
            {
                "detected_type": detected.value if detected is not None else None,
                "report": report_to_json(report) if report is not None else None,
            }
        )
    )


def main() -> int:
    request: dict[str, Any] = json.loads(sys.stdin.read())
    path = sys.argv[1]
    schema = SubmissionSchema.parse(request["schema"])
    detected = detect_file_type(path)
    if detected is None:
        _emit(None, None)
        return 0

    preview = PreviewSpec(**request["preview"]) if "preview" in request else None
    limits = ValidationLimits(**request["limits"]) if "limits" in request else None
    try:
        match detected:
            case FileType.CSV:
                with open(path, "rb") as stream:
                    report = validate_csv(stream, schema, limits, preview=preview)
            case FileType.XLSX:
                report = validate_xlsx(path, schema, limits, preview=preview)
            case FileType.SQLITE:
                report = validate_sqlite(path, schema, limits, preview=preview)
    except OSError:
        # See the module docstring: the file is local, downloaded, and
        # already read by detection — an I/O-level failure of the parse
        # is a malformed-content verdict, not a worker crash and not a
        # retryable infrastructure class (the parent's download is the
        # only retry path, and it already succeeded).
        parser_version, code, message = _MALFORMED_BY_TYPE[detected]
        builder = ValidationReportBuilder(
            parser_version=parser_version, file_type=detected
        )
        builder.add_error(code, message)
        report = builder.build(
            row_count=0,
            detected_columns=[],
            missing_required_columns=[],
            extra_columns=[],
            type_error_counts={},
            null_ratios={},
            duplicate_counts={},
            duration_ms=0.0,
        )
    _emit(detected, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
