/**
 * The directed-isolation helpers: test-owned rows through the SAME
 * factories the world seeding uses — each caller establishes exactly
 * the state its chain needs and removes it afterwards; nothing here
 * touches another test's rows or any global shape.
 *
 * Mechanically this is `runWorldAction`'s sibling: a focused
 * `uv run python -c` against the pinned test-stack env (backendEnv),
 * driving `tests.e2e.factories` directly — seed_student /
 * clean_world / the notification fixtures. No browser can reach these
 * tables, exactly the world-action boundary.
 */
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";

import { BACKEND_DIR, backendEnv } from "./global-setup";

/** The shared python preamble: path setup + a NullPool session
 * factory on the pinned test stack (browser_world's own recipe). */
const PREAMBLE = `
import asyncio, json, sys
sys.path.insert(0, ".")
from uuid import UUID as _UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.core.config import get_settings
from tests.e2e.factories import (
    clean_world,
    create_unread_notification,
    delete_notification,
    seed_student,
)
engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
factory = async_sessionmaker(engine, expire_on_commit=False)
async def _main():
`;

function runFixture(body: string): string {
  const script = `${PREAMBLE}${body}\nasyncio.run(_main())`;
  const result = spawnSync("uv", ["run", "python", "-c", script], {
    cwd: BACKEND_DIR,
    env: backendEnv,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(
      `isolation fixture failed (${result.status}): ${result.stderr}`,
    );
  }
  return result.stdout.trim();
}

/** One UNREAD inbox row for the user — the mark-read chain's OWN
 * notification (a run-unique marker title), however many other rows
 * or consumers the battery left behind. Returns the row id. */
export function createUnreadNotification(
  userId: string,
  marker: string,
): string {
  return runFixture(
    `    nid = await create_unread_notification(
        factory, user_id=_UUID("${userId}"), title=${JSON.stringify(marker)}
    )
    print(nid)`,
  );
}

/** Remove the fixture row (deliveries first — the FK order). */
export function deleteNotification(notificationId: string): void {
  runFixture(
    `    await delete_notification(factory, _UUID("${notificationId}"))`,
  );
}

/** One dedicated ACTIVE student with a bound phone and NO wallet
 * events — spendable is deterministically 0, below every reward. The
 * run marker is a fresh uuid's hex (seed_student folds run[:11]/run[:8]
 * as HEX — anything else raises before the row exists); the chain owns
 * the whole lifecycle, `deleteUserCascade` removes it. */
export function spawnDedicatedStudent(): {
  id: string;
  username: string;
  password: string;
} {
  const marker = randomUUID().replaceAll("-", "");
  const raw = runFixture(
    `    fixture = await seed_student(factory, run="${marker}")
    print(json.dumps({"id": str(fixture.user_id), "username": fixture.username, "password": fixture.password}))`,
  );
  return JSON.parse(raw) as { id: string; username: string; password: string };
}

/** Remove EVERYTHING of the dedicated user (clean_world's scoped
 * cascade — sessions, wallet, ledger, notifications, the user row). */
export function deleteUserCascade(userId: string): void {
  runFixture(
    `    await clean_world(factory, user_ids=[_UUID("${userId}")])`,
  );
}
