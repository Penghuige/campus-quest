/**
 * Staff-invitation issuance page (spec §5.8 step 1; the §8 Admin
 * surface) — view-decision pins for the pure module
 * `features/admin/staffInvitationView.ts`:
 *
 * - link building joins the DEPLOYMENT MOUNT prefix through the shared
 *   apiBase contract ("" in dev/CI, "/campus" in production) — the
 *   copyable one-time link must never hardcode either;
 * - the email gate mirrors the backend's minimal shape check
 *   (`staff_service._normalized_email`): a client-side rejection must
 *   never reach the POST, and every server-rejected shape must also be
 *   client-rejected;
 * - error copy branches on `error.code` only (the §29 rule): 409
 *   USERNAME_ALREADY_EXISTS gets the business copy ("该邮箱已是注册账
 *   号") — the branch exists on the ACCEPT side today and lands on
 *   create when the backend adds the pre-check (reviewer ruling
 *   2026-10-15); RATE_LIMITED names the 20/hour admin bucket.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";

import {
  buildInvitationLink,
  describeInvitationError,
  INVITATION_ROLE_OPTIONS,
  validateInvitationEmail,
} from "../features/admin/staffInvitationView";

function apiError(
  code: string,
  message: string,
  status: number,
): ApiError {
  return new ApiError({ code, message, details: null, status, requestId: "req-1" });
}

describe("invitation role options (spec §5.8)", () => {
  test("TEACHER is the default-first option; ADMIN is the only other role", () => {
    assert.deepEqual(
      INVITATION_ROLE_OPTIONS.map((option) => option.value),
      ["TEACHER", "ADMIN"],
    );
    for (const option of INVITATION_ROLE_OPTIONS) {
      assert.equal(typeof option.label, "string");
      assert.ok(option.label.length > 0);
    }
  });
});

describe("validateInvitationEmail (the backend shape mirror)", () => {
  test("accepts a plain address and one wrapped in whitespace", () => {
    assert.equal(validateInvitationEmail("new.teacher@example.edu"), undefined);
    assert.equal(validateInvitationEmail("  new.teacher@example.edu  "), undefined);
  });

  test("rejects every server-rejected shape", () => {
    // Empty / whitespace-only / no @ / leading @ / trailing @ / over 320.
    assert.ok(validateInvitationEmail("") !== undefined);
    assert.ok(validateInvitationEmail("   ") !== undefined);
    assert.ok(validateInvitationEmail("no-at-sign.example") !== undefined);
    assert.ok(validateInvitationEmail("@example.edu") !== undefined);
    assert.ok(validateInvitationEmail("new.teacher@") !== undefined);
    const tooLong = `${"a".repeat(310)}@example.edu`;
    assert.equal(tooLong.length, 322);
    assert.ok(validateInvitationEmail(tooLong) !== undefined);
  });

  test("the rejection copy is actionable Chinese, identical across shapes", () => {
    assert.equal(
      validateInvitationEmail("no-at-sign.example"),
      validateInvitationEmail(""),
    );
    assert.ok(
      String(validateInvitationEmail("no-at-sign.example")).length > 0,
    );
  });
});

describe("buildInvitationLink (the one-time copyable URL)", () => {
  test("dev/CI mount: no prefix between origin and /staff/invite/", () => {
    assert.equal(
      buildInvitationLink("https://localhost:3000", "", "tok_abc123"),
      "https://localhost:3000/staff/invite/tok_abc123",
    );
  });

  test("production mount: the /campus prefix rides before /staff/invite/", () => {
    assert.equal(
      buildInvitationLink("https://scnu.example.edu", "/campus", "tok_abc123"),
      "https://scnu.example.edu/campus/staff/invite/tok_abc123",
    );
  });

  test("the token passes through verbatim (URL-safe alphabet, no re-encoding)", () => {
    const token = "AbC_-012";
    assert.ok(buildInvitationLink("https://h", "", token).endsWith(`/staff/invite/${token}`));
  });
});

describe("describeInvitationError (code-branched copy, §29)", () => {
  test("409 USERNAME_ALREADY_EXISTS gets the already-registered business copy", () => {
    const view = describeInvitationError(
      apiError("USERNAME_ALREADY_EXISTS", "该邮箱已被其他账号使用", 409),
      "创建失败，请稍后重试",
    );
    assert.equal(view.message, "该邮箱已是注册账号，不能重复邀请");
    assert.equal(view.requestId, "req-1");
  });

  test("RATE_LIMITED names the per-admin hourly bucket", () => {
    const view = describeInvitationError(
      apiError("RATE_LIMITED", "请求过于频繁", 429),
      "创建失败，请稍后重试",
    );
    assert.match(view.message, /每小时最多 20 个/);
    assert.equal(view.requestId, "req-1");
  });

  test("VALIDATION_ERROR and every other typed refusal surface the backend message", () => {
    const validation = describeInvitationError(
      apiError("VALIDATION_ERROR", "员工邀请信息校验失败", 400),
      "创建失败，请稍后重试",
    );
    assert.equal(validation.message, "员工邀请信息校验失败");

    const permission = describeInvitationError(
      apiError("PERMISSION_DENIED", "仅管理员可以创建员工邀请", 403),
      "创建失败，请稍后重试",
    );
    assert.equal(permission.message, "仅管理员可以创建员工邀请");
  });

  test("transport failures get the network line and no request id", () => {
    const view = describeInvitationError(new TypeError("fetch failed"), "创建失败，请稍后重试");
    assert.equal(view.message, "网络异常，请检查连接后重试");
    assert.equal(view.requestId, null);
  });
});
