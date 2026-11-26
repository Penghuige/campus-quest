import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { listPublicAchievements } from "../features/innovation/reviewApi";
import { getPublicProjects, getPublicRecruitments } from "../features/innovation/recruitmentApi";
import { getPublicTalents } from "../features/innovation/talentApi";
import { recordLogout } from "../lib/accessToken";

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
  recordLogout();
});

test("discovery previews use bounded public list queries", async () => {
  const paths: string[] = [];
  globalThis.fetch = (async (input) => {
    paths.push(String(input));
    return Response.json({ items: [], total: 0, limit: 3, offset: 0 });
  }) as typeof fetch;

  await Promise.all([
    getPublicRecruitments(new URLSearchParams({ limit: "3", offset: "0" })),
    getPublicProjects(new URLSearchParams({ limit: "3", offset: "0" })),
    getPublicTalents(new URLSearchParams({ limit: "3", offset: "0" })),
    listPublicAchievements(0, 3),
  ]);

  assert.deepEqual(paths.sort(), [
    "/api/v1/ie/achievements?limit=3&offset=0",
    "/api/v1/ie/projects?limit=3&offset=0",
    "/api/v1/ie/recruitments?limit=3&offset=0",
    "/api/v1/ie/talents?limit=3&offset=0",
  ]);
  assert.ok(paths.every((path) => !path.includes("/me/")));
});

test("achievement pagination keeps its existing default page size", async () => {
  let path = "";
  globalThis.fetch = (async (input) => {
    path = String(input);
    return Response.json({ items: [], total: 0, limit: 20, offset: 40 });
  }) as typeof fetch;

  await listPublicAchievements(40);
  assert.equal(path, "/api/v1/ie/achievements?limit=20&offset=40");
});
