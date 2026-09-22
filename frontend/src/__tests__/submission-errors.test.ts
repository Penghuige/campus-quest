/**
 * Task 4: submission/abandon conflict mapping through the fetch stub —
 * the envelope -> ApiError -> typed-copy chain the upload panel and the
 * abandon control render (spec §10/§8.5/§29; patterns §6/§15).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { createUploadIntent } from "../features/submissions/api";
import { describeSubmissionError, SUBMISSION_CONFLICT_TEXT } from "../features/submissions/submissionErrors";
import { isApiError } from "../lib/errors";

let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status: number) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
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
  globalThis.fetch = (async () => responseFor()) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

async function intentError(): Promise<unknown> {
  return rejectionOf(createUploadIntent("11111111-1111-4111-8111-111111111111", { name: "a.csv", size: 1 }, "CSV"));
}

describe("conflict-code -> typed copy (the four brief codes + neighbors)", () => {
  const cases: Array<[string, string, string]> = [
    ["SUBMISSION_WINDOW_CLOSED", "提交窗口已关闭", "提交窗口已关闭，这个任务无法再提交"],
    ["CLAIM_NOT_SUBMITTABLE", "当前状态不可提交", "当前状态不能提交，可能已在审核中或已结束"],
    ["FILE_TOO_LARGE", "文件过大", "文件大小超出限制，请压缩后重新选择"],
    ["FILE_TYPE_NOT_ALLOWED", "类型不允许", "这个任务不接受该文件类型，请改用任务允许的格式"],
    ["RATE_LIMITED", "请求过多", "操作过于频繁，请稍后再试"],
    ["ABANDON_LIMIT_REACHED", "今日放弃次数已达上限", "今日放弃次数已达上限，请明天再试"],
    ["CLAIM_NOT_ABANDONABLE", "不可放弃", "当前状态不能放弃（审核中的任务由老师处理）"],
  ];

  for (const [code, serverMessage, expectedCopy] of cases) {
    test(`${code} -> stable typed copy (message text never parsed)`, async () => {
      stubFetch(envelope(code, serverMessage), code === "FILE_TOO_LARGE" ? 400 : 409);
      const error = await intentError();
      assert.ok(isApiError(error));
      const view = describeSubmissionError(error);
      assert.equal(view.message, expectedCopy);
      assert.equal(view.requestId, null);
    });
  }

  test("every mapped code keeps retry-friendly framing (no lockout copy)", () => {
    for (const copy of Object.values(SUBMISSION_CONFLICT_TEXT)) {
      assert.doesNotMatch(copy, /禁止|无法恢复|永久/);
    }
  });
});

describe("tiering outside the typed map", () => {
  test("system code -> infra copy + request id", async () => {
    stubFetch(envelope("INTERNAL_ERROR", "boom", { request_id: "req-7" }), 500);
    const error = await intentError();
    const view = describeSubmissionError(error);
    assert.equal(view.message, "服务暂时不可用，请稍后重试");
    assert.equal(view.requestId, "req-7");
  });

  test("known out-of-surface business code -> backend message as fallback", async () => {
    // STUDENT_NOT_WHITELISTED is registry-known but not submission-surface.
    stubFetch(envelope("STUDENT_NOT_WHITELISTED", "账号未在白名单中"), 403);
    const error = await intentError();
    const view = describeSubmissionError(error);
    assert.equal(view.message, "账号未在白名单中");
  });

  test("unknown code (registry drift) -> generic copy + request id", async () => {
    stubFetch(envelope("FUTURE_CODE", "未来错误", { request_id: "req-12" }), 409);
    const error = await intentError();
    const view = describeSubmissionError(error);
    assert.equal(view.message, "操作失败，请稍后重试");
    assert.equal(view.requestId, "req-12");
  });

  test("network failure (non-ApiError) -> connectivity copy", () => {
    const view = describeSubmissionError(new TypeError("fetch failed"));
    assert.equal(view.message, "网络异常，请检查连接后重试");
    assert.equal(view.requestId, null);
  });

  test("storage PUT failures (non-ApiError) keep the connectivity tier", () => {
    const view = describeSubmissionError(new TypeError("对象存储上传失败，请检查网络连接"));
    assert.equal(view.message, "网络异常，请检查连接后重试");
  });
});
