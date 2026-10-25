import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { beginAuthTransition, endAuthTransition, getAuthEpoch, onAuthEpochChange, recordLogin, recordLogout, receiveCrossTabResetForTests, setAccessToken } from "../lib/accessToken";
import { emptyRecruitmentForm, normalizeRecruitmentForm } from "../features/innovation/recruitmentForm";
import { clearPendingRecruitment, getPendingRecruitment, rememberPendingRecruitment, type PendingRecruitment } from "../features/innovation/recruitmentPending";

afterEach(() => { endAuthTransition(); recordLogout(); });
function snapshot(): PendingRecruitment {
  const form = emptyRecruitmentForm();
  form.contacts = [{ channel: "WECHAT", value: "PRIVATE_CONTACT_SENTINEL" }];
  return { epoch: getAuthEpoch(), command: { kind: "save", id: null, body: { request_id: "original-key", fields: normalizeRecruitmentForm(form) } }, record: null, form, confirmation: null };
}
test("unknown work is isolated by project and retains the exact original command", () => {
  const value = snapshot(); rememberPendingRecruitment("p", value);
  assert.equal(getPendingRecruitment("other"), undefined);
  assert.equal(getPendingRecruitment("p"), value);
  setAccessToken("same-account-refresh");
  assert.equal(getPendingRecruitment("p")?.command.body.request_id, "original-key");
  clearPendingRecruitment("p", value.epoch + 1, "original-key");
  clearPendingRecruitment("p", value.epoch, "different-key");
  assert.equal(getPendingRecruitment("p"), value);
  clearPendingRecruitment("p", value.epoch, "original-key");
  assert.equal(getPendingRecruitment("p"), undefined);
  clearPendingRecruitment("p", value.epoch, "original-key");
});
test("every local, sibling and pre-request account transition drops private work", async () => {
  for (const transition of [recordLogout, () => recordLogin("other-account"), receiveCrossTabResetForTests, beginAuthTransition]) {
    const old = snapshot(); rememberPendingRecruitment("p", old);
    await transition();
    assert.equal(getPendingRecruitment("p"), undefined);
    rememberPendingRecruitment("p", old);
    assert.equal(getPendingRecruitment("p"), undefined, "late response must not restore old contacts");
    endAuthTransition();
  }
});
test("auth epoch subscriptions can be removed without interfering with privacy cleanup", () => {
  let changes = 0; const off = onAuthEpochChange(() => { changes++; });
  recordLogout(); off(); recordLogin("new-account");
  assert.equal(changes, 1);
});
