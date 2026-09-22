/**
 * Task 5 (Plan 09): redemption surface — typed conflict copy, shelf
 * states from server verdicts, lifecycle labels, the advisory
 * Idempotency-Key mint, and the redeem wrapper's wire contract against
 * a stubbed fetch transport (the api client's own test seam; no mock
 * library — patterns §1/§19 and the Task 2 precedent).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { redeemReward, type RewardItemDto } from "../features/points/api";
import {
  describeRedeemError,
  newIdempotencyKey,
  REDEEM_ERROR_TEXT,
  redemptionStatusView,
  rewardShelfView,
  rewardWindowLabel,
} from "../features/rewards/redeemView";
import { ApiError } from "../lib/errors";

type RecordedRequest = {
  url: string;
  method: string;
  headers: Headers;
  body: string | null;
};

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
}

function envelope(code: string, message: string, requestId: string | null = null) {
  return JSON.stringify({
    error: { code, message, details: null, request_id: requestId },
  });
}

function apiError(code: string, status = 409): ApiError {
  return new ApiError({ code, message: `server copy ${code}`, status });
}

function reward(overrides: Partial<RewardItemDto>): RewardItemDto {
  return {
    id: "44444444-4444-4444-8444-444444444444",
    name: "文创帆布包",
    description: null,
    point_cost: 300,
    stock: 5,
    per_user_term_limit: null,
    available_from: null,
    available_until: null,
    window_open: true,
    ...overrides,
  };
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

describe("reward shelf states (server verdicts verbatim)", () => {
  test("open window + stock -> redeemable with stock line", () => {
    const view = rewardShelfView(reward({ stock: 8 }));
    assert.equal(view.state, "redeemable");
    assert.equal(view.stateLabel, "可兑换");
    assert.equal(view.stockLabel, "剩余 8 件");
    assert.equal(view.windowLabel, null);
  });

  test("unbounded stock -> 库存充足; low stock -> 仅剩 wording", () => {
    assert.equal(rewardShelfView(reward({ stock: null })).stockLabel, "库存充足");
    assert.equal(rewardShelfView(reward({ stock: 3 })).stockLabel, "仅剩 3 件");
    assert.equal(rewardShelfView(reward({ stock: 5 })).stockLabel, "仅剩 5 件");
    assert.equal(rewardShelfView(reward({ stock: 6 })).stockLabel, "剩余 6 件");
  });

  test("zero stock -> out-of-stock state", () => {
    const view = rewardShelfView(reward({ stock: 0 }));
    assert.equal(view.state, "out-of-stock");
    assert.equal(view.stateLabel, "已兑完");
    assert.equal(view.stockLabel, null);
  });

  test("closed window wins over stock (the server's own gate order)", () => {
    const view = rewardShelfView(reward({ window_open: false, stock: 9 }));
    assert.equal(view.state, "window-closed");
    assert.equal(view.stateLabel, "暂不可兑换");
    // A still-stocked closed item keeps its stock line.
    assert.equal(view.stockLabel, "剩余 9 件");
  });

  test("window label formats one-sided and bounded windows", () => {
    const from = reward({
      available_from: "2026-09-20T02:00:00Z",
      available_until: "2026-09-30T18:00:00Z",
    });
    assert.match(rewardWindowLabel(from) ?? "", /兑换窗口/);
    assert.match(
      rewardWindowLabel(reward({ available_from: "2026-09-20T02:00:00Z" })) ?? "",
      /起可兑换$/,
    );
    assert.match(
      rewardWindowLabel(reward({ available_until: "2026-09-30T18:00:00Z" })) ?? "",
      /截止$/,
    );
  });
});

describe("typed conflict copy (branch on error.code, never message text)", () => {
  test("the three §16.1 gate codes map to distinct stable copy", () => {
    assert.equal(
      describeRedeemError(apiError("INSUFFICIENT_POINTS")).message,
      REDEEM_ERROR_TEXT.INSUFFICIENT_POINTS,
    );
    assert.equal(
      describeRedeemError(apiError("REWARD_OUT_OF_STOCK")).message,
      REDEEM_ERROR_TEXT.REWARD_OUT_OF_STOCK,
    );
    assert.equal(
      describeRedeemError(apiError("REDEMPTION_LIMIT_REACHED")).message,
      REDEEM_ERROR_TEXT.REDEMPTION_LIMIT_REACHED,
    );
    // The gate copy carries no request id (it is expected behavior).
    assert.equal(describeRedeemError(apiError("INSUFFICIENT_POINTS")).requestId, null);
  });

  test("system code -> infra copy + request id", () => {
    const view = describeRedeemError(apiError("INTERNAL_ERROR", 500));
    assert.equal(view.message, "服务暂时不可用，请稍后重试");
    assert.equal(view.requestId, null); // no envelope id in this ApiError
  });

  test("unknown code (registry drift) -> generic copy + request id", () => {
    const error = new ApiError({
      code: "SOME_FUTURE_CODE",
      message: "whatever",
      status: 409,
      requestId: "req-1",
    });
    const view = describeRedeemError(error);
    assert.equal(view.message, "兑换失败，请稍后重试");
    assert.equal(view.requestId, "req-1");
  });

  test("other known business code -> the backend's own message as fallback", () => {
    const view = describeRedeemError(apiError("PERMISSION_DENIED", 403));
    assert.equal(view.message, "server copy PERMISSION_DENIED");
  });

  test("network failure (non-ApiError) -> connectivity line", () => {
    assert.equal(
      describeRedeemError(new TypeError("fetch failed")).message,
      "网络异常，请检查连接后重试",
    );
  });
});

describe("redemption lifecycle labels (product wording, no raw enums)", () => {
  test("every backend status has zh-CN wording", () => {
    assert.deepEqual(redemptionStatusView("REQUESTED"), {
      label: "待审核",
      tone: "info",
    });
    assert.deepEqual(redemptionStatusView("UNDER_REVIEW"), {
      label: "审核中",
      tone: "info",
    });
    assert.deepEqual(redemptionStatusView("APPROVED"), {
      label: "已通过",
      tone: "success",
    });
    assert.deepEqual(redemptionStatusView("REJECTED"), {
      label: "未通过",
      tone: "danger",
    });
    assert.deepEqual(redemptionStatusView("FULFILLED"), {
      label: "已发放",
      tone: "success",
    });
  });

  test("unknown status degrades to a calm neutral, never the raw string", () => {
    const view = redemptionStatusView("SOMETHING_NEW");
    assert.equal(view.label, "处理中");
    assert.notEqual(view.label, "SOMETHING_NEW");
  });
});

describe("advisory Idempotency-Key mint (spec §32, V1 advisory)", () => {
  test("keys are UUID-shaped and unique per attempt", () => {
    const seen = new Set<string>();
    for (let index = 0; index < 50; index += 1) {
      const key = newIdempotencyKey();
      assert.match(
        key,
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
      );
      seen.add(key);
    }
    assert.equal(seen.size, 50);
  });
});

describe("redeem wrapper wire contract", () => {
  test("POSTs to the redeem path with the Idempotency-Key header", async () => {
    stubFetch(
      JSON.stringify({
        id: "55555555-5555-4555-8555-555555555555",
        reward_item_id: "44444444-4444-4444-8444-444444444444",
        status: "REQUESTED",
        points: 300,
        term_key: "2026-fall",
        created_at: "2026-09-21T08:00:00Z",
      }),
      201,
    );
    const redemption = await redeemReward("44444444-4444-4444-8444-444444444444", "key-1");
    assert.equal(recorded?.url, "/api/v1/rewards/44444444-4444-4444-8444-444444444444/redeem");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.headers.get("Idempotency-Key"), "key-1");
    assert.equal(redemption.status, "REQUESTED");
    assert.equal(redemption.points, 300);
  });

  test("a typed 409 envelope rejects as ApiError with the gate code", async () => {
    stubFetch(envelope("INSUFFICIENT_POINTS", "积分不足"), 409);
    await assert.rejects(
      redeemReward("44444444-4444-4444-8444-444444444444", "key-2"),
      (error: unknown) => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.code, "INSUFFICIENT_POINTS");
        assert.equal(error.status, 409);
        return true;
      },
    );
  });
});
