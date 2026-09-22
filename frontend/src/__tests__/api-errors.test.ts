/**
 * Task 1 (Plan 09): envelope -> ApiError mapping.
 *
 * Pins the spec §29 contract the whole frontend branches on:
 * - every field of the error envelope survives into `ApiError`
 *   (code / message / details / request_id / HTTP status);
 * - business branching uses `error.code` ONLY — the Chinese `message` is
 *   display text and must never be parsed (two envelopes with the same
 *   code but different messages take the same branch);
 * - non-envelope failures degrade to the system `HTTP_ERROR` code with a
 *   safe message;
 * - the credentials/CSRF/request-id request contract of `apiRequest`.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { apiRequest, REQUEST_ID_HEADER } from "../lib/api";
import {
  ApiError,
  isApiError,
  isKnownErrorCode,
  isSystemErrorCode,
} from "../lib/errors";

type RecordedRequest = {
  url: string;
  method: string;
  headers: Headers;
  credentials: RequestCredentials | undefined;
};

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status: number, headers: Record<string, string> = {}) {
  // 204/205 are null-body statuses: undici rejects a Response constructed
  // with an explicit (even empty) body for them.
  responseFor = () => new Response(body.length > 0 ? body : null, { status, headers });
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      headers: new Headers(init?.headers),
      credentials: init?.credentials,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function envelope(code: string, message: string, extra: object = {}) {
  return JSON.stringify({
    error: { code, message, details: { limit: 3 }, request_id: "req-123", ...extra },
  });
}

/** The only sanctioned way to branch on claim failures: by code. */
function claimFailureBranch(error: ApiError): string {
  switch (error.code) {
    case "ASSIGNMENT_LIMIT_REACHED":
      return "show-active-claims";
    case "NO_ASSIGNMENT_AVAILABLE":
      return "show-empty";
    default:
      return "generic";
  }
}

describe("envelope -> ApiError mapping", () => {
  test("preserves code, message, details, request_id, and status", async () => {
    stubFetch(
      envelope("ASSIGNMENT_LIMIT_REACHED", "当前进行中的任务已达到上限"),
      409,
      { [REQUEST_ID_HEADER]: "hdr-unused" },
    );

    await assert.rejects(
      apiRequest("/api/v1/tasks/1/claim", { method: "POST", body: {} }),
      (error: unknown) => {
        assert.ok(isApiError(error));
        assert.equal(error.code, "ASSIGNMENT_LIMIT_REACHED");
        assert.equal(error.message, "当前进行中的任务已达到上限");
        assert.deepEqual(error.details, { limit: 3 });
        assert.equal(error.requestId, "req-123");
        assert.equal(error.status, 409);
        return true;
      },
    );
  });

  test("branches on code, never by parsing the message", async () => {
    const messages = [
      "当前进行中的任务已达到上限",
      "Server reworded this text entirely",
      "", // even an empty message must not change the branch
    ];
    for (const message of messages) {
      stubFetch(envelope("ASSIGNMENT_LIMIT_REACHED", message), 409);
      const error = await apiRequest("/api/v1/me", { method: "POST", body: {} }).then(
        () => assert.fail("expected rejection"),
        (e: unknown) => e,
      );
      assert.ok(isApiError(error));
      assert.equal(claimFailureBranch(error), "show-active-claims");
      assert.equal(error.message, message); // display text kept verbatim
    }

    stubFetch(envelope("NO_ASSIGNMENT_AVAILABLE", "当前没有可领取的任务"), 409);
    const other = await apiRequest("/api/v1/me", { method: "POST", body: {} }).then(
      () => assert.fail("expected rejection"),
      (e: unknown) => e,
    );
    assert.ok(isApiError(other));
    assert.equal(claimFailureBranch(other), "show-empty");
  });

  test("request id falls back to the X-Request-ID response header", async () => {
    const body = JSON.stringify({
      error: { code: "INTERNAL_ERROR", message: "服务暂不可用" }, // no request_id
    });
    stubFetch(body, 500, { [REQUEST_ID_HEADER]: "hdr-abc" });

    const error = await apiRequest("/api/v1/me").then(
      () => assert.fail("expected rejection"),
      (e: unknown) => e,
    );
    assert.ok(isApiError(error));
    assert.equal(error.requestId, "hdr-abc");
  });

  test("non-envelope body degrades to system HTTP_ERROR with a safe message", async () => {
    stubFetch("<html>Bad Gateway leak</html>", 502);

    const error = await apiRequest("/api/v1/me").then(
      () => assert.fail("expected rejection"),
      (e: unknown) => e,
    );
    assert.ok(isApiError(error));
    assert.equal(error.code, "HTTP_ERROR");
    assert.equal(error.status, 502);
    assert.ok(isSystemErrorCode(error.code));
    assert.ok(!error.message.includes("Bad Gateway")); // never echo raw body
  });

  test("unknown envelope code is preserved verbatim for diagnostics", async () => {
    stubFetch(envelope("BRAND_NEW_CODE", "未来新增的错误"), 400);

    const error = await apiRequest("/api/v1/me").then(
      () => assert.fail("expected rejection"),
      (e: unknown) => e,
    );
    assert.ok(isApiError(error));
    assert.equal(error.code, "BRAND_NEW_CODE");
    assert.equal(isKnownErrorCode(error.code), false);
    assert.equal(claimFailureBranch(error), "generic");
  });

  test("401 maps to AUTHENTICATION_REQUIRED (the session hook's anonymous signal)", async () => {
    stubFetch(envelope("AUTHENTICATION_REQUIRED", "未登录或登录状态已失效"), 401);

    const error = await apiRequest("/api/v1/me").then(
      () => assert.fail("expected rejection"),
      (e: unknown) => e,
    );
    assert.ok(isApiError(error));
    assert.equal(error.status, 401);
    assert.equal(error.code, "AUTHENTICATION_REQUIRED");
  });
});

