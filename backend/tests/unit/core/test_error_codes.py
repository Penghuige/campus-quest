# backend/tests/unit/core/test_error_codes.py
"""The importable registry must mirror the interfaces.md tables exactly.

The frozen lists below are typed by hand from the tables in
docs/architecture/interfaces.md. If either the enum or the document drifts,
these tests fail loudly instead of letting a new code slip in unregistered.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.core.error_codes import ErrorCode

_BACKEND_DIR = Path(__file__).resolve().parents[3]
_INTERFACES_MD = (
    Path(__file__).resolve().parents[4] / "docs" / "architecture" / "interfaces.md"
)

FROZEN_BUSINESS_CODES = (
    "VALIDATION_ERROR",
    "AUTHENTICATION_REQUIRED",
    "PERMISSION_DENIED",
    "ACCOUNT_NOT_ACTIVE",
    "STUDENT_NOT_WHITELISTED",
    "PHONE_ALREADY_BOUND",
    "TASK_NOT_CLAIMABLE",
    "NO_ASSIGNMENT_AVAILABLE",
    "ASSIGNMENT_LIMIT_REACHED",
    "TASK_ACTIVE_CLAIM_EXISTS",
    "CLAIM_CUTOFF_REACHED",
    "CLAIM_NOT_SUBMITTABLE",
    "SUBMISSION_WINDOW_CLOSED",
    "FILE_TOO_LARGE",
    "FILE_TYPE_NOT_ALLOWED",
    "SUBMISSION_VALIDATION_FAILED",
    "ALREADY_REVIEWED",
    "INSUFFICIENT_POINTS",
    "REWARD_OUT_OF_STOCK",
    "REDEMPTION_LIMIT_REACHED",
    "RATING_NOT_ELIGIBLE",
)

FROZEN_SYSTEM_CODES = (
    "INTERNAL_ERROR",
    "NOT_FOUND",
    "METHOD_NOT_ALLOWED",
    "HTTP_ERROR",
)


def test_enum_membership_matches_frozen_registry() -> None:
    assert [code.value for code in ErrorCode] == [
        *FROZEN_BUSINESS_CODES,
        *FROZEN_SYSTEM_CODES,
    ]


def test_member_value_equals_member_name() -> None:
    # interfaces.md: every enum member persists `value == member name`.
    for code in ErrorCode:
        assert code.value == code.name


def _parse_code_tables() -> tuple[list[str], list[str]]:
    """Return (business_codes, system_codes) parsed from interfaces.md."""
    tables: list[list[str]] = []
    current: list[str] | None = None
    for line in _INTERFACES_MD.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("| `") and stripped.endswith("|"):
            code = stripped.split("|")[1].strip().strip("`")
            if current is not None:
                current.append(code)
        elif stripped.startswith("|"):
            if stripped.replace(" ", "").startswith("|Code|"):
                current = []
                tables.append(current)
        else:
            current = None
    assert len(tables) == 2, f"expected 2 code tables in {_INTERFACES_MD}"
    return tables[0], tables[1]


def test_interfaces_md_tables_match_the_enum() -> None:
    # Doc drift fails loudly: the registry document is the authority, so the
    # enum and the markdown tables must carry the exact same code strings.
    business_codes, system_codes = _parse_code_tables()
    assert business_codes == [*FROZEN_BUSINESS_CODES]
    assert system_codes == [*FROZEN_SYSTEM_CODES]
    assert business_codes + system_codes == [code.value for code in ErrorCode]


def test_registry_module_imports_no_web_framework() -> None:
    # Domain modules and workers import the constants; the registry must stay
    # dependency-free so that never pulls FastAPI (or any web stack) in.
    web_roots = "{'fastapi', 'starlette', 'celery', 'sqlalchemy', 'redis'}"
    code = "\n".join(
        [
            "import sys",
            "import app.core.error_codes",
            f"web = sorted(m for m in sys.modules if m.split('.')[0] in {web_roots})",
            "print('LEAKS=' + ','.join(web))",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "LEAKS="
