import { describeSectionError, isApiError } from "@/lib/errors";
import type { DraftFields, ProjectDraftDto } from "./api";

export const DRAFT_FIELDS = [
  { key: "title", label: "项目名称", limit: 120, rows: 1, hint: "必填；其余内容可以稍后补充。" },
  { key: "summary", label: "项目简介", limit: 2000, rows: 5, hint: "简要描述你希望推进的项目。" },
  { key: "direction", label: "项目方向", limit: 120, rows: 1, hint: "例如环保、教育、技术创新。" },
  { key: "stage", label: "项目阶段", limit: 80, rows: 1, hint: "例如想法阶段、调研中；按实际情况填写。" },
  { key: "team_status", label: "团队现状", limit: 1000, rows: 4, hint: "可以记录已有成员与当前分工，也可以暂时留空。" },
] as const;

export type DraftFieldErrors = Partial<Record<keyof DraftFields, string>>;
export const EMPTY_DRAFT_FIELDS: DraftFields = { title: "", summary: "", direction: "", stage: "", team_status: "" };

export function draftFields(draft: ProjectDraftDto): DraftFields {
  return { title: draft.title, summary: draft.summary, direction: draft.direction, stage: draft.stage, team_status: draft.team_status };
}

export function normalizeDraftFields(fields: DraftFields): DraftFields {
  return { title: fields.title.trim(), summary: fields.summary.trim(), direction: fields.direction.trim(), stage: fields.stage.trim(), team_status: fields.team_status.trim() };
}

export function codePointCount(value: string): number {
  return Array.from(value).length;
}

export function validateDraftFields(fields: DraftFields): DraftFieldErrors {
  const normalized = normalizeDraftFields(fields);
  const errors: DraftFieldErrors = {};
  if (!normalized.title) errors.title = "请输入项目名称";
  for (const field of DRAFT_FIELDS) {
    if (codePointCount(normalized[field.key]) > field.limit) {
      errors[field.key] = `${field.label}不能超过 ${field.limit} 个字符`;
    }
  }
  return errors;
}

export function sameDraftFields(a: DraftFields, b: DraftFields): boolean {
  return DRAFT_FIELDS.every(({ key }) => a[key] === b[key]);
}

export interface DraftSaveError {
  message: string;
  requestId: string | null;
  reload: boolean;
}

export function describeDraftSaveError(error: unknown): DraftSaveError {
  if (isApiError(error)) {
    switch (error.code) {
      case "PROJECT_DRAFT_VERSION_CONFLICT":
        return { message: "草稿已在其他页面更新。你的未保存内容已保留，请读取最新版本后再决定如何修改。", requestId: null, reload: true };
      case "PROJECT_DRAFT_REQUEST_CONFLICT":
        return { message: "这次新建请求已保存过不同内容。你的输入已保留，请返回草稿列表确认原草稿后继续编辑。", requestId: null, reload: false };
      case "PERMISSION_DENIED":
      case "ACCOUNT_NOT_ACTIVE":
        return { message: "仅正常状态的学生账号可以管理自己的项目草稿。", requestId: null, reload: false };
      case "NOT_FOUND":
        return { message: "草稿不存在或当前账号无权访问。你的输入已保留。", requestId: null, reload: false };
      case "VALIDATION_ERROR":
        return { message: "内容未通过校验，请检查各字段的长度后重试。", requestId: null, reload: false };
    }
  }
  const view = describeSectionError(error);
  return { ...view, message: isApiError(error) ? view.message : "网络异常，尚未确认保存结果。请重试保存；重复提交不会重复新建草稿。", reload: false };
}
