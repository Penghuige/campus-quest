# backend/tests/workers/sandbox_fixtures/hog_entry.py
"""TEST-ONLY sandbox fixture entry: consumes the stdin request, then
allocates memory until the child's RLIMIT_AS turns the next allocation
into MemoryError (256 MB target under a 128 MB limit in the test).
The parent must classify the kill as VALIDATION_WORKER_CRASHED and
keep running — the T5 parked ruling's memory cap in miniature.
"""

from __future__ import annotations

import json
import sys

_TARGET_BYTES = 256 * 1024 * 1024
_CHUNK = 16 * 1024 * 1024


def main() -> int:
    json.loads(sys.stdin.read())
    block = bytearray()
    while len(block) < _TARGET_BYTES:
        block += b"\x00" * _CHUNK
    print(json.dumps({"detected_type": None, "report": None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
