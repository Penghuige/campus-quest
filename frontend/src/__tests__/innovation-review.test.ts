import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { apiRequest } from "../lib/api";
import { recordLogout, setAccessToken } from "../lib/accessToken";
import { getWorkflow, listEvidence, completeEvidence, removeEvidence, claimReview, declareConflict, readReviewEvidence, submitRevision, withdrawReview, publishRevision, listReviewQueue, getReviewDetail, decideReview, listPublicAchievements, getPublicAchievement, readEvidence, uploadEvidence, saveProofBlob } from "../features/innovation/reviewApi";
import { evidenceFileError, evidenceStatusText, reviewError, reviewStatusText } from "../features/innovation/reviewPresentation";
import { ApiError } from "../lib/errors";

const original = globalThis.fetch;
afterEach(() => { globalThis.fetch = original; recordLogout(); });
const command = { request_id: "key", workflow_version: 2, project_version: 3, achievement_version: 4, evidence_ids: ["proof"] };

test("review commands preserve saved version and stable request key; never grant themselves authority", async () => {
  const seen: unknown[] = [];
  globalThis.fetch = (async (path, init) => { seen.push([String(path), init?.method ?? "GET", init?.body ? JSON.parse(String(init.body)) : null]); return new Response("{}"); }) as typeof fetch;
  await submitRevision("p/x", "a", command); await submitRevision("p/x", "a", command);
  await withdrawReview("p/x", "a", { workflow_version: 2, case_id: "c", case_version: 1 });
  await publishRevision("p/x", "a", command);
  await decideReview("c", { version: 3, revision_id: "r", request_id: "key", decision: "RETURNED", reason: "补充说明" });
  const base = "/api/v1/ie/me/project-drafts/p%2Fx/achievements/a";
  assert.deepEqual(seen, [[`${base}/submit`, "POST", command], [`${base}/submit`, "POST", command], [`${base}/withdraw`, "POST", { workflow_version: 2, case_id: "c", case_version: 1 }], [`${base}/publish-update`, "POST", command], ["/api/v1/ie/ops/achievement-reviews/c/decision", "POST", { version: 3, revision_id: "r", request_id: "key", decision: "RETURNED", reason: "补充说明" }]]);
});

test("binary responses retain authenticated transport and error envelopes", async () => {
  setAccessToken("test-memory-token");
  globalThis.fetch = (async (_path, init) => { assert.equal(new Headers(init?.headers).get("Authorization"), "Bearer test-memory-token"); return new Response(new Uint8Array([0, 255, 128]), { headers: { "Content-Type": "application/pdf" } }); }) as typeof fetch;
  const file = await apiRequest<Blob>("/api/v1/private", { responseFormat: "blob" });
  assert.deepEqual([...new Uint8Array(await file.arrayBuffer())], [0, 255, 128]);
  globalThis.fetch = (async () => new Response(JSON.stringify({ error: { code: "FORBIDDEN", message: "private" } }), { status: 403 })) as typeof fetch;
  await assert.rejects(apiRequest("/api/v1/private", { responseFormat: "blob" }), { status: 403 });
});

test("upload echoes signed headers without credentials, then relies on the server check; replay PUT 412 is safe", async () => {
  for (const putStatus of [200, 412]) {
    let call = 0;
    globalThis.fetch = (async (path, init) => {
      call++;
      if (call === 1) return Response.json({ evidence: { id: "e" }, upload_url: "http://127.0.0.1:9000/signed", pinned_content_length: 3, client_headers: { "Content-Type": "application/pdf", "If-None-Match": "*" } });
      if (call === 2) { assert.equal(String(path), "http://127.0.0.1:9000/signed"); assert.equal(init?.credentials, "omit"); assert.deepEqual(init?.headers, { "Content-Type": "application/pdf", "If-None-Match": "*" }); return new Response(null, { status: putStatus }); }
      assert.equal(String(path).endsWith("/evidence/e/complete"), true); return Response.json({ id: "e", state: "READY" });
    }) as typeof fetch;
    assert.equal((await uploadEvidence("p", "a", new Blob(["pdf"], { type: "application/pdf" }), "stable")).state, "READY");
    assert.equal(call, 3);
  }
});

test("upload rejects size pin mismatch, provider errors and account changes before completion", async () => {
  for (const scenario of ["size", "provider", "account"] as const) {
    let call = 0;
    globalThis.fetch = (async () => {
      call++;
      if (call === 1) return Response.json({ evidence: { id: "e" }, upload_url: "http://127.0.0.1:9000/signed", pinned_content_length: scenario === "size" ? 4 : 3, client_headers: {} });
      if (scenario === "account") recordLogout();
      return new Response(null, { status: scenario === "provider" ? 403 : 200 });
    }) as typeof fetch;
    await assert.rejects(uploadEvidence("p", "a", new Blob(["pdf"], { type: "application/pdf" }), "stable"));
    assert.equal(call, scenario === "size" ? 1 : 2);
  }
});

