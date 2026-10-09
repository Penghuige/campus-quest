import { describeSectionError, isApiError } from "@/lib/errors";
import type { OwnerQualificationDto } from "./qualificationApi";

export interface QualificationView { label: string; hint: string; actionLabel: string | null; canApply: boolean }
export interface QualificationMutationError { message: string; requestId: string | null; reload: boolean }

export function ownerQualificationView(record: OwnerQualificationDto, profileVersion: number | null, dirty: boolean): QualificationView {
  if (record.status === "APPROVED") {
    return { label: "负责人资格已开通", hint: "负责人资格已由管理员开通。成果仍需单独核实；修改本人资料不会自动撤销资格。", actionLabel: null, canApply: false };
  }
  const pending = record.status === "PENDING";
  const label = pending ? "负责人资格等待管理员开通" : "未申请负责人资格";
  const actionLabel = pending ? "更新资格申请" : "申请负责人资格";
  if (dirty) return { label, hint: "有未保存的资料修改，请先保存后再申请。申请只提交已保存的资料。", actionLabel, canApply: false };
  if (profileVersion === null) return { label, hint: "请先保存姓名、学号、专业和年级，再申请负责人资格。", actionLabel, canApply: false };
  if (pending && profileVersion <= (record.profile_version ?? 0)) {
    return { label, hint: "管理员将查看本次已提交的资料快照。请等待开通；更新并保存资料后可更新申请。", actionLabel, canApply: false };
  }
  return { label, hint: pending ? "已有新保存的资料。更新申请后，管理员将查看新的资料快照。" : "申请将提交当前已保存的四项资料，等待管理员人工开通。", actionLabel, canApply: true };
}

export function qualificationMutationError(error: unknown): QualificationMutationError {
  if (!isApiError(error)) return { message: "尚未确认资格操作结果，请重新读取资格状态后再决定。", requestId: null, reload: true };
  if (error.code === "CONFLICT") return { message: "资格申请或资料版本已改变，或本次操作重复。请重新读取资格状态后再决定。", requestId: error.requestId, reload: true };
  return { ...describeSectionError(error), reload: error.status >= 500 };
}
