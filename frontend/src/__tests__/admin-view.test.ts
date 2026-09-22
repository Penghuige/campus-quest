/**
 * Task 10 (Plan 09): the admin workspace's PURE decision/view core
 * (`features/admin/adminView.ts`) — the pins the six pages render
 * through, in the teacherView/teacher-view precedent's shape.
 *
 * Focus points (the brief's contract pins):
 * - `CONFLICT` vs `VALIDATION_ERROR` vs transport failures take
 *   different copy through `describeAdminMutationError`, branched on
 *   `error.code` ONLY (the §29 rule; the Chinese message is display
 *   material and never parsed);
 * - the whitelist import's collision list extraction
 *   (`details.student_numbers`) and counts/confirm derivation;
 * - the account-status action affordances mirror spec §5.7 (incl.
 *   PENDING_PHONE having no admin transition);
 * - the settings stored-form parsers round-trip the backend's canonical
 *   spellings (JSON array / decimal text / "true"/"false" /
 *   comma-separated CIDRs) and degrade on drift;
 * - the reward edit body is DIFF-ONLY under the backend's presence
 *   semantics (absent = unchanged, explicit null clears a bound).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { ApiError } from "../lib/errors";

import type { RewardCatalogueDto } from "../features/admin/adminApi";
import {
  accountActions,
  adminReasonReady,
  canConfirmWhitelistImport,
  conflictStudentNumbers,
  describeAdminMutationError,
  parseAbandonLimitValue,
  parseCidrsSettingValue,
  parseEmojiSettingValue,
  parseNetworkEnabledValue,
  redemptionActions,
  redemptionStatusView,
  rewardFormToCreateBody,
  rewardFormToUpdateBody,
  roleLabel,
  SETTING_KEY_VIEWS,
  settingDiffText,
  settingValueText,
  userStatusView,
  whitelistCodeView,
  whitelistCountsText,
  EMPTY_REWARD_FORM,
  rewardFormFromCatalogue,
  type RewardFormValues,
} from "../features/admin/adminView";

function apiError(
  code: string,
  message: string,
  details: unknown = null,
  status = 409,
): ApiError {
  return new ApiError({ code, message, details, status, requestId: "req-1" });
}

describe("describeAdminMutationError (the CONFLICT-vs-validation contract)", () => {
  test("a 409 CONFLICT takes the concurrent/state-conflict copy and flag", () => {
    const view = describeAdminMutationError(
      apiError("CONFLICT", "部分学号已存在于白名单，未导入任何行"),
      "fallback",
    );
    assert.equal(view.conflict, true);
    assert.match(view.message, /部分学号已存在于白名单/);
    assert.match(view.message, /与当前状态冲突/);
    assert.match(view.message, /请刷新后重试/);
    assert.equal(view.requestId, "req-1");
  });

  test("a 422 VALIDATION_ERROR shows the backend's own message, no conflict flag", () => {
    const view = describeAdminMutationError(
      apiError("VALIDATION_ERROR", "系统设置 EMOJI_WHITELIST 不合法", null, 422),
      "fallback",
    );
    assert.equal(view.conflict, false);
    assert.equal(view.message, "系统设置 EMOJI_WHITELIST 不合法");
  });

  test("a missing-envelope failure degrades to the network line", () => {
    const view = describeAdminMutationError(new TypeError("fetch failed"), "fallback");
    assert.deepEqual(view, { message: "网络异常，请检查连接后重试", requestId: null, conflict: false });
  });

  test("conflictStudentNumbers extracts the deterministic sorted collision list", () => {
    const error = apiError("CONFLICT", "冲突", { student_numbers: ["20260002", "20260001"] });
    assert.deepEqual(conflictStudentNumbers(error), ["20260002", "20260001"]);
    // Non-conflict failures, conflicts without the list, and malformed
    // lists all answer empty — never a throw.
    assert.deepEqual(conflictStudentNumbers(apiError("VALIDATION_ERROR", "x", null, 400)), []);
    assert.deepEqual(conflictStudentNumbers(apiError("CONFLICT", "c", { other: 1 })), []);
    assert.deepEqual(
      conflictStudentNumbers(apiError("CONFLICT", "c", { student_numbers: [1, "2"] })),
      ["2"],
    );
  });
});

describe("account directory affordances (spec §5.7)", () => {
  test("status wording covers the four states with non-color tones", () => {
    assert.deepEqual(userStatusView("ACTIVE"), { label: "正常", tone: "success" });
    assert.deepEqual(userStatusView("SUSPENDED"), { label: "已停用", tone: "warning" });
    assert.deepEqual(userStatusView("BANNED"), { label: "已封禁", tone: "danger" });
    assert.deepEqual(userStatusView("PENDING_PHONE"), { label: "待验证", tone: "muted" });
    assert.deepEqual(userStatusView("WEIRD"), { label: "未知状态", tone: "muted" });
  });

  test("role labels cover the closed set", () => {
    assert.equal(roleLabel("STUDENT"), "学生");
    assert.equal(roleLabel("TEACHER"), "教师");
    assert.equal(roleLabel("ADMIN"), "管理员");
  });

  test("ACTIVE offers suspend+ban; SUSPENDED/BANNED offer reactivate; PENDING_PHONE none", () => {
    assert.deepEqual(
      accountActions("ACTIVE").map((action) => action.kind),
      ["suspend", "ban"],
    );
    assert.deepEqual(
      accountActions("SUSPENDED").map((action) => action.kind),
      ["reactivate"],
    );
    assert.deepEqual(
      accountActions("BANNED").map((action) => action.kind),
      ["reactivate"],
    );
    assert.deepEqual(accountActions("PENDING_PHONE"), []);
  });

  test("the blank-after-trim reason mirror", () => {
    assert.equal(adminReasonReady("  "), false);
    assert.equal(adminReasonReady(" 依据 "), true);
  });
});

describe("whitelist preview views", () => {
  test("row-code wording covers the backend WhitelistRowCode vocabulary", () => {
    assert.deepEqual(whitelistCodeView("IMPORTABLE"), { label: "可导入", tone: "success" });
    assert.deepEqual(whitelistCodeView("DUPLICATE_IN_FILE"), {
      label: "文件内重复",
      tone: "warning",
    });
    assert.deepEqual(whitelistCodeView("DUPLICATE_IN_DB"), {
      label: "已在白名单",
      tone: "muted",
    });
    assert.deepEqual(whitelistCodeView("FULL_WIDTH_DIGITS"), {
      label: "全角数字",
      tone: "warning",
    });
    assert.deepEqual(whitelistCodeView("INVALID_CHARACTERS"), {
      label: "含非法字符",
      tone: "danger",
    });
    assert.deepEqual(whitelistCodeView("INVALID_LENGTH"), {
      label: "长度不合法",
      tone: "warning",
    });
    // Drift degrades to the raw code, never a throw.
    assert.deepEqual(whitelistCodeView("SOMETHING_NEW"), {
      label: "SOMETHING_NEW",
      tone: "muted",
    });
  });

  test("counts text lists non-zero problem classes only", () => {
    assert.equal(
      whitelistCountsText({
        total_rows: 12,
        importable: 9,
        duplicate_in_file: 1,
        duplicate_in_db: 2,
        full_width_digits: 0,
        invalid_characters: 0,
        invalid_length: 0,
      }),
      "共 12 行：可导入 9 行，文件内重复 1 行，已在白名单 2 行",
    );
    assert.equal(
      whitelistCountsText({
        total_rows: 2,
        importable: 2,
        duplicate_in_file: 0,
        duplicate_in_db: 0,
        full_width_digits: 0,
        invalid_characters: 0,
        invalid_length: 0,
      }),
      "共 2 行：可导入 2 行",
    );
  });

  test("confirm needs a non-empty importable set bound by its digest", () => {
    const base = {
      total_rows: 1,
      decisions: [],
      counts: {
        total_rows: 1,
        importable: 1,
        duplicate_in_file: 0,
        duplicate_in_db: 0,
        full_width_digits: 0,
        invalid_characters: 0,
        invalid_length: 0,
      },
    };
    assert.equal(
      canConfirmWhitelistImport({ ...base, importable: ["20260001"], confirm_token: "d" }),
      true,
    );
    assert.equal(
      canConfirmWhitelistImport({ ...base, importable: [], confirm_token: "d" }),
      false,
    );
    assert.equal(
      canConfirmWhitelistImport({ ...base, importable: ["20260001"], confirm_token: " " }),
      false,
    );
  });
});

describe("redemption queue affordances (spec §16.2)", () => {
  test("status wording and action sets follow the state machine", () => {
    assert.deepEqual(redemptionStatusView("REQUESTED"), { label: "待审核", tone: "info" });
    assert.deepEqual(redemptionStatusView("APPROVED"), {
      label: "已批准 · 待发放",
      tone: "warning",
    });
    assert.deepEqual(redemptionActions("REQUESTED"), ["approve", "reject"]);
    assert.deepEqual(redemptionActions("UNDER_REVIEW"), ["approve", "reject"]);
    assert.deepEqual(redemptionActions("APPROVED"), ["fulfill"]);
    assert.deepEqual(redemptionActions("FULFILLED"), []);
    assert.deepEqual(redemptionActions("REJECTED"), []);
  });
});

describe("settings stored-form parsers (backend system/service.py canonical forms)", () => {
  test("emoji whitelist: JSON array round-trip; null and drift degrade to empty", () => {
    assert.deepEqual(parseEmojiSettingValue('["🎉","👏"]'), ["🎉", "👏"]);
    assert.deepEqual(parseEmojiSettingValue(null), []);
    assert.deepEqual(parseEmojiSettingValue("not json"), []);
    assert.deepEqual(parseEmojiSettingValue('["a",1]'), ["a"]);
    assert.deepEqual(parseEmojiSettingValue('"str"'), []);
  });

  test("abandon limit: decimal text -> number; anything else null", () => {
    assert.equal(parseAbandonLimitValue("3"), 3);
    assert.equal(parseAbandonLimitValue("0"), 0);
    assert.equal(parseAbandonLimitValue(null), null);
    assert.equal(parseAbandonLimitValue("x"), null);
  });

  test("network enabled: only the canonical true/false spellings parse", () => {
    assert.equal(parseNetworkEnabledValue("true"), true);
    assert.equal(parseNetworkEnabledValue("false"), false);
    assert.equal(parseNetworkEnabledValue(null), null);
    assert.equal(parseNetworkEnabledValue("True"), null);
  });

  test("CIDRs: comma-separated canonical split with blank filtering", () => {
    assert.deepEqual(parseCidrsSettingValue("10.0.0.0/8,192.168.0.0/16"), [
      "10.0.0.0/8",
      "192.168.0.0/16",
    ]);
    assert.deepEqual(parseCidrsSettingValue(null), []);
    assert.deepEqual(parseCidrsSettingValue(""), []);
  });

  test("value preview lines are per-key human forms", () => {
    assert.equal(settingValueText("CURRENT_ACADEMIC_TERM", "2026-2027-1"), "2026-2027-1");
    assert.equal(settingValueText("CURRENT_ACADEMIC_TERM", null), "未设置（使用部署默认值）");
    assert.equal(settingValueText("EMOJI_WHITELIST", '["🎉"]'), "🎉");
    assert.equal(settingValueText("EMOJI_WHITELIST", "[]"), "（空列表：全部表情禁用）");
    assert.equal(settingValueText("MANAGEMENT_NETWORK_ENABLED", "true"), "已开启");
    assert.equal(settingValueText("MANAGEMENT_NETWORK_CIDRS", "10.0.0.0/8"), "10.0.0.0/8");
  });

  test("the registry renders exactly the five registered keys in order", () => {
    assert.deepEqual(
      SETTING_KEY_VIEWS.map((view) => view.key),
      [
        "CURRENT_ACADEMIC_TERM",
        "EMOJI_WHITELIST",
        "ABANDON_DAILY_LIMIT",
        "MANAGEMENT_NETWORK_ENABLED",
        "MANAGEMENT_NETWORK_CIDRS",
      ],
    );
  });

  test("the confirm diff shows old -> new", () => {
    assert.equal(settingDiffText("old", "new"), "“old” → “new”");
    assert.match(settingDiffText(null, "new"), /未设置/);
  });
});

describe("reward catalogue form bodies (presence-based edit contract)", () => {
  const row: RewardCatalogueDto = {
    id: "r1",
    name: "文创礼包",
    description: "含贴纸",
    point_cost: 100,
    stock: 10,
    per_user_term_limit: 1,
    available_from: "2026-09-01T00:00:00.000Z",
    available_until: null,
    window_open: true,
  };

  function toLocal(ms: number): string {
    const date = new Date(ms);
    const pad = (value: number) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  const base: RewardFormValues = {
    ...EMPTY_REWARD_FORM,
    ...rewardFormFromCatalogue(row, toLocal),
    reason: "调价",
  };

  test("prefill + no edits produce a body with only the reason (diff-only)", () => {
    const result = rewardFormToUpdateBody(base, row);
    assert.equal(result.ok, true);
    assert.deepEqual(result.body, { reason: "调价" });
  });

  test("changed values ride; a cleared held bound sends explicit null", () => {
    const result = rewardFormToUpdateBody(
      { ...base, pointCost: "120", stock: "" },
      row,
    );
    assert.equal(result.ok, true);
    assert.equal(result.body?.point_cost, 120);
    assert.equal(result.body?.stock, null);
    // Unchanged bounds stay ABSENT (not re-sent).
    assert.equal(result.body?.per_user_term_limit, undefined);
    assert.equal(result.body?.available_from, undefined);
  });

  test("an empty bound the row never held stays absent (no phantom clear)", () => {
    const result = rewardFormToUpdateBody({ ...base, availableUntilLocal: "" }, row);
    assert.equal(result.ok, true);
    assert.equal(result.body?.available_until, undefined);
  });

  test("a blank reason refuses the update even when fields are valid", () => {
    const result = rewardFormToUpdateBody({ ...base, reason: "  " }, row);
    assert.equal(result.ok, false);
    assert.equal(result.body, undefined);
  });

  test("create sends every managed field with tz-aware datetimes", () => {
    const result = rewardFormToCreateBody({
      ...base,
      pointCost: "100",
      availableFromLocal: "2026-09-01T08:00",
      availableUntilLocal: "",
    });
    assert.equal(result.ok, true);
    assert.equal(result.body?.point_cost, 100);
    assert.equal(result.body?.stock, 10);
    // Symmetric-parse assertion (zone-independent): the wire value must
    // carry an explicit UTC offset AND denote the same instant the
    // datetime-local string names in the browser's zone.
    assert.match(result.body?.available_from ?? "", /Z$/);
    assert.equal(
      Date.parse(result.body?.available_from ?? ""),
      new Date("2026-09-01T08:00").getTime(),
    );
    assert.equal(result.body?.available_until, null);
    assert.equal(result.body?.reason, "调价");
  });

  test("band checks block obvious mistakes before any request leaves", () => {
    const result = rewardFormToCreateBody({ ...base, pointCost: "0" });
    assert.equal(result.ok, false);
    assert.equal(result.errors.pointCost, "兑换积分必须大于 0");
  });
});
