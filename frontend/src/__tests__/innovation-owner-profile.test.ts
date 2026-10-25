import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { getOwnerProfile, saveOwnerProfile } from "../features/innovation/ownerApi";
import { normalizeOwnerFields, ownerFields, validateOwnerFields, describeOwnerSaveError } from "../features/innovation/ownerForm";
import { recordLogout } from "../lib/accessToken";
import { ApiError } from "../lib/errors";

const fields = { name: " 小叶 ", student_no: " 001234 ", major: " 计算机 ", grade: " 大一 " };
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("owner form requires exactly four trimmed self-reported fields and preserves leading zero", () => {
  assert.deepEqual(normalizeOwnerFields(fields), { name: "小叶", student_no: "001234", major: "计算机", grade: "大一" });
  assert.deepEqual(validateOwnerFields(fields), {});
  for (const [key, limit] of Object.entries({ name: 80, student_no: 40, major: 120, grade: 40 })) {
    assert.ok(validateOwnerFields({ ...fields, [key]: " \n " })[key as keyof typeof fields]);
    assert.deepEqual(validateOwnerFields({ ...fields, [key]: "🌱".repeat(limit) }), {});
    assert.ok(validateOwnerFields({ ...fields, [key]: "🌱".repeat(limit + 1) })[key as keyof typeof fields]);
  }
});

test("editing an owner profile includes only self-reported fields, never qualification or version", () => {
  const profile = { ...fields, version: 4, owner_qualified: true };
  const editable = ownerFields(profile);
  assert.deepEqual(editable, fields);
  editable.student_no = "000999";
  assert.equal(profile.student_no, fields.student_no);
});

test("owner API has no target account or qualification parameter and uses expected version", async () => {
  const seen: { url: string; method: string; body: unknown }[] = [];
  globalThis.fetch = (async (input, init) => {
    seen.push({ url: String(input), method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
    return new Response(JSON.stringify(init?.method === "PUT" ? { ...fields, version: 1 } : { profile: null }));
  }) as typeof fetch;
  assert.equal((await getOwnerProfile()).profile, null);
  await saveOwnerProfile(fields, 0);
  await saveOwnerProfile(fields, 1);
  assert.deepEqual(seen, [
    { url: "/api/v1/ie/me/owner-profile", method: "GET", body: null },
    { url: "/api/v1/ie/me/owner-profile", method: "PUT", body: { ...fields, version: 0 } },
    { url: "/api/v1/ie/me/owner-profile", method: "PUT", body: { ...fields, version: 1 } },
  ]);
});

test("stale and unknown saves require reconciliation, without claiming success", () => {
  const stale = describeOwnerSaveError(new ApiError({ code: "OWNER_PROFILE_VERSION_CONFLICT", status: 409, message: "raw" }));
  assert.equal(stale.reload, true);
  assert.match(stale.message, /保留/);
  const unknown = describeOwnerSaveError(new TypeError("offline"));
  assert.equal(unknown.reload, true);
  assert.match(unknown.message, /尚未确认/);
  assert.equal(describeOwnerSaveError(new ApiError({ code: "VALIDATION_ERROR", status: 422, message: "raw" })).reload, false);
});

test("owner save refusals and possible post-commit server failures have distinct recovery", () => {
  for (const code of ["PERMISSION_DENIED", "ACCOUNT_NOT_ACTIVE"]) {
    const denied = describeOwnerSaveError(new ApiError({ code, status: 403, message: "raw" }));
    assert.equal(denied.reload, false);
    assert.match(denied.message, /正常状态.*学生/);
  }
  for (const status of [429, 500, 503]) {
    const message = status === 429 ? "请求过于频繁，请稍后再试" : "raw server detail";
    const view = describeOwnerSaveError(new ApiError({ code: status === 429 ? "RATE_LIMITED" : "INTERNAL_ERROR", status, message, requestId: "trace-owner" }));
    assert.equal(view.reload, status >= 500);
    assert.equal(view.requestId, status >= 500 ? "trace-owner" : null);
    assert.doesNotMatch(view.message, /raw server detail|保存成功/);
    if (status === 429) assert.equal(view.message, message);
  }
});

test("profile reads and saves discard private responses after an auth transition", async () => {
  for (const invoke of [() => getOwnerProfile(), () => saveOwnerProfile(fields, 0)]) {
    let finish!: (response: Response) => void;
    globalThis.fetch = (() => new Promise<Response>((resolve) => { finish = resolve; })) as typeof fetch;
    const pending = invoke();
    recordLogout();
    finish(new Response(JSON.stringify({ ...fields, version: 1 })));
    await assert.rejects(pending, { name: "AbortError" });
  }
});
