/**
 * Task 8 (Plan 09): staff auth surface — pure logic pins.
 *
 * Covers the four test duties of the brief:
 * - invitation form validation (password band + the repeat field);
 * - TOTP code format (exactly 6 ASCII digits) and the login second-factor
 *   rule (6-digit code OR one recovery code);
 * - the recovery-codes ONE-TIME display state machine: shown ->
 *   confirmed-seen -> never re-fetchable client-side (no exported
 *   transition restores the codes; no storage path exists);
 * - staff-login error mapping incl. TOTP_SETUP_REQUIRED and the
 *   per-surface AUTHENTICATION_REQUIRED overrides (same envelope code,
 *   three different user-facing meanings);
 * - the QR/secret display choice: TOTP_QR_STRATEGY pins text-and-copy,
 *   and the provisioning view passes the backend URI through verbatim.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";
import {
  TOTP_QR_STRATEGY,
  confirmRecoveryCodesSeen,
  copyAllRecoveryCodesText,
  describeStaffError,
  recoveryCodesIssued,
  totpProvisioningView,
  validateInvitePassword,
  validateInvitePasswordRepeat,
  validateSecondFactor,
  validateStaffEmail,
  validateTotpCode,
} from "../features/auth/staffAuthView";

function apiError(code: string, message = "backend text", requestId: string | null = null): ApiError {
  return new ApiError({ code, message, status: 400, details: null, requestId });
}

// --- invitation form validation ---------------------------------------------

describe("validateStaffEmail (login identifier)", () => {
  test("accepts a normal address and trims outer whitespace", () => {
    assert.equal(validateStaffEmail("  Teacher@School.edu.cn "), null);
  });

  test("rejects empty and obviously malformed addresses", () => {
    assert.match(validateStaffEmail("")!, /请输入邮箱/);
    assert.match(validateStaffEmail("not-an-email")!, /请输入正确的邮箱地址/);
    assert.match(validateStaffEmail("a@b")!, /请输入正确的邮箱地址/);
    assert.match(validateStaffEmail("a b@c.d")!, /请输入正确的邮箱地址/);
  });

  test("rejects the backend's 320-char ceiling (mirror band)", () => {
    const over = `a${"x".repeat(315)}@b.cd`; // 320 chars total -> too long
    assert.ok(over.length > 320);
    assert.match(validateStaffEmail(over)!, /请输入正确的邮箱地址/);
  });
});

describe("invitation password step", () => {
  test("the shared 10-128 band governs the password field", () => {
    assert.match(validateInvitePassword("short")!, /10-128/);
    assert.equal(validateInvitePassword("correct-horse-battery"), null);
    assert.equal(validateInvitePassword("x".repeat(129)) !== null, true);
  });

  test("the repeat field: empty and mismatch are client-only concerns", () => {
    assert.match(validateInvitePasswordRepeat("correct-horse", "")!, /请再次输入密码/);
    assert.match(validateInvitePasswordRepeat("correct-horse", "different-horse")!, /不一致/);
    assert.equal(validateInvitePasswordRepeat("correct-horse", "correct-horse"), null);
  });
});

// --- TOTP / second-factor formats --------------------------------------------

describe("validateTotpCode (setup confirm step)", () => {
  test("exactly 6 ASCII digits pass; outer whitespace tolerated", () => {
    assert.equal(validateTotpCode("012345"), null);
    assert.equal(validateTotpCode("  012345\n"), null);
  });

  test("5 or 7 digits, letters, and full-width digits fail", () => {
    assert.match(validateTotpCode("01234")!, /6 位数字/);
    assert.match(validateTotpCode("0123456")!, /6 位数字/);
    assert.match(validateTotpCode("012a45")!, /6 位数字/);
    assert.match(validateTotpCode("１２３４５６")!, /6 位数字/); // full-width
    assert.match(validateTotpCode("")!, /请输入/);
  });
});

describe("validateSecondFactor (staff login field)", () => {
  test("a 6-digit TOTP code passes", () => {
    assert.equal(validateSecondFactor("234567"), null);
  });

  test("a recovery code (backend shape 0f1e2-d3c4b) passes", () => {
    assert.equal(validateSecondFactor("0f1e2-d3c4b"), null);
    // Case-insensitive input: the backend's uniform rejection owns the
    // final verdict; convenience checks must not invent a stricter band.
    assert.equal(validateSecondFactor("0F1E2-D3C4B"), null);
  });

  test("garbage and empty fail with the both-formats hint", () => {
    assert.match(validateSecondFactor("")!, /动态验证码或恢复代码/);
    assert.match(validateSecondFactor("0f1e2d3c4b")!, /恢复代码/); // missing dash
    assert.match(validateSecondFactor("0123456789")!, /恢复代码/); // 10 digits
  });
});

// --- per-surface error mapping -----------------------------------------------

describe("describeStaffError (AUTHENTICATION_REQUIRED per surface)", () => {
  test("invite-accept: uniform dead-token copy for the 401", () => {
    const view = describeStaffError(apiError("AUTHENTICATION_REQUIRED"), "invite-accept");
    assert.equal(view.summary, "邀请链接无效或已被使用");
    assert.deepEqual(view.fieldErrors, {});
  });

  test("staff-login: wrong credentials copy, NEVER the student 学号 copy", () => {
    const view = describeStaffError(apiError("AUTHENTICATION_REQUIRED"), "staff-login");
    assert.equal(view.summary, "邮箱、密码或动态验证码错误");
    assert.notEqual(view.summary, "学号或密码不正确");
  });

  test("totp-confirm: wrong-code copy keeps the retry safe", () => {
    const view = describeStaffError(apiError("AUTHENTICATION_REQUIRED"), "totp-confirm");
    assert.match(view.summary!, /动态验证码错误/);
  });

  test("totp-session: dead pending-session bearer gets its own copy", () => {
    const view = describeStaffError(apiError("AUTHENTICATION_REQUIRED"), "totp-session");
    assert.match(view.summary!, /绑定会话已失效/);
  });
});

describe("describeStaffError (other envelope tiers delegate)", () => {
  test("TOTP_SETUP_REQUIRED at login maps to actionable copy", () => {
    const view = describeStaffError(apiError("TOTP_SETUP_REQUIRED"), "staff-login");
    assert.match(view.summary!, /尚未完成动态口令绑定/);
  });

  test("USERNAME_ALREADY_EXISTS at invite keeps the backend's zh copy", () => {
    const view = describeStaffError(
      apiError("USERNAME_ALREADY_EXISTS", "该邮箱已被其他账号使用"),
      "invite-accept",
    );
    assert.equal(view.summary, "该邮箱已被其他账号使用");
  });

  test("VALIDATION_ERROR password band lands on the password field", () => {
    const error = new ApiError({
      code: "VALIDATION_ERROR",
      message: "员工邀请信息校验失败",
      status: 400,
      details: { field: "password", reason: "password length must be 10-128" },
    });
    const view = describeStaffError(error, "invite-accept");
    assert.deepEqual(view.fieldErrors, { password: "密码长度需为 10-128 个字符" });
  });

  test("an unknown code degrades to the generic line + request id", () => {
    const view = describeStaffError(apiError("BRAND_NEW", "x", "req-88"), "staff-login");
    assert.equal(view.summary, "操作失败，请稍后重试");
    assert.equal(view.requestId, "req-88");
  });
});

// --- the one-time recovery-codes display state machine ------------------------

describe("recovery codes: one-time display state machine", () => {
  const issued = ["0f1e2-d3c4b", "1a2b3-c4d5e", "f0e1d-2c3b4", "a1b2c-3d4e5"];

  test("issued codes enter the shown phase exactly once", () => {
    const state = recoveryCodesIssued([...issued]);
    assert.equal(state.phase, "shown");
    assert.deepEqual(state.codes, issued);
  });

  test("confirmed-seen drops the plaintext list — irreversibly", () => {
    const confirmed = confirmRecoveryCodesSeen(recoveryCodesIssued([...issued]));
    assert.equal(confirmed.phase, "confirmed");
    assert.equal(confirmed.codes, null);
    // Every re-application of the ONLY exported exit transition keeps
    // codes null: there is no path back to "shown".
    const again = confirmRecoveryCodesSeen(confirmed);
    assert.equal(again.phase, "confirmed");
    assert.equal(again.codes, null);
  });

  test("no exported transition can re-issue codes once confirmed", () => {
    const confirmed = confirmRecoveryCodesSeen(recoveryCodesIssued([...issued]));
    // The module's whole transition alphabet applied to `confirmed`:
    for (const next of [
      confirmRecoveryCodesSeen(confirmed),
      confirmRecoveryCodesSeen(confirmRecoveryCodesSeen(confirmed)),
    ]) {
      assert.equal(next.codes, null);
      assert.notEqual(next.phase, "shown");
    }
  });

  test("copy-all payload is one code per line, exactly as displayed", () => {
    assert.equal(copyAllRecoveryCodesText(issued), issued.join("\n"));
  });
});

// --- QR / secret display choice -----------------------------------------------

describe("TOTP provisioning display (the no-QR choice)", () => {
  const setup = {
    secret: "JBSWY3DPEHPK3PXP",
    otpauth_uri:
      "otpauth://totp/CampusQuest:teacher%40school.edu?secret=JBSWY3DPEHPK3PXP&issuer=CampusQuest",
  };

  test("the strategy is pinned to text-and-copy (no QR dependency)", () => {
    const view = totpProvisioningView(setup);
    assert.equal(view.strategy, TOTP_QR_STRATEGY);
    assert.equal(TOTP_QR_STRATEGY, "text-and-copy");
  });

  test("the otpauth URI passes through verbatim; the label parses for manual entry", () => {
    const view = totpProvisioningView(setup);
    assert.equal(view.otpauthUri, setup.otpauth_uri); // byte-for-byte
    assert.equal(view.accountLabel, "CampusQuest:teacher@school.edu");
    assert.equal(view.secret, setup.secret);
  });

  test("a malformed URI degrades to label=null; secret and raw URI stay visible", () => {
    const view = totpProvisioningView({ secret: "ABC234", otpauth_uri: "not a uri://" });
    assert.equal(view.accountLabel, null);
    assert.equal(view.otpauthUri, "not a uri://");
    assert.equal(view.secret, "ABC234");
  });
});