test("workflow, private material, operations and school reads discard earlier-account results", async () => {
  for (const invoke of [() => getWorkflow("p", "a"), () => listEvidence("p", "a"), () => listReviewQueue(), () => getReviewDetail("c"), () => listPublicAchievements(), () => getPublicAchievement("a"), () => readEvidence("p", "a", "e")]) {
    let finish!: (value: Response) => void;
    globalThis.fetch = (() => new Promise<Response>((resolve) => { finish = resolve; })) as typeof fetch;
    const pending = invoke(); recordLogout(); finish(new Response("{}"));
    await assert.rejects(pending, { name: "AbortError" });
  }
});

test("file convenience validation never labels unchecked material as ready; states distinguish review from publication", () => {
  assert.equal(evidenceFileError(new Blob(["x"], { type: "application/pdf" })), null);
  assert.ok(evidenceFileError(new Blob([], { type: "application/pdf" })));
  assert.ok(evidenceFileError(new Blob(["x"], { type: "application/zip" })));
  assert.ok(evidenceFileError(new Blob([new Uint8Array(10 * 1024 * 1024 + 1)], { type: "image/png" })));
  assert.equal(reviewStatusText("SUBMITTED"), "等待首次核实");
  assert.equal(reviewStatusText("RETURNED"), "已退回，修改后可重新提交");
  assert.equal(reviewStatusText("APPROVED"), "首次核实已通过");
});

test("private material and operator wrappers preserve scoped paths, cache isolation and expected case versions", async () => {
  const seen: unknown[] = [];
  globalThis.fetch = (async (path, init) => { seen.push([String(path), init?.method ?? "GET", init?.body ? JSON.parse(String(init.body)) : null, init?.cache]); return new Response("{}"); }) as typeof fetch;
  await completeEvidence("p", "a", "e/x"); await removeEvidence("p", "a", "e/x");
  await claimReview("c/x", 7); await declareConflict("c/x", 7); await readReviewEvidence("c/x", "e/x");
  const base = "/api/v1/ie/me/project-drafts/p/achievements/a/evidence/e%2Fx";
  assert.deepEqual(seen, [[`${base}/complete`, "POST", null, "no-store"], [base, "DELETE", null, "no-store"], ["/api/v1/ie/ops/achievement-reviews/c%2Fx/claim", "POST", { version: 7 }, "no-store"], ["/api/v1/ie/ops/achievement-reviews/c%2Fx/conflict", "POST", { version: 7 }, "no-store"], ["/api/v1/ie/ops/achievement-reviews/c%2Fx/evidence/e%2Fx/content", "GET", null, "no-store"]]);
});

test("safe errors and file-state copy distinguish permission, stale workflow, validation and unavailable checks", () => {
  assert.ok(reviewError(new ApiError({ code: "INTERNAL_ERROR", status: 500, message: "raw secret", requestId: "review-trace-123" })).includes("review-trace-123"));
  for (const [status, fragment] of [[403, "权限"], [404, "不可访问"], [409, "状态已改变"], [422, "已保存"], [503, "尚未获准"]] as const) {
    const copy = reviewError(new ApiError({ code: "CONFLICT", status, message: "raw secret provider path" }));
    assert.ok(copy.includes(fragment)); assert.ok(!copy.includes("raw secret"));
  }
  assert.ok(reviewError(new ApiError({ code: "INTERNAL_ERROR", status: 500, message: "raw" })).includes("尚未确认"));
  assert.ok(reviewError(new TypeError("offline")).includes("尚未确认"));
  assert.equal(evidenceStatusText("PENDING"), "等待上传或重新检查"); assert.equal(evidenceStatusText("CHECKING"), "文件检查中"); assert.equal(evidenceStatusText("READY"), "文件检查通过"); assert.equal(evidenceStatusText("REJECTED"), "文件检查未通过");
});

test("private blob downloads use temporary local URLs and generic names, never a remote storage address", async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  const create = URL.createObjectURL; const revoke = URL.revokeObjectURL;
  const names: string[] = []; const revoked: string[] = [];
  try {
    URL.createObjectURL = () => "blob:local-proof"; URL.revokeObjectURL = (url) => { revoked.push(url); };
    Object.defineProperty(globalThis, "document", { configurable: true, value: { createElement: () => ({ href: "", download: "", click() { assert.equal(this.href, "blob:local-proof"); names.push(this.download); } }) } });
    for (const type of ["application/pdf", "image/png", "image/jpeg"]) saveProofBlob(new Blob(["x"], { type }));
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.deepEqual(names, ["evidence.pdf", "evidence.png", "evidence.jpg"]); assert.equal(revoked.length, 3);
  } finally { URL.createObjectURL = create; URL.revokeObjectURL = revoke; if (descriptor) Object.defineProperty(globalThis, "document", descriptor); else Reflect.deleteProperty(globalThis, "document"); }
});
