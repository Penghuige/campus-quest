import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyRecruitmentForm, normalizeRecruitmentForm, validateRecruitmentForm, toRecruitmentForm, localDeadlineToUtc, initialRecruitmentForm, chooseRecruitmentStage, hasRecruitmentChanges, invalidRecruitmentContactChannels } from "../features/innovation/recruitmentForm";

const complete = () => ({ ...emptyRecruitmentForm(), project_title: "模拟项目", direction: "学习", stage: "构想", introduction: "模拟资料", idea_stage: true as const, goal: "验证", initial_plan: "访谈", role_name: "研究伙伴", headcount: "1", duties: "访谈", requirements: "愿意参与", time_commitment: "每周两小时", beginner_friendly: false as const, contacts: [{ channel: "WECHAT" as const, value: "demo_partner" }] });
test("new roles reuse project context without implied stage choice or contact consent", () => {
  const form = initialRecruitmentForm({ title: "模拟项目", direction: "教育", stage: "调研中", summary: "模拟访谈" });
  assert.equal(form.project_title, "模拟项目"); assert.equal(form.direction, "教育");
  assert.equal(form.stage, "调研中"); assert.equal(form.introduction, "模拟访谈");
  assert.equal(form.idea_stage, ""); assert.deepEqual(form.contacts, []);
  assert.equal(initialRecruitmentForm().project_title, "");
  assert.deepEqual(initialRecruitmentForm().contacts, []);
});
test("a unified stage choice writes coherent idea fields and preserves other stage details", () => {
  const idea = chooseRecruitmentStage({ ...complete(), stage: "调研中", idea_stage: false }, "true");
  assert.equal(idea.stage, "创意起步"); assert.equal(idea.idea_stage, true);
  const other = chooseRecruitmentStage(idea, "false");
  assert.equal(other.idea_stage, false); assert.equal(other.stage, "调研中");
  assert.equal(validateRecruitmentForm(other, true, 0).stage, undefined);
  assert.equal(chooseRecruitmentStage({ ...other, stage: "原型验证" }, "false").stage, "原型验证");
  const unselected = chooseRecruitmentStage({ ...other, stage: "原型验证" }, "");
  assert.equal(unselected.idea_stage, ""); assert.equal(unselected.stage, "原型验证");
});
test("reading a legacy idea role does not rewrite its saved stage description", () => {
  const form = toRecruitmentForm(normalizeRecruitmentForm(complete()));
  assert.equal(form.stage, "构想"); assert.equal(form.idea_stage, true);
  assert.equal(normalizeRecruitmentForm(form).stage, "构想");
});
test("comparing stage categories preserves each description without putting hidden notes in the request", () => {
  const other = { ...complete(), idea_stage: false as const, stage: "已访谈十二人，正在验证原型" };
  const idea = chooseRecruitmentStage(other, "true");
  const restored = chooseRecruitmentStage(idea, "false");
  assert.equal(restored.stage, "已访谈十二人，正在验证原型");
  assert.equal(restored.idea_stage, false);
  assert.ok(!JSON.stringify(normalizeRecruitmentForm(idea)).includes("已访谈十二人"));
  const legacy = { ...complete(), stage: "原来的构想说明" };
  assert.equal(chooseRecruitmentStage(chooseRecruitmentStage(legacy, "false"), "true").stage, "原来的构想说明");
  assert.equal(chooseRecruitmentStage(chooseRecruitmentStage(idea, ""), "false").stage, "已访谈十二人，正在验证原型");
});
test("an unfinished saved idea draft can explicitly choose its stage", () => {
  const selected = chooseRecruitmentStage({ ...complete(), stage: "" }, "true");
  assert.equal(selected.idea_stage, true); assert.equal(selected.stage, "创意起步");
  assert.equal(validateRecruitmentForm(selected, true, 0).stage, undefined);
});
test("temporary stage notes do not make unchanged visible content dirty", () => {
  const original = { ...complete(), idea_stage: false as const, stage: "原型验证" };
  const restored = chooseRecruitmentStage(chooseRecruitmentStage(original, "true"), "false");
  assert.equal(hasRecruitmentChanges(restored, original), false);
  assert.equal(hasRecruitmentChanges({ ...restored, role_name: "新岗位" }, original), true);
});
test("invalid selected channels are identified without rejecting valid selected values", () => {
  assert.deepEqual(invalidRecruitmentContactChannels([
    { channel: "WECHAT", value: "demo_partner" }, { channel: "QQ", value: "not-a-number" },
    { channel: "EMAIL", value: "invalid-email" }, { channel: "PHONE", value: "13800138000" },
  ]), ["QQ", "EMAIL"]);
  assert.deepEqual(invalidRecruitmentContactChannels([]), []);
  assert.deepEqual(invalidRecruitmentContactChannels([{ channel: "QQ", value: "12345678" }, { channel: "QQ", value: "23456789" }]), ["QQ"]);
});
test("publication cannot call the literal idea stage a different stage", () => {
  const errors = validateRecruitmentForm({ ...complete(), stage: "创意起步", idea_stage: false }, true, 0);
  assert.ok(errors.stage);
});
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
