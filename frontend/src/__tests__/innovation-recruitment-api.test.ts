import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { recordLogout, setAccessToken } from "../lib/accessToken";
import { emptyRecruitmentForm, normalizeRecruitmentForm } from "../features/innovation/recruitmentForm";
import { createRecruitment, updateRecruitment, publishRecruitment, closeRecruitment, reopenRecruitment, confirmActiveRecruitment, revokeRecruitmentContact, getRecruitmentContacts, getPublicProjects, getPublicRecruitments } from "../features/innovation/recruitmentApi";

const original = globalThis.fetch;
afterEach(() => { globalThis.fetch = original; recordLogout(); });
test("private commands preserve request keys and versions; contact access is a separate GET", async () => {
  setAccessToken("demo-token");
  const calls: { path: string; init: RequestInit }[] = [];
  globalThis.fetch = (async (path, init) => { calls.push({ path: String(path), init: init ?? {} }); return Response.json({}); }) as typeof fetch;
  const command = { version: 3, request_id: "stable" };
  const fields = normalizeRecruitmentForm(emptyRecruitmentForm());
  await createRecruitment("p/a", { request_id: "create", fields });
  await updateRecruitment("p", "r", { ...command, fields });
  for (const operation of [publishRecruitment, closeRecruitment, reopenRecruitment, confirmActiveRecruitment]) await operation("p", "r", command);
  await revokeRecruitmentContact("p", "r", { ...command, channel: "WECHAT", close_if_last: true });
  await getRecruitmentContacts("r");
  assert.match(calls[0].path, /p%2Fa\/recruitments$/);
  for (const call of calls.slice(1, 7)) assert.equal(JSON.parse(String(call.init.body)).request_id, "stable");
  assert.match(calls[7].path, /\/recruitments\/r\/contacts$/);
  assert.equal(calls[7].init.body, undefined);
  assert.ok(calls.every((call) => call.init.cache === "no-store"));
});
test("public filters are encoded and never treated as private draft search", async () => {
  const paths: string[] = [];
  globalThis.fetch = (async (path) => { paths.push(String(path)); return Response.json({}); }) as typeof fetch;
  await getPublicProjects(new URLSearchParams({ q: "学校 & 项目", recruiting_only: "true", offset: "20" }));
  await getPublicRecruitments(new URLSearchParams({ beginner_friendly: "false" }));
  assert.match(paths[0], /\/ie\/projects\?/);
  assert.equal(new URL(paths[0], "http://test").searchParams.get("q"), "学校 & 项目");
  assert.match(paths[1], /beginner_friendly=false/);
});
test("late contact response cannot cross account epoch", async () => {
  let finish!: () => void;
  globalThis.fetch = (async () => { await new Promise<void>((resolve) => { finish = resolve; }); return Response.json({ contacts: [{ channel: "WECHAT", value: "private" }] }); }) as typeof fetch;
  const request = getRecruitmentContacts("r");
  recordLogout(); finish();
  await assert.rejects(request, { name: "AbortError" });
});
test("requests have an abortable budget so stalled operations can be retried", async () => {
  let signal: AbortSignal | null | undefined;
  globalThis.fetch = (async (_path, init) => { signal = init?.signal; return Response.json({}); }) as typeof fetch;
  await getRecruitmentContacts("r");
  assert.ok(signal instanceof AbortSignal);
  assert.equal(signal.aborted, false);
});
