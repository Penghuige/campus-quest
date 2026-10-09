import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  approveOwnerQualification,
  applyOwnerQualification,
  getOwnerQualification,
  getOwnerQualificationApplication,
  listOwnerQualificationApplications,
  type OwnerQualificationDto,
} from "../features/innovation/qualificationApi";
import { ownerQualificationView, qualificationMutationError } from "../features/innovation/qualificationForm";
import { recordLogout } from "../lib/accessToken";
import { ApiError } from "../lib/errors";

const notApplied: OwnerQualificationDto = { status: "NOT_APPLIED", version: 0, profile_version: null, requested_at: null, approved_at: null };
const pending: OwnerQualificationDto = { status: "PENDING", version: 1, profile_version: 2, requested_at: "2026-10-09T01:00:00Z", approved_at: null };
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("qualification cannot be requested before a profile is saved or while edits are unsaved", () => {
  assert.equal(ownerQualificationView(notApplied, null, false).canApply, false);
  assert.match(ownerQualificationView(notApplied, null, false).hint, /先保存/);
  const dirty = ownerQualificationView(notApplied, 2, true);
  assert.equal(dirty.canApply, false);
  assert.match(dirty.hint, /未保存|先保存/);
  assert.equal(ownerQualificationView(notApplied, 2, false).canApply, true);
});

test("pending application keeps its submitted snapshot and permits only a newer saved profile", () => {
  const same = ownerQualificationView(pending, 2, false);
  assert.equal(same.canApply, false);
  assert.match(same.hint, /快照|已提交/);
  const newer = ownerQualificationView(pending, 3, false);
  assert.equal(newer.canApply, true);
  assert.match(newer.actionLabel ?? "", /更新/);
  assert.equal(ownerQualificationView(pending, 3, true).canApply, false);
  assert.equal(ownerQualificationView(pending, 1, false).canApply, false);
});

test("approved qualification cannot be overwritten by newer profile edits and does not approve achievements", () => {
  const approved: OwnerQualificationDto = { ...pending, status: "APPROVED", version: 2, approved_at: "2026-10-09T02:00:00Z" };
  for (const dirty of [false, true]) {
    const view = ownerQualificationView(approved, 4, dirty);
    assert.equal(view.canApply, false);
    assert.equal(view.actionLabel, null);
    assert.match(view.label, /已开通/);
    assert.match(view.hint, /成果.*单独/);
  }
});

test("conflicts and uncertain qualification writes block retries until a successful reread", () => {
  for (const cause of [new TypeError("offline"), new ApiError({ code: "CONFLICT", status: 409, message: "raw conflict", requestId: "qualification-conflict" }), new ApiError({ code: "INTERNAL_ERROR", status: 503, message: "raw exception", requestId: "qualification-server" })]) {
    const view = qualificationMutationError(cause);
    assert.equal(view.reload, true);
    assert.doesNotMatch(view.message, /raw|成功/);
    if (cause instanceof ApiError) assert.equal(view.requestId, cause.requestId);
  }
});

test("known qualification refusals do not masquerade as unknown committed writes", () => {
  for (const code of ["PERMISSION_DENIED", "ACCOUNT_NOT_ACTIVE", "VALIDATION_ERROR", "RATE_LIMITED"]) {
    const view = qualificationMutationError(new ApiError({ code, status: code === "VALIDATION_ERROR" ? 422 : code === "RATE_LIMITED" ? 429 : 403, message: "明确拒绝", requestId: "qualification-denial" }));
    assert.equal(view.reload, false);
    assert.doesNotMatch(view.message, /成功/);
  }
});

test("qualification APIs send saved versions only and admin reads use separate escaped scoped paths", async () => {
  const seen: unknown[] = [];
  globalThis.fetch = (async (input, init) => {
    seen.push([String(input), init?.method ?? "GET", init?.body ? JSON.parse(String(init.body)) : null, init?.cache]);
    return new Response(JSON.stringify(pending));
  }) as typeof fetch;
  await getOwnerQualification();
  await applyOwnerQualification(0, 2);
  await listOwnerQualificationApplications({ limit: 20, offset: 40 });
  await getOwnerQualificationApplication("account/id");
  await approveOwnerQualification("account/id", 1);
  assert.deepEqual(seen, [
    ["/api/v1/ie/me/owner-qualification", "GET", null, "no-store"],
    ["/api/v1/ie/me/owner-qualification", "POST", { version: 0, profile_version: 2 }, undefined],
    ["/api/v1/admin/ie/owner-qualifications?limit=20&offset=40", "GET", null, "no-store"],
    ["/api/v1/admin/ie/owner-qualifications/account%2Fid", "GET", null, "no-store"],
    ["/api/v1/admin/ie/owner-qualifications/account%2Fid/approve", "POST", { version: 1 }, undefined],
  ]);
});

test("qualification and admin PII responses are discarded when the account changes", async () => {
  const invokes = [() => getOwnerQualification(), () => applyOwnerQualification(0, 2), () => listOwnerQualificationApplications(), () => getOwnerQualificationApplication("id"), () => approveOwnerQualification("id", 1)];
  for (const invoke of invokes) {
    let finish!: (response: Response) => void;
    globalThis.fetch = (() => new Promise<Response>((resolve) => { finish = resolve; })) as typeof fetch;
    const result = invoke();
    recordLogout();
    finish(new Response(JSON.stringify(pending)));
    await assert.rejects(result, { name: "AbortError" });
  }
});

test("qualification API preserves business conflict envelopes and request tracing", async () => {
  globalThis.fetch = (async () => new Response(JSON.stringify({ error: { code: "CONFLICT", message: "申请已改变", request_id: "qualification-http" } }), { status: 409 })) as typeof fetch;
  await assert.rejects(applyOwnerQualification(1, 3), (cause: unknown) => cause instanceof ApiError && cause.code === "CONFLICT" && cause.requestId === "qualification-http");
});
