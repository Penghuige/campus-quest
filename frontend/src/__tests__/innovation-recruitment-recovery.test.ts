import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { beginAuthTransition, endAuthTransition, getAuthEpoch, recordLogin, recordLogout, receiveCrossTabResetForTests, setAccessToken } from "../lib/accessToken";
import { emptyRecruitmentForm } from "../features/innovation/recruitmentForm";
import { clearRecruitmentRecovery, getRecruitmentRecovery, rememberRecruitmentRecovery } from "../features/innovation/recruitmentRecovery";

afterEach(() => { endAuthTransition(); recordLogout(); });
const snapshot = () => ({ epoch: getAuthEpoch(), record: null, form: { ...emptyRecruitmentForm(), role_name: "unsaved", contacts: [{ channel: "EMAIL" as const, value: "qa@example.invalid" }] }, newDefaults: emptyRecruitmentForm() });
test("route recovery retains private input only for its project and current auth context", () => {
  const value = snapshot(); rememberRecruitmentRecovery("p", value);
  assert.equal(getRecruitmentRecovery("other"), undefined);
  assert.equal(getRecruitmentRecovery("p")?.form.role_name, "unsaved");
  setAccessToken("same-account-refresh");
  assert.equal(getRecruitmentRecovery("p")?.form.contacts[0].value, "qa@example.invalid");
  clearRecruitmentRecovery("p", value.epoch + 1);
  assert.ok(getRecruitmentRecovery("p"));
  clearRecruitmentRecovery("p", value.epoch);
  assert.equal(getRecruitmentRecovery("p"), undefined);
});
test("logout, login, sibling reset and pre-request auth transitions erase recovered contacts", async () => {
  for (const transition of [recordLogout, () => recordLogin("another-account"), receiveCrossTabResetForTests, beginAuthTransition]) {
    const old = snapshot(); rememberRecruitmentRecovery("p", old);
    assert.ok(getRecruitmentRecovery("p"));
    await transition(); assert.equal(getRecruitmentRecovery("p"), undefined);
    rememberRecruitmentRecovery("p", old);
    assert.equal(getRecruitmentRecovery("p"), undefined, "late old-epoch callback cannot restore contacts");
    endAuthTransition();
  }
});
test("a delayed completed command cannot erase a newer unsaved form in the same epoch", () => {
  const commandInput = snapshot(); rememberRecruitmentRecovery("p", commandInput);
  const newerInput = { ...snapshot(), form: { ...commandInput.form, role_name: "newer unsaved input" } };
  rememberRecruitmentRecovery("p", newerInput);
  clearRecruitmentRecovery("p", commandInput.epoch, commandInput.form);
  assert.equal(getRecruitmentRecovery("p"), newerInput);
  clearRecruitmentRecovery("p", newerInput.epoch, newerInput.form);
  assert.equal(getRecruitmentRecovery("p"), undefined);
});
