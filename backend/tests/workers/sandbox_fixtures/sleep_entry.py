# backend/tests/workers/sandbox_fixtures/sleep_entry.py
"""TEST-ONLY sandbox fixture entry: consumes the stdin request exactly
like the real worker entry, then sleeps far past any test wall
deadline. The parent's hard deadline must kill it and classify the
run as VALIDATION_TIMED_OUT while the parent process survives.
"""

from __future__ import annotations

import json
import sys
import time


def main() -> int:
    json.loads(sys.stdin.read())  # drain the request like the real entry
    time.sleep(30.0)
    print(json.dumps({"detected_type": None, "report": None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
