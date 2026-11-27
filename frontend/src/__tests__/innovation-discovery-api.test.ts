import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { listPublicAchievements } from "../features/innovation/reviewApi";
import { getPublicProjects, getPublicRecruitments } from "../features/innovation/recruitmentApi";
import {
  completeTalentFile,
  createTalentFileIntent,
  downloadOwnTalentFile,
  downloadPublicTalentFile,
  getPublicTalent,
  getPublicTalents,
  getTalentContacts,
  getTalentFiles,
  getTalentProfile,
  getTalentTags,
  hideTalentProfile,
  publishTalentProfile,
  putTalentFile,
  removeTalentFile,
  revokeTalentContact,
  revokeTalentFile,
  saveTalentProfile,
  suggestTalentTag,
} from "../features/innovation/talentApi";
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

test("talent API endpoints map paths, methods, and upload status rules", async () => {
  const calls: Array<{ url: string; method: string }> = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ url, method });
    if (url.includes("/content")) {
      return new Response(new Blob(["ok"]), { status: 200 });
    }
    return Response.json({});
  }) as typeof fetch;

  await getTalentProfile();
  await saveTalentProfile({} as never);
  await publishTalentProfile({ expected_version: 1 } as never);
  await hideTalentProfile({ expected_version: 1 } as never);
  await revokeTalentContact({ expected_version: 1 } as never);
  await getTalentTags();
  await suggestTalentTag({ name: "AI" } as never);
  await getTalentFiles();
  await createTalentFileIntent({} as never);
  await completeTalentFile("f 1");
  await removeTalentFile("f 1", { expected_version: 1 } as never);
  await revokeTalentFile("f 1", { expected_version: 1 } as never);
  await downloadOwnTalentFile("f 1");
  await getPublicTalent("t 1");
  await getTalentContacts("t 1", 2);
  await downloadPublicTalentFile("t 1", "f 1", 2);

  assert.equal(calls.length, 16);
  assert.equal(calls[0]?.url, "/api/v1/ie/me/talent-profile");
  assert.equal(calls[1]?.method, "PUT");
  assert.equal(calls[9]?.url, "/api/v1/ie/me/talent-profile/files/f%201/complete");
  assert.equal(
    calls[15]?.url,
    "/api/v1/ie/talents/t%201/files/f%201/content?expected_public_version=2",
  );

  // putTalentFile accepts 2xx and 412 (possible existing object), rejects 500
  const grant = { upload_url: "https://storage.local/put", client_headers: { "x-test": "1" } } as never;
  const file = new File(["abc"], "a.txt", { type: "text/plain" });
  globalThis.fetch = (async () => new Response(null, { status: 412 })) as typeof fetch;
  await putTalentFile(grant, file);

  globalThis.fetch = (async () => new Response(null, { status: 500 })) as typeof fetch;
  await assert.rejects(() => putTalentFile(grant, file), /文件上传未完成/);
});
