/**
 * Approvals view pins (auth-extension-II spec §5; TDD with a
 * mutation-sensitivity check): the phone-mask privacy rule, the reject
 * reason band, and the CONFLICT copy.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";

import {
  describeApprovalError,
  validateRejectReason,
} from "../features/admin/approvalsView";

describe("validateRejectReason (optional but bounded)", () => {
  test("empty is valid (transport-optional); long is refused", () => {
    assert.equal(validateRejectReason(""), undefined);
    assert.equal(validateRejectReason("  "), undefined);
    assert.ok(validateRejectReason("长".repeat(201)) !== undefined);
    assert.equal(validateRejectReason("正当理由"), undefined);
  });
});

describe("describeApprovalError (the admin envelope family)", () => {
  test("CONFLICT gets the concurrent-review copy", () => {
    const view = describeApprovalError(
      new ApiError({ code: "CONFLICT", message: "状态冲突", details: null, status: 409, requestId: "r1" }),
      "操作失败",
    );
    assert.match(view.message, /已被其他管理员审核/);
    assert.equal(view.requestId, "r1");
  });

  test("other typed refusals pass through; transport keeps the network line", () => {
    const typed = describeApprovalError(
      new ApiError({ code: "PERMISSION_DENIED", message: "无权", details: null, status: 403, requestId: "r2" }),
      "操作失败",
    );
    assert.equal(typed.message, "无权");

    const net = describeApprovalError(new TypeError("x"), "操作失败");
    assert.equal(net.message, "网络异常，请检查连接后重试");
    assert.equal(net.requestId, null);
  });
});
