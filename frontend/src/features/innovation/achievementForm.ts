import { describeSectionError, isApiError } from "@/lib/errors";
import type { AchievementFields } from "./achievementApi";

export const ACHIEVEMENT_FIELDS = [
  { key: "title", label: "成果名称", limit: 120, rows: 1, hint: "必填；例如作品原型、调研报告或阶段成果。" },
  { key: "description", label: "作品与阶段成果说明", limit: 4000, rows: 6, hint: "按实际进展描述作品内容，可以稍后补充。" },
  { key: "work_url", label: "作品链接", limit: 2000, rows: 1, hint: "选填完整的 http 或 https 链接；平台不会自动访问。" },
  { key: "award_text", label: "立项或获奖说明", limit: 1000, rows: 4, hint: "选填；按实际情况填写，与项目阶段分开记录。" },
] as const;
export const EMPTY_ACHIEVEMENT: AchievementFields = { title: "", description: "", work_url: "", award_text: "" };
export type AchievementErrors = Partial<Record<keyof AchievementFields, string>>;

export function achievementFields<T extends AchievementFields>(value: T): AchievementFields {
  return { title: value.title, description: value.description, work_url: value.work_url, award_text: value.award_text };
}

export function normalizeAchievementFields(value: AchievementFields): AchievementFields {
  return { title: value.title.trim(), description: value.description.trim(), work_url: value.work_url.trim(), award_text: value.award_text.trim() };
}

export function validWorkLink(value: string): boolean {
  if (!/^https?:\/\/[^/]+/i.test(value) || /[\u0000-\u0020\u007f\\]/.test(value)) return false;
  try {
    const url = new URL(value);
    return Boolean(url.hostname) && !url.username && !url.password;
  } catch { return false; }
}

export function validateAchievementFields(value: AchievementFields): AchievementErrors {
  const fields = normalizeAchievementFields(value);
  const errors: AchievementErrors = {};
  if (!fields.title) errors.title = "请输入成果名称";
  for (const field of ACHIEVEMENT_FIELDS) {
    if (Array.from(fields[field.key]).length > field.limit) errors[field.key] = `${field.label}不能超过 ${field.limit} 个字符`;
  }
  if (fields.work_url && !validWorkLink(fields.work_url)) errors.work_url = "请输入不含账号密码、空白字符的完整 http 或 https 链接";
  return errors;
}

export interface AchievementSaveError {
  message: string;
  requestId: string | null;
  reload: boolean;
  retryCreate: boolean;
}

export function describeAchievementError(error: unknown, editing: boolean): AchievementSaveError {
  const view = describeSectionError(error);
  if (isApiError(error) && error.status < 500) {
    if (error.status === 409) return { message: editing ? "成果已在其他页面更新。当前输入已保留，请读取最新版本后再决定如何修改。" : "这次新建请求已保存过不同内容。当前输入已保留，请返回列表核对。", requestId: null, reload: editing, retryCreate: false };
    return { ...view, reload: false, retryCreate: false };
  }
  return { ...view, message: editing ? "尚未确认保存结果，当前输入已保留。请读取最新版本后再继续。" : "尚未确认新建结果。请重试这次请求，不会重复创建；也可以返回列表核对。", reload: editing, retryCreate: !editing };
}
