import { describeSectionError, isApiError } from "@/lib/errors";
import type { OwnerFields } from "./ownerApi";

export const OWNER_FIELDS = [
  { key: "name", label: "姓名", limit: 80 },
  { key: "student_no", label: "学号", limit: 40 },
  { key: "major", label: "专业", limit: 120 },
  { key: "grade", label: "年级", limit: 40 },
] as const;
export const EMPTY_OWNER_FIELDS: OwnerFields = { name: "", student_no: "", major: "", grade: "" };
export type OwnerFieldErrors = Partial<Record<keyof OwnerFields, string>>;

export function normalizeOwnerFields(fields: OwnerFields): OwnerFields {
  return { name: fields.name.trim(), student_no: fields.student_no.trim(), major: fields.major.trim(), grade: fields.grade.trim() };
}

export function ownerFields(fields: OwnerFields): OwnerFields {
  return { name: fields.name, student_no: fields.student_no, major: fields.major, grade: fields.grade };
}

export function validateOwnerFields(fields: OwnerFields): OwnerFieldErrors {
  const errors: OwnerFieldErrors = {};
  for (const { key, label, limit } of OWNER_FIELDS) {
    const value = fields[key].trim();
    if (!value) errors[key] = `请输入${label}`;
    else if (Array.from(value).length > limit) errors[key] = `${label}不能超过 ${limit} 个字符`;
  }
  return errors;
}

export interface OwnerSaveError { message: string; requestId: string | null; reload: boolean }

export function describeOwnerSaveError(error: unknown): OwnerSaveError {
  if (isApiError(error)) {
    if (error.code === "OWNER_PROFILE_VERSION_CONFLICT") return { message: "资料已在其他页面保存。你的未保存内容已保留，请读取最新资料后再决定如何修改。", requestId: null, reload: true };
    if (error.code === "VALIDATION_ERROR") return { message: "内容未通过校验，请检查四项资料是否完整以及长度是否超限。", requestId: null, reload: false };
    if (error.code === "PERMISSION_DENIED" || error.code === "ACCOUNT_NOT_ACTIVE") return { message: "仅正常状态的学生账号可以管理自己的负责人资料。", requestId: null, reload: false };
    const view = describeSectionError(error);
    // A server failure can occur after commit; reconcile rather than assume failure.
    return { ...view, reload: error.status >= 500 };
  }
  return { message: "网络异常，尚未确认保存结果。当前输入已保留，请读取最新资料确认是否已保存。", requestId: null, reload: true };
}
