import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { getInnovationCapabilities, getOperationsGrant, saveOperationsGrant } from "../features/innovation/operationsApi";
import { operationsMutationError } from "../features/innovation/operationsForm";
import { recordLogout } from "../lib/accessToken";
import { ApiError } from "../lib/errors";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("operations API uses scoped paths and version, never a role promotion", async () => {
  const requests: unknown[] = [];
  globalThis.fetch = (async (input, init) => {
    requests.push([String(input), init?.method ?? "GET", init?.body ? JSON.parse(String(init.body)) : null]);
    return new Response(JSON.stringify({ enabled: true, version: 1 }));
  }) as typeof fetch;
  await getInnovationCapabilities();
  await getOperationsGrant("account/id");
  await saveOperationsGrant("account/id", { enabled: true, version: 0, reason: "指定运营" });
  assert.deepEqual(requests, [
    ["/api/v1/ie/me/capabilities", "GET", null],
    ["/api/v1/admin/ie/operations-grants/account%2Fid", "GET", null],
    ["/api/v1/admin/ie/operations-grants/account%2Fid", "PUT", { enabled: true, version: 0, reason: "指定运营" }],
  ]);
});

test("lost acknowledgements and stale grants require rereading before another mutation", () => {
  for (const cause of [new TypeError("offline"), new ApiError({ code: "CONFLICT", status: 409, message: "raw" }), new ApiError({ code: "INTERNAL_ERROR", status: 500, message: "raw" })]) {
    assert.equal(operationsMutationError(cause).reload, true);
  }
  assert.equal(operationsMutationError(new ApiError({ code: "VALIDATION_ERROR", status: 422, message: "raw" })).reload, false);
});

test("grant and capability responses are discarded on account changes", async () => {
  for (const invoke of [() => getInnovationCapabilities(), () => getOperationsGrant("id"), () => saveOperationsGrant("id", { enabled: false, version: 1, reason: "撤回" })]) {
    let finish!: (response: Response) => void;
    globalThis.fetch = (() => new Promise<Response>((resolve) => { finish = resolve; })) as typeof fetch;
    const pending = invoke();
    recordLogout();
    finish(new Response("{}"));
    await assert.rejects(pending, { name: "AbortError" });
  }
});
