"""Seed the load-test world (CLI shell; logic in backend/loadtest/seed.py).

Usage (from backend/, with the stack env exported — or via
`make loadtest-seed`):

  uv run python scripts/seed_load_world.py \
      --claim-slots 50 --race-users 300 --race-slots 60 \
      --deadline-in-minutes 20 --out-dir ../backend/loadtest/runs/<ts>

Writes world.json (task ids + per-user tokens) into --out-dir. Tokens are
minted through the same session-issuance path as login, never via HTTP
(the per-IP login limiter must not see the suite). Not for production:
get_settings' DATABASE_URL must point at the local compose stack.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loadtest.seed import LoadWorldExistsError, seed_world  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claim-slots",
        type=int,
        required=True,
        help="S1 pre-seeded claims (submitter users)",
    )
    parser.add_argument(
        "--race-users", type=int, required=True, help="S2 racers (claim_id stays null)"
    )
    parser.add_argument(
        "--race-slots", type=int, default=60, help="S2 available slots (default 60)"
    )
    parser.add_argument(
        "--students",
        type=int,
        default=None,
        help="total students (default: claim-slots + race-users)",
    )
    parser.add_argument(
        "--deadline-in-minutes",
        type=int,
        default=20,
        help="S1 run window from now (default 20)",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--force", action="store_true", help="replace an existing load world"
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    try:
        world = await seed_world(
            claim_slots=args.claim_slots,
            race_users=args.race_users,
            race_slots=args.race_slots,
            students=args.students,
            deadline_in_minutes=args.deadline_in_minutes,
            out_dir=args.out_dir,
            force=args.force,
        )
    except LoadWorldExistsError as error:
        print(f"seed_load_world: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    deadline = datetime.fromisoformat(world["deadline_at"])
    print(
        f"seed_load_world: task {world['task_id']} — "
        f"{world['slots_total']} slots, {len(world['users'])} users, "
        f"deadline {deadline.astimezone(UTC).isoformat()}"
    )
    print(f"world.json -> {args.out_dir / 'world.json'}")


if __name__ == "__main__":
    asyncio.run(main())
