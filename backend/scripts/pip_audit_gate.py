# backend/scripts/pip_audit_gate.py
"""pip-audit CI gate with a review-dated exemption file.

Fails the build on any dependency vulnerability NOT explicitly
exempted. Exemptions live in ``pip-audit-exemptions.json``:

    {
      "PYSEC-2025-1234": {
        "reason": "dev-only transitive; no runtime exposure",
        "review_by": "2026-12-31"
      }
    }

Discipline: every exemption carries a free-text reason and a review
deadline; an expired ``review_by`` is itself a failure (exemptions are
rented, not owned). pip-audit has no native severity filter — it treats
every finding equally — so this gate is STRICTER than "high/critical
only": anything unfixed and unexempted is red, and accepted lows must
be explicitly rented above.

Exit codes: 0 clean / 1 vulnerabilities or expired exemptions /
2 environment error.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_EXEMPTIONS_PATH = _BACKEND_ROOT / "pip-audit-exemptions.json"
_DATE_FORMAT = "%Y-%m-%d"


def _load_exemptions() -> tuple[dict[str, dict], list[str]]:
    if not _EXEMPTIONS_PATH.exists():
        return {}, []
    raw = json.loads(_EXEMPTIONS_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []
    today = dt.date.today()
    for vuln_id, entry in raw.items():
        try:
            review_by = dt.datetime.strptime(entry["review_by"], _DATE_FORMAT).date()
        except (KeyError, ValueError):
            problems.append(
                f"{vuln_id}: malformed exemption (needs reason + review_by YYYY-MM-DD)"
            )
            continue
        if review_by < today:
            problems.append(
                f"{vuln_id}: exemption expired {review_by} — renew with a fresh "
                "reason and review date, or fix the dependency"
            )
        if not str(entry.get("reason", "")).strip():
            problems.append(f"{vuln_id}: exemption carries no reason")
    return raw, problems


def main() -> int:
    exemptions, problems = _load_exemptions()
    if problems:
        print("exemption file problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    command = [
        "uv",
        "run",
        "pip-audit",
        "--skip-editable",
        # Local project (.) is audited by its locked dependencies;
        # --skip-editable keeps the audit on the lockfile's packages.
    ]
    for vuln_id in exemptions:
        command.extend(["--ignore-vuln", vuln_id])

    print(f"$ {' '.join(command)}")
    result = subprocess.run(command, cwd=_BACKEND_ROOT)

    if result.returncode != 0:
        print(
            "\npip-audit failed. To accept a finding, add it to "
            "pip-audit-exemptions.json with a reason and a review date "
            "(rented, not owned)."
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
