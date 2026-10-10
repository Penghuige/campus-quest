import { isApiError } from "@/lib/errors";
import type { WorkflowDto, EvidenceDto, PublicAchievementDto } from "./reviewApi";

export function evidenceFileError(file: Blob): string | null {
  if (!["application/pdf", "image/png", "image/jpeg"].includes(file.type)) return "请选择 PDF、PNG 或 JPEG 文件。";
  if (file.size < 1 || file.size > 10 * 1024 * 1024) return "单份证明须大于 0 字节，且不超过 10 MiB。";
  return null;
}
export function reviewStatusText(state: WorkflowDto["first_review_state"], current?: WorkflowDto["review_case"], moderation: WorkflowDto["moderation_state"] = "NORMAL") {
  if (current?.operation === "UPDATE") {
    if (current.status === "APPROVED") return "更新复审已通过";
    const status = ({ SUBMITTED: "等待更新复审", RETURNED: "更新已退回", WITHDRAWN: "更新已撤回" })[current.status];
    return `${status}，${moderation === "TAKEN_DOWN" ? "旧通过版本保持下架" : "旧通过版本继续公开"}`;
  }
  return ({ DRAFT: "尚未提交首次核实", SUBMITTED: "等待首次核实", RETURNED: "已退回，修改后可重新提交", APPROVED: "首次核实已通过" })[state];
}
export function isReviewPending(workflow: Pick<WorkflowDto, "first_review_state" | "review_case">) {
  return workflow.first_review_state === "SUBMITTED" || workflow.review_case?.status === "SUBMITTED";
}
export function publicReviewText(item: Pick<PublicAchievementDto, "updated_after_first_review" | "latest_reviewed_at">) {
  if (!item.updated_after_first_review) return "当前为首次核实通过的版本。";
  return item.latest_reviewed_at ? "当前为更新复审通过的版本。" : "历史更新内容，未逐项复审。";
}
export const evidenceStatusText = (state: EvidenceDto["state"]) => ({ PENDING: "等待上传或重新检查", CHECKING: "文件检查中", READY: "文件检查通过", REJECTED: "文件检查未通过" })[state];
export function reviewError(cause: unknown): string {
  const message = reviewErrorMessage(cause);
  return isApiError(cause) && cause.requestId ? `${message} 请求 ID：${cause.requestId}` : message;
}
function reviewErrorMessage(cause: unknown): string {
  if (isApiError(cause)) {
    if (cause.status === 403) return "当前账号没有操作权限，请确认负责人资格或运营授权。";
    if (cause.status === 404) return "该记录目前不可访问，请重新读取列表。";
    if (cause.status === 409) return "记录状态已改变，或材料已被历史版本引用。请重新读取状态后再操作。";
    if (cause.status === 422) return "请检查已保存的负责人四项资料、项目五项概况、成果说明及已检查通过的证明。";
    if (cause.status === 503) return "服务暂时不可用，操作结果尚未确认。未完成检查的材料尚未获准使用；请重新读取或沿用原请求重试。";
  }
  return "操作结果尚未确认。请先重新读取状态；重试必须沿用这次请求，不要重复新建。";
}
