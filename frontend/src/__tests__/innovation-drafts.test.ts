import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  createProjectDraft,
  getProjectDraft,
  listProjectDrafts,
  updateProjectDraft,
} from "../features/innovation/api";
import {
  describeDraftSaveError,
  draftFields,
  normalizeDraftFields,
  sameDraftFields,
  validateDraftFields,
} from "../features/innovation/draftForm";
import { ApiError } from "../lib/errors";
import { recordLogout } from "../lib/accessToken";

const fields = {
  title: "校园环保计划",
  summary: "",
  direction: "",
  stage: "",
  team_status: "",
};
const id = "11111111-1111-4111-8111-111111111111";
const requestId = "22222222-2222-4222-8222-222222222222";
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("incomplete private overview is valid and every field is trimmed", () => {
  assert.deepEqual(validateDraftFields(fields), {});
  assert.deepEqual(normalizeDraftFields({
    title: "  校园环保计划  ", summary: " \n ", direction: " 环保 ",
    stage: " 想法阶段 ", team_status: "  尚未组队  ",
  }), {
    title: fields.title, summary: "", direction: "环保",
    stage: "想法阶段", team_status: "尚未组队",
  });
  assert.equal(validateDraftFields({ ...fields, title: " \n\t " }).title, "请输入项目名称");
});

test("field limits count Unicode code points, including astral characters", () => {
  const limits = { title: 120, summary: 2000, direction: 120, stage: 80, team_status: 1000 };
  for (const [field, limit] of Object.entries(limits)) {
    const atLimit = { ...fields, [field]: "🌱".repeat(limit) };
    assert.deepEqual(validateDraftFields(atLimit), {}, `${field} accepts its boundary`);
    assert.ok(validateDraftFields({ ...atLimit, [field]: "🌱".repeat(limit + 1) })[field as keyof typeof fields]);
  }
  assert.deepEqual(validateDraftFields({ ...fields, title: `  ${"🌱".repeat(120)}  ` }), {});
});

test("editing a saved project excludes resource metadata and detects every unsaved field", () => {
  const populated = { title: fields.title, summary: "调研校园垃圾分类现状", direction: "环保", stage: "调研中", team_status: "两人负责问卷，一人整理数据" };
  const saved = { ...populated, id, version: 3, created_at: "2026-10-08T01:00:00Z", updated_at: "2026-10-08T01:00:00Z" };
  const editable = draftFields(saved);
  assert.deepEqual(editable, populated);
  assert.equal(sameDraftFields(editable, { ...populated }), true);
  for (const key of Object.keys(fields) as (keyof typeof fields)[]) {
    assert.equal(sameDraftFields(editable, { ...populated, [key]: populated[key] + "修改" }), false, key);
  }
  editable.summary = "尚未保存的简介";
  assert.equal(saved.summary, populated.summary);
});

test("IE requests use private paths, explicit idempotency and expected version", async () => {
  const requests: { url: string; method: string; body: unknown }[] = [];
  const dto = { ...fields, id, version: 3, created_at: "2026-10-08T01:00:00Z", updated_at: "2026-10-08T01:00:00Z" };
  globalThis.fetch = (async (input, init) => {
    requests.push({ url: String(input), method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
    return new Response(JSON.stringify(String(input).includes("?") ? { items: [dto], total: 21, limit: 20, offset: 20 } : dto), { status: 200 });
  }) as typeof fetch;
  const page = await listProjectDrafts({ limit: 20, offset: 20 });
  assert.equal(page.total, 21);
  assert.equal((await getProjectDraft(id)).version, 3);
  await createProjectDraft(fields, requestId);
  await createProjectDraft(fields, requestId);
  await updateProjectDraft(id, fields, 3);
  assert.deepEqual(requests, [
    { url: "/api/v1/ie/me/project-drafts?limit=20&offset=20", method: "GET", body: null },
    { url: `/api/v1/ie/me/project-drafts/${id}`, method: "GET", body: null },
    { url: "/api/v1/ie/me/project-drafts", method: "POST", body: { ...fields, request_id: requestId } },
    { url: "/api/v1/ie/me/project-drafts", method: "POST", body: { ...fields, request_id: requestId } },
    { url: `/api/v1/ie/me/project-drafts/${id}`, method: "PATCH", body: { ...fields, version: 3 } },
  ]);
});

test("save errors distinguish stale versions, unknown create outcome and permissions", () => {
  const conflict = describeDraftSaveError(new ApiError({ code: "PROJECT_DRAFT_VERSION_CONFLICT", status: 409, message: "stale" }));
  assert.equal(conflict.reload, true);
  assert.match(conflict.message, /未保存.*保留/);
  assert.match(describeDraftSaveError(new Error("offline")).message, /重试/);
  const forbidden = describeDraftSaveError(new ApiError({ code: "PERMISSION_DENIED", status: 403, message: "raw" }));
  assert.equal(forbidden.reload, false);
  assert.match(forbidden.message, /学生/);
});

test("refused draft saves retain actionable messages and system failure tracing", () => {
  for (const [code, status, expected] of [
    ["PROJECT_DRAFT_REQUEST_CONFLICT", 409, /新建请求.*不同内容.*保留/],
    ["ACCOUNT_NOT_ACTIVE", 403, /正常状态.*学生/],
    ["NOT_FOUND", 404, /不存在.*无权访问.*保留/],
    ["VALIDATION_ERROR", 422, /校验.*长度/],
  ] as const) {
    const view = describeDraftSaveError(new ApiError({ code, status, message: "raw server detail" }));
    assert.equal(view.reload, false);
    assert.match(view.message, expected);
    assert.doesNotMatch(view.message, /raw server detail/);
  }
  const system = describeDraftSaveError(new ApiError({ code: "INTERNAL_ERROR", status: 500, message: "raw server detail", requestId: "trace-draft" }));
  assert.equal(system.requestId, "trace-draft");
  assert.doesNotMatch(system.message, /raw server detail|保存成功/);
});

test("a successful private response from an old auth context is discarded", async () => {
  let finish!: (response: Response) => void;
  globalThis.fetch = (() => new Promise<Response>((resolve) => { finish = resolve; })) as typeof fetch;
  const pending = getProjectDraft(id);
  recordLogout();
  finish(new Response(JSON.stringify({ ...fields, id, version: 1 }), { status: 200 }));
  await assert.rejects(pending, { name: "AbortError" });
});
