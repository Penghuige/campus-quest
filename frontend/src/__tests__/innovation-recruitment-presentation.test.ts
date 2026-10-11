import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../lib/errors";
import { recruitmentStatusText, recruitmentError, recruitmentReceiptText } from "../features/innovation/recruitmentPresentation";
import { recruitmentNotificationLink, notificationEventView } from "../features/notifications/inboxView";
test("states and moderation use visible words", () => {
  for (const state of ["DRAFT", "OPEN", "CLOSED", "EXPIRED"] as const) assert.ok(recruitmentStatusText(state, "NORMAL"));
  assert.equal(recruitmentStatusText("OPEN", "TAKEN_DOWN"), "已下架");
});
test("unknown outcome retains exact request and known conflicts request a refresh", () => {
  assert.equal(recruitmentError(new Error("lost ACK")).unknown, true);
  assert.equal(recruitmentError(new ApiError({ status: 503, code: "INTERNAL_ERROR", message: "temporary" })).unknown, true);
  assert.equal(recruitmentError(new ApiError({ status: 409, code: "CONFLICT", message: "stale" })).reload, true);
  for (const status of [403, 404, 422, 429]) assert.equal(recruitmentError(new ApiError({ status, code: "VALIDATION_ERROR", message: "denied" })).unknown, false);
});
test("old action receipt displays current state instead of resurrecting applied state", () => {
  assert.match(recruitmentReceiptText({ replayed: true, applied_version: 2, current: { version: 5, status: "CLOSED", moderation_state: "NORMAL" } }), /当前.*已关闭/);
});
test("recruitment reminder links only to its fixed local management route", () => {
  const path = "/profile/project-drafts/11111111-1111-4111-8111-111111111111/recruitments";
  assert.equal(notificationEventView("IE_RECRUITMENT_CONFIRMATION_DUE").label, "招募确认");
  assert.equal(recruitmentNotificationLink("IE_RECRUITMENT_CONFIRMATION_DUE", `请前往 ${path} 确认继续`), path);
  assert.equal(recruitmentNotificationLink("ACCOUNT_SECURITY", path), null);
  assert.equal(recruitmentNotificationLink("IE_RECRUITMENT_CONFIRMATION_DUE", `https://evil.test${path}`), null);
});
