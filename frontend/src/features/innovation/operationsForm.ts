import { describeSectionError, isApiError } from "@/lib/errors";

export function operationsMutationError(error: unknown): { message: string; requestId: string | null; reload: boolean } {
  if (!isApiError(error)) return { message: "尚未确认操作结果，请重新读取授权状态后再决定。", requestId: null, reload: true };
  if (error.code === "CONFLICT") return { message: "授权状态已改变或操作重复，请重新读取后再决定。", requestId: error.requestId ?? null, reload: true };
  return { ...describeSectionError(error), reload: error.status >= 500 };
}
