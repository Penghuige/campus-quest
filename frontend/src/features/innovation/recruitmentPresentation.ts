import { isApiError } from "@/lib/errors";
import { BUSINESS_TIME_CONFIG, parseServerInstant } from "@/lib/time";
import type { RecruitmentActionDto, RecruitmentDto } from "./recruitmentApi";
export function recruitmentDateTime(value: string): string {
  return new Intl.DateTimeFormat(BUSINESS_TIME_CONFIG.locale, { timeZone: BUSINESS_TIME_CONFIG.timeZone, year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(parseServerInstant(value)));
}
export function recruitmentStatusText(status: RecruitmentDto["status"], moderation: RecruitmentDto["moderation_state"]): string {
  if (moderation === "TAKEN_DOWN") return "已下架";
  return { DRAFT: "未发布", OPEN: "正在招募", CLOSED: "已关闭", EXPIRED: "已截止" }[status];
}
export function recruitmentError(cause: unknown) {
  const api = isApiError(cause);
  const unknown = !api || cause.status >= 500;
  const reload = api && cause.code === "CONFLICT";
  return { unknown, reload, message: unknown ? "结果尚未确认。请重试原请求，期间保留相同内容。" : reload ? "版本或状态已改变。你的输入已保留，请先读取最新版本。" : api ? cause.message : "操作失败，请重试。" };
}
type Receipt = Pick<RecruitmentActionDto, "replayed" | "applied_version"> & { current: Pick<RecruitmentDto, "version" | "status" | "moderation_state"> };
export function recruitmentReceiptText(receipt: Receipt): string {
  return `${receipt.replayed ? "已确认原请求结果。" : "操作已完成。"}当前${recruitmentStatusText(receipt.current.status, receipt.current.moderation_state)}。`;
}
