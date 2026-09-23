/**
 * CampusQuest browser-e2e global teardown — Plan 10 Task 2 (E2).
 *
 * The seed's mirror: S3 objects, seeded rows, the run's board members,
 * and the broker backlog restore — all scoped to THIS run's world file
 * (browser_world.py clean). Runs after every worker exits, pass or
 * fail; a missing world file means the seed never landed, which is a
 * no-op rather than an error.
 */
import { existsSync } from "node:fs";

import { runWorldAction } from "./global-setup";

export default function globalTeardown(): void {
  const worldFile = process.env.CQ_E2E_WORLD_FILE;
  if (worldFile === undefined || !existsSync(worldFile)) {
    return;
  }
  try {
    runWorldAction("clean", worldFile);
  } catch (error) {
    // Never mask the run's own report, but NEVER clean silently-fail
    // either: an orphaned world pollutes every later suite against the
    // shared test stack.
    console.error("[global-teardown] world cleanup FAILED:", error);
    process.exitCode = process.exitCode ?? 1;
  }
}
