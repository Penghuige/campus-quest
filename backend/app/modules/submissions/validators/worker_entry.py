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
run. An uncaught exception here (stderr traceback, non-zero exit) is
the crash taxonomy: the parent answers ``VALIDATION_WORKER_CRASHED``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from app.modules.submissions.enums import FileType
from app.modules.submissions.schema import SubmissionSchema
from app.modules.submissions.validators.common import (
    PreviewSpec,
    ValidationLimits,
    report_to_json,
)
from app.modules.submissions.validators.csv_validator import validate_csv
from app.modules.submissions.validators.detect import detect_file_type
from app.modules.submissions.validators.sqlite_validator import validate_sqlite
from app.modules.submissions.validators.xlsx_validator import validate_xlsx


def main() -> int:
    request: dict[str, Any] = json.loads(sys.stdin.read())
    path = sys.argv[1]
    schema = SubmissionSchema.parse(request["schema"])
    detected = detect_file_type(path)
    if detected is None:
        print(json.dumps({"detected_type": None, "report": None}))
        return 0

    preview = PreviewSpec(**request["preview"]) if "preview" in request else None
    limits = ValidationLimits(**request["limits"]) if "limits" in request else None
    match detected:
        case FileType.CSV:
            with open(path, "rb") as stream:
                report = validate_csv(stream, schema, limits, preview=preview)
        case FileType.XLSX:
            report = validate_xlsx(path, schema, limits, preview=preview)
        case FileType.SQLITE:
            report = validate_sqlite(path, schema, limits, preview=preview)
    print(
        json.dumps({"detected_type": detected.value, "report": report_to_json(report)})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
