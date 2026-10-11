import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyRecruitmentForm, normalizeRecruitmentForm, validateRecruitmentForm, toRecruitmentForm, localDeadlineToUtc } from "../features/innovation/recruitmentForm";

const complete = () => ({ ...emptyRecruitmentForm(), project_title: "模拟项目", direction: "学习", stage: "构想", introduction: "模拟资料", idea_stage: true as const, goal: "验证", initial_plan: "访谈", role_name: "研究伙伴", headcount: "1", duties: "访谈", requirements: "愿意参与", time_commitment: "每周两小时", beginner_friendly: false as const, contacts: [{ channel: "WECHAT" as const, value: "demo_partner" }] });
test("an incomplete draft is saveable, while publication needs explicit choices and consent", () => {
  assert.deepEqual(validateRecruitmentForm(emptyRecruitmentForm(), false, 0), {});
  const errors = validateRecruitmentForm(emptyRecruitmentForm(), true, 0);
  assert.ok(errors.idea_stage && errors.beginner_friendly && errors.contacts && errors.headcount);
  assert.deepEqual(validateRecruitmentForm(complete(), true, 0), {});
});
test("headcount boundaries and Unicode lengths", () => {
  for (const count of ["1", "999"]) assert.deepEqual(validateRecruitmentForm({ ...complete(), headcount: count }, true, 0), {});
  for (const count of ["0", "1000", "1.5", "1e2", "-1", "abc"]) assert.ok(validateRecruitmentForm({ ...complete(), headcount: count }, false, 0).headcount);
  assert.deepEqual(validateRecruitmentForm({ ...complete(), project_title: "😀".repeat(120) }, true, 0), {});
  assert.ok(validateRecruitmentForm({ ...complete(), project_title: "😀".repeat(121) }, false, 0).project_title);
});
test("idea start needs goal/plan and unselected account contacts never appear", () => {
  assert.ok(validateRecruitmentForm({ ...complete(), goal: "" }, true, 0).goal);
  assert.deepEqual(validateRecruitmentForm({ ...complete(), idea_stage: false, goal: "", initial_plan: "" }, true, 0), {});
  assert.equal(normalizeRecruitmentForm(emptyRecruitmentForm()).contacts?.length, 0);
  const values = normalizeRecruitmentForm({ ...complete(), contacts: [{ channel: "QQ", value: "12345678" }, { channel: "EMAIL", value: "demo@example.com" }, { channel: "PHONE", value: "13800138000" }, { channel: "WECHAT", value: "demo_partner" }] });
  assert.equal(values.contacts?.length, 4);
  assert.ok(validateRecruitmentForm({ ...complete(), contacts: [{ channel: "WECHAT", value: "" }] }, false, 0).contacts);
});
test("business local deadline is timezone explicit and preserves an instant", () => {
  assert.equal(localDeadlineToUtc("2027-01-01T08:00"), "2027-01-01T00:00:00.000Z");
  assert.throws(() => localDeadlineToUtc("2027-02-30T08:00"), RangeError);
  assert.ok(validateRecruitmentForm({ ...complete(), deadline_local: "2027-01-01T08:00" }, true, Date.parse("2027-01-01T00:00Z")).deadline_local);
  assert.equal(toRecruitmentForm({ ...normalizeRecruitmentForm(complete()), deadline_at: "2027-01-01T00:00:00Z" }).deadline_local, "2027-01-01T08:00");
});
