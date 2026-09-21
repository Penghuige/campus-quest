/**
 * Task 3 (Plan 09): claim conflict mapping through the fetch stub — the
 * envelope -> ApiError -> typed-copy chain ClaimButton renders (spec §8.3/
 * §29; patterns §6/§15), plus the bodyless-claim wire pin (§42: the client
 * cannot smuggle an assignment choice).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { claimTask } from "../features/tasks/api";
import { describeClaimError } from "../features/tasks/claimErrors";
import { describeSectionError } from "../lib/errors";
import { isApiError } from "../lib/errors";

type RecordedRequest = {
  url: string;
  method: string;
  body: string | null;
  headers: Headers;
};

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status: number, headers: Record<string, string> = {}) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status, headers });
}

function envelope(code: string, message: string, extra: Record<string, unknown> = {}) {
  return JSON.stringify({
    error: { code, message, details: null, request_id: null, ...extra },
  });
}

async function rejectionOf(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => assert.fail("expected rejection"),
    (error: unknown) => error,
  );
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      headers: new Headers(init?.headers),
      body: typeof init?.body === "string" ? init.body : null,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("claim wire contract", () => {
  test("claim posts with NO body — the server picks the assignment (§8.3)", async () => {
    stubFetch(
      JSON.stringify({
        claim_id: "11111111-1111-4111-8111-111111111111",
        task_id: "22222222-2222-4222-8222-222222222222",
        status: "CLAIMED",
        platform: "小红书",
        keyword: "campus-quest-keyword",
        claimed_at: "2026-09-21T08:00:00Z",
        deadline_at: "2026-09-21T20:00:00Z",
        grace_deadline_at: "2026-09-22T20:00:00Z",
        base_reward_points_snapshot: 160,
      }),
      201,
    );
    const claim = await claimTask("22222222-2222-4222-8222-222222222222");
    assert.equal(recorded?.url, "/api/v1/tasks/22222222-2222-4222-8222-222222222222/claim");
    assert.equal(recorded?.method, "POST");
    // No request body and no assignment material in the request: the ONLY
    // platform/keyword the surface ever sees come from the RESPONSE.
    assert.equal(recorded?.body, null);
    assert.equal(claim.platform, "小红书");
    assert.equal(claim.keyword, "campus-quest-keyword");
  });
});

describe("conflict-code -> typed copy", () => {
  const cases: Array<[string, string, string]> = [
    ["NO_ASSIGNMENT_AVAILABLE", "没有可用的任务单元", "当前没有可领取的任务单元，请稍后重试或先看看其他任务"],
    ["ASSIGNMENT_LIMIT_REACHED", "进行中的任务已达上限", "进行中的任务已达上限，请先提交或放弃一个任务后再领取"],
    ["TASK_ACTIVE_CLAIM_EXISTS", "已存在进行中的领取", "你已经领取过这个任务，完成或放弃后可再次领取"],
    ["CLAIM_CUTOFF_REACHED", "已过领取截止时间", "该任务已过领取截止时间，看看其他任务吧"],
    ["TASK_NOT_CLAIMABLE", "任务当前不可领取", "该任务当前不可领取"],
    ["RATE_LIMITED", "请求过于频繁", "操作过于频繁，请稍后再试"],
  ];

  for (const [code, serverMessage, expectedCopy] of cases) {
    test(`${code} -> stable typed copy (message text never parsed)`, async () => {
      stubFetch(envelope(code, serverMessage), 409);
      const error = await rejectionOf(claimTask("22222222-2222-4222-8222-222222222222"));
      assert.ok(isApiError(error));
      const view = describeClaimError(error);
      assert.equal(view.message, expectedCopy);
      assert.equal(view.requestId, null);
    });
  }

  test("NO_ASSIGNMENT_AVAILABLE copy stays retry-friendly (no lockout copy)", async () => {
    stubFetch(envelope("NO_ASSIGNMENT_AVAILABLE", "x"), 409);
    const error = await rejectionOf(claimTask("22222222-2222-4222-8222-222222222222"));
    const view = describeClaimError(error);
    assert.match(view.message, /稍后重试|其他任务/);
    assert.doesNotMatch(view.message, /无法|禁止|不得/);
  });

  test("system code -> infra copy + request id", async () => {
    stubFetch(envelope("INTERNAL_ERROR", "boom", { request_id: "req-9" }), 500);
    const error = await rejectionOf(claimTask("22222222-2222-4222-8222-222222222222"));
    const view = describeClaimError(error);
    assert.equal(view.message, "服务暂时不可用，请稍后重试");
    assert.equal(view.requestId, "req-9");
  });

  test("known out-of-surface business code -> backend message as fallback", async () => {
    stubFetch(envelope("ACCOUNT_NOT_ACTIVE", "账号当前不可用，请联系管理员"), 403);
    const error = await rejectionOf(claimTask("22222222-2222-4222-8222-222222222222"));
    const view = describeClaimError(error);
    assert.equal(view.message, "账号当前不可用，请联系管理员");
  });

  test("unknown code (registry drift) -> generic copy + request id", async () => {
    stubFetch(envelope("BRAND_NEW_CODE", "未来错误", { request_id: "req-11" }), 409);
    const error = await rejectionOf(claimTask("22222222-2222-4222-8222-222222222222"));
    const view = describeClaimError(error);
    assert.equal(view.message, "领取失败，请稍后重试");
    assert.equal(view.requestId, "req-11");
  });

  test("network failure (non-ApiError) -> connectivity copy", () => {
    const view = describeClaimError(new TypeError("fetch failed"));
    assert.equal(view.message, "网络异常，请检查连接后重试");
    assert.equal(view.requestId, null);
  });
});

describe("section error mapping (dashboard/list/detail shared triad)", () => {
  test("system failure -> infra copy + request id", async () => {
    stubFetch(envelope("INTERNAL_ERROR", "boom", { request_id: "req-5" }), 500);
    const error = await rejectionOf(claimTask("22222222-2222-4222-8222-222222222222"));
    const view = describeSectionError(error);
    assert.equal(view.message, "服务暂时不可用，请稍后重试");
    assert.equal(view.requestId, "req-5");
  });

  test("known business code -> the backend's own Chinese message", async () => {
    stubFetch(envelope("PERMISSION_DENIED", "仅学生账号可领取任务"), 403);
    const error = await rejectionOf(claimTask("22222222-2222-4222-8222-222222222222"));
    const view = describeSectionError(error);
    assert.equal(view.message, "仅学生账号可领取任务");
    assert.equal(view.requestId, null);
  });

  test("network failure -> connectivity copy", () => {
    const view = describeSectionError(new TypeError("fetch failed"));
    assert.equal(view.message, "网络异常，请检查连接后重试");
  });
});
