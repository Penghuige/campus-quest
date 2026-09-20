# backend/tests/workers/sandbox_fixtures/garbage_entry.py
"""TEST-ONLY sandbox fixture entry: consumes the stdin request, then
exits non-zero after writing garbage to stdout — the uncontrolled-
crash shape (traceback on stderr, no protocol answer). The parent must
classify it as VALIDATION_WORKER_CRASHED, never hang, and never try to
interpret the stdout bytes as a report.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    json.loads(sys.stdin.read())
    print("half-written answer, then the process derailed")
    sys.stderr.write("fixture: simulated parser crash\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
