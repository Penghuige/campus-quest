import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { createAchievementDraft, getAchievementDraft, listAchievementDrafts, updateAchievementDraft } from "../features/innovation/achievementApi";
import { achievementFields, describeAchievementError, normalizeAchievementFields, validateAchievementFields } from "../features/innovation/achievementForm";
import { recordLogout } from "../lib/accessToken";
import { ApiError } from "../lib/errors";

const fields = { title: " 原型 ", description: " 进展 ", work_url: " https://example.com/work ", award_text: " 暂无 " };
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("achievement forms trim four content fields, preserve partial drafts and enforce Unicode limits", () => {
  assert.deepEqual(normalizeAchievementFields(fields), { title: "原型", description: "进展", work_url: "https://example.com/work", award_text: "暂无" });
  assert.deepEqual(achievementFields({ ...fields, approved: true }), fields);
  assert.deepEqual(validateAchievementFields({ title: "成果", description: "", work_url: "", award_text: "" }), {});
  assert.ok(validateAchievementFields({ ...fields, title: " " }).title);
  for (const [key, limit] of Object.entries({ title: 120, description: 4000, award_text: 1000 })) {
    assert.deepEqual(validateAchievementFields({ ...fields, [key]: "🌱".repeat(limit) }), {});
    assert.ok(validateAchievementFields({ ...fields, [key]: "🌱".repeat(limit + 1) })[key as keyof typeof fields]);
  }
  for (const value of ["javascript:alert(1)", "https:example.com", "http:///example.com", "//example.com", "https://user:password@example.com", "https://example.com/a b", "https://example.com/\nsecret", "https://example.com/" + "x".repeat(2000)]) {
    assert.ok(validateAchievementFields({ ...fields, work_url: value }).work_url, value);
  }
});

test("private child API carries explicit parent scope, create key and expected version", async () => {
  const seen: unknown[] = [];
  globalThis.fetch = (async (input, init) => {
    seen.push([String(input), init?.method ?? "GET", init?.body ? JSON.parse(String(init.body)) : null]);
    return new Response("{}");
  }) as typeof fetch;
  await listAchievementDrafts("parent/id", { limit: 20, offset: 40 });
  await getAchievementDraft("parent/id", "child/id");
  await createAchievementDraft("parent/id", fields, "stable-request-key");
  await createAchievementDraft("parent/id", fields, "stable-request-key");
  await updateAchievementDraft("parent/id", "child/id", fields, 3);
  const base = "/api/v1/ie/me/project-drafts/parent%2Fid/achievements";
  assert.deepEqual(seen, [
    [`${base}?limit=20&offset=40`, "GET", null],
    [`${base}/child%2Fid`, "GET", null],
    [base, "POST", { ...fields, request_id: "stable-request-key" }],
    [base, "POST", { ...fields, request_id: "stable-request-key" }],
    [`${base}/child%2Fid`, "PATCH", { ...fields, version: 3 }],
  ]);
});

test("unknown create uses the frozen request, while unknown edits and version conflicts require rereading", () => {
  const conflict = new ApiError({ code: "CONFLICT", status: 409, message: "raw" });
  assert.equal(describeAchievementError(conflict, true).reload, true);
  assert.equal(describeAchievementError(new TypeError("offline"), true).reload, true);
  assert.equal(describeAchievementError(new TypeError("offline"), false).retryCreate, true);
  assert.equal(describeAchievementError(conflict, false).retryCreate, false);
  assert.equal(describeAchievementError(new ApiError({ code: "VALIDATION_ERROR", status: 422, message: "raw" }), true).reload, false);
});

test("all private achievement responses from an earlier account are discarded", async () => {
  for (const invoke of [() => listAchievementDrafts("p"), () => getAchievementDraft("p", "a"), () => createAchievementDraft("p", fields, "r"), () => updateAchievementDraft("p", "a", fields, 1)]) {
    let finish!: (response: Response) => void;
    globalThis.fetch = (() => new Promise<Response>((resolve) => { finish = resolve; })) as typeof fetch;
    const pending = invoke();
    recordLogout();
    finish(new Response("{}"));
    await assert.rejects(pending, { name: "AbortError" });
  }
});
