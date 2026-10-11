import { getAuthEpoch, onAuthEpochChange } from "@/lib/accessToken";
import type { RecruitmentDto, ContactChannel, RecruitmentFieldsDto, VersionCommand, RevokeCommand } from "./recruitmentApi";
import type { RecruitmentForm } from "./recruitmentForm";

export type RecruitmentAction = "publish" | "close" | "reopen" | "confirm" | "revoke";
export type PendingRecruitmentCommand = { kind: "save"; id: string | null; body: { request_id: string; version?: number; fields: RecruitmentFieldsDto } } | { kind: RecruitmentAction; id: string; body: VersionCommand | RevokeCommand };
export type RecruitmentConfirmation = { kind: RecruitmentAction; channel?: ContactChannel; completed: boolean };
export type PendingRecruitment = { epoch: number; command: PendingRecruitmentCommand; record: RecruitmentDto | null; form: RecruitmentForm; confirmation: RecruitmentConfirmation | null };

// Tab memory only: route changes may unmount an editor while its receipt is
// unknown. Never persist contacts or replay a previous account's command.
const pending = new Map<string, PendingRecruitment>();
onAuthEpochChange(() => pending.clear());

export function getPendingRecruitment(projectId: string): PendingRecruitment | undefined {
  return pending.get(projectId);
}
export function rememberPendingRecruitment(projectId: string, snapshot: PendingRecruitment): void {
  if (snapshot.epoch === getAuthEpoch()) pending.set(projectId, snapshot);
}
export function clearPendingRecruitment(projectId: string, epoch: number, requestId: string): void {
  const snapshot = pending.get(projectId);
  if (snapshot?.epoch === epoch && snapshot.command.body.request_id === requestId) pending.delete(projectId);
}