describe("apiRequest request contract", () => {
  test("sends cookies, JSON content type, and the CSRF header on mutations", async () => {
    stubFetch(JSON.stringify({ ok: true }), 200);
    const globe = globalThis as { document?: unknown };
    globe.document = { cookie: "theme=dark; csrf_token=tok-9; other=1" };
    try {
      await apiRequest("/api/v1/me/nickname", {
        method: "PATCH",
        body: { nickname: "新昵称" },
      });
      assert.ok(recorded);
      assert.equal(recorded.credentials, "include");
      assert.equal(recorded.headers.get("Content-Type"), "application/json");
      assert.equal(recorded.headers.get("X-CSRF-Token"), "tok-9");
      assert.equal(recorded.method, "PATCH");
    } finally {
      delete globe.document;
    }
  });

  test("GET sends no CSRF header and no body", async () => {
    stubFetch(JSON.stringify({ ok: true }), 200);
    await apiRequest("/api/v1/me");
    assert.ok(recorded);
    assert.equal(recorded.headers.get("X-CSRF-Token"), null);
    assert.equal(recorded.credentials, "include");
  });

  test("forwards init.requestId as X-Request-ID", async () => {
    stubFetch(JSON.stringify({ ok: true }), 200);
    await apiRequest("/api/v1/me", { requestId: "incoming-42" });
    assert.ok(recorded);
    assert.equal(recorded.headers.get(REQUEST_ID_HEADER), "incoming-42");
  });

  test("returns the decoded JSON body on success", async () => {
    stubFetch(JSON.stringify({ id: "u-1", nickname: "小明" }), 200);
    const me = await apiRequest<{ id: string; nickname: string }>("/api/v1/me");
    assert.deepEqual(me, { id: "u-1", nickname: "小明" });
  });

  test("204 resolves to undefined", async () => {
    stubFetch("", 204);
    const result = await apiRequest<void>("/api/v1/me/email/unbind", { method: "POST" });
    assert.equal(result, undefined);
  });
});
