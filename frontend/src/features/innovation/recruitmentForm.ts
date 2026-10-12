import { BUSINESS_TIME_CONFIG } from "@/lib/time";
import type { ContactChannel, RecruitmentFieldsDto } from "./recruitmentApi";
import type { ProjectDraftDto } from "./api";
import { codePointCount } from "./draftForm";

export const RECRUITMENT_TEXT_FIELDS = [
  { key: "project_title", label: "项目名称", limit: 120, rows: 1 },
  { key: "direction", label: "项目方向", limit: 120, rows: 1 },
  { key: "stage", label: "项目阶段", limit: 80, rows: 1 },
  { key: "introduction", label: "项目介绍", limit: 2000, rows: 4 },
  { key: "goal", label: "阶段目标", limit: 2000, rows: 3 },
  { key: "initial_plan", label: "初步方案", limit: 2000, rows: 3 },
  { key: "role_name", label: "岗位名称", limit: 120, rows: 1 },
  { key: "duties", label: "主要职责", limit: 2000, rows: 3 },
  { key: "requirements", label: "能力要求", limit: 2000, rows: 3 },
  { key: "time_commitment", label: "时间投入", limit: 500, rows: 2 },
] as const;
type TextKey = typeof RECRUITMENT_TEXT_FIELDS[number]["key"];
export type RecruitmentForm = Record<TextKey, string> & {
  headcount: string;
  idea_stage: boolean | "";
  beginner_friendly: boolean | "";
  deadline_local: string;
  contacts: { channel: ContactChannel; value: string }[];
  /** Temporary alternative descriptions; excluded from API fields and dirty checks. */
  stage_notes?: Partial<Record<"idea" | "other", string>>;
};
export type RecruitmentFormErrors = Partial<Record<keyof RecruitmentForm, string>>;
export function emptyRecruitmentForm(): RecruitmentForm {
  return { project_title: "", direction: "", stage: "", introduction: "", goal: "", initial_plan: "", role_name: "", duties: "", requirements: "", time_commitment: "", headcount: "", idea_stage: "", beginner_friendly: "", deadline_local: "", contacts: [] };
}
export type RecruitmentProjectDefaults = Pick<ProjectDraftDto, "title" | "summary" | "direction" | "stage">;
export function initialRecruitmentForm(project?: RecruitmentProjectDefaults): RecruitmentForm {
  const form = emptyRecruitmentForm();
  if (!project) return form;
  return { ...form, project_title: project.title, direction: project.direction, stage: project.stage, introduction: project.summary };
}
export function chooseRecruitmentStage(form: RecruitmentForm, choice: string): RecruitmentForm {
  const notes = { ...form.stage_notes };
  if (form.idea_stage === true) notes.idea = form.stage;
  else if (form.idea_stage === false || !form.stage_notes) notes.other = form.stage;
  if (choice === "true") return { ...form, idea_stage: true, stage: notes.idea ?? "创意起步", stage_notes: notes };
  if (choice === "false") return { ...form, idea_stage: false, stage: notes.other ?? "", stage_notes: notes };
  return { ...form, idea_stage: "", stage_notes: notes };
}
export function hasRecruitmentChanges(form: RecruitmentForm, original: RecruitmentForm): boolean {
  const rawFields = (value: RecruitmentForm) => JSON.stringify(value, (key, field: unknown) => key === "stage_notes" ? undefined : field);
  return rawFields(form) !== rawFields(original);
}
export function invalidRecruitmentContactChannels(contacts: RecruitmentForm["contacts"]): ContactChannel[] {
  const seen = new Set<ContactChannel>();
  const invalid = new Set<ContactChannel>();
  for (const { channel, value } of contacts) {
    const trimmed = value.trim();
    const valid = channel === "WECHAT" ? /^[A-Za-z][A-Za-z0-9_-]{5,19}$/.test(trimmed) : channel === "QQ" ? /^[1-9][0-9]{4,19}$/.test(trimmed) : channel === "EMAIL" ? /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed) : /^(?:\+?[0-9][0-9 ()-]{5,24})$/.test(trimmed);
    if (!valid || codePointCount(trimmed) > 254 || seen.has(channel)) invalid.add(channel);
    seen.add(channel);
  }
  return [...invalid];
}
function wallParts(instant: number): number[] {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: BUSINESS_TIME_CONFIG.timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(instant);
  return ["year", "month", "day", "hour", "minute"].map((type) => Number(parts.find((part) => part.type === type)?.value));
}
function wallUtc(parts: number[]): number { return Date.UTC(parts[0], parts[1] - 1, parts[2], parts[3], parts[4]); }
/** datetime-local is a business wall time, never the browser's system timezone. */
export function localDeadlineToUtc(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) throw new RangeError("请选择完整截止时间。");
  const parts = match.slice(1).map(Number);
  const target = wallUtc(parts);
  const calendar = new Date(target);
  if (parts[0] < 100 || calendar.getUTCFullYear() !== parts[0] || calendar.getUTCMonth() + 1 !== parts[1] || calendar.getUTCDate() !== parts[2] || calendar.getUTCHours() !== parts[3] || calendar.getUTCMinutes() !== parts[4]) throw new RangeError("截止时间无效。");
  let instant = target;
  for (let i = 0; i < 3; i++) instant += target - wallUtc(wallParts(instant));
  if (wallParts(instant).some((part, i) => part !== parts[i])) throw new RangeError("该时区中不存在这个时间。");
  return new Date(instant).toISOString();
}
export function toRecruitmentForm(fields: RecruitmentFieldsDto): RecruitmentForm {
  const form = emptyRecruitmentForm();
  for (const { key } of RECRUITMENT_TEXT_FIELDS) form[key] = fields[key] ?? "";
  form.headcount = fields.headcount == null ? "" : String(fields.headcount);
  form.idea_stage = fields.idea_stage ?? "";
  form.beginner_friendly = fields.beginner_friendly ?? "";
  form.contacts = (fields.contacts ?? []).map((contact) => ({ ...contact }));
  if (fields.deadline_at) {
    const [year, month, day, hour, minute] = wallParts(Date.parse(fields.deadline_at));
    form.deadline_local = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}T${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  }
  return form;
}
export function normalizeRecruitmentForm(form: RecruitmentForm): RecruitmentFieldsDto {
  const fields: RecruitmentFieldsDto = { project_title: "", direction: "", stage: "", introduction: "", goal: "", initial_plan: "", role_name: "", duties: "", requirements: "", time_commitment: "" };
  for (const { key } of RECRUITMENT_TEXT_FIELDS) fields[key] = form[key].trim();
  return { ...fields, headcount: form.headcount.trim() ? Number(form.headcount) : null, idea_stage: form.idea_stage === "" ? null : form.idea_stage, beginner_friendly: form.beginner_friendly === "" ? null : form.beginner_friendly, deadline_at: form.deadline_local ? localDeadlineToUtc(form.deadline_local) : null, contacts: form.contacts.map((contact) => ({ ...contact, value: contact.value.trim() })) };
}
export function validateRecruitmentForm(form: RecruitmentForm, publication: boolean, now: number): RecruitmentFormErrors {
  const errors: RecruitmentFormErrors = {};
  for (const { key, label, limit } of RECRUITMENT_TEXT_FIELDS) {
    const required = publication && (!(["goal", "initial_plan"] as string[]).includes(key) || form.idea_stage === true);
    if (required && !form[key].trim()) errors[key] = `请填写${label}。`;
    else if (codePointCount(form[key].trim()) > limit) errors[key] = `最多 ${limit} 个字符。`;
  }
  if ((publication || form.headcount.trim()) && (!/^[1-9]\d{0,2}$/.test(form.headcount.trim()) || Number(form.headcount) > 999)) errors.headcount = "招募人数须为 1～999 的整数。";
  for (const key of ["idea_stage", "beginner_friendly"] as const) if (publication && form[key] === "") errors[key] = key === "idea_stage" ? "请选择项目阶段。" : "请明确选择是或否。";
  if (publication && form.idea_stage === false && form.stage.trim() === "创意起步") errors.stage = "阶段说明为创意起步，请在项目阶段中选择创意起步。";
  if (form.deadline_local) {
    try { if (publication && Date.parse(localDeadlineToUtc(form.deadline_local)) <= now) errors.deadline_local = "截止时间须晚于当前时间。"; }
    catch { errors.deadline_local = "请选择有效的截止时间。"; }
  }
  if (publication && !form.contacts.length) errors.contacts = "请至少选择并授权一个联系方式。";
  if (invalidRecruitmentContactChannels(form.contacts).length) errors.contacts = "请填写有效的授权联系值；每类渠道只选一个。";
  return errors;
}
