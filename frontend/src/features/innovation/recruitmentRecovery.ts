import { getAuthEpoch, onAuthEpochChange } from "@/lib/accessToken";
import type { RecruitmentDto } from "./recruitmentApi";
import type { RecruitmentForm } from "./recruitmentForm";

export type RecruitmentRecovery = { epoch: number; record: RecruitmentDto | null; form: RecruitmentForm; newDefaults: RecruitmentForm };

// Written by the mounted client editor only. Never serialize contact values
// into browser storage, history, URLs or requests merely to retain input.
const recoveries = new Map<string, RecruitmentRecovery>();
onAuthEpochChange(() => recoveries.clear());
export function getRecruitmentRecovery(projectId: string): RecruitmentRecovery | undefined {
  return recoveries.get(projectId);
}
export function rememberRecruitmentRecovery(projectId: string, snapshot: RecruitmentRecovery): void {
  if (snapshot.epoch === getAuthEpoch()) recoveries.set(projectId, snapshot);
}
export function clearRecruitmentRecovery(projectId: string, epoch: number, expectedForm?: RecruitmentForm): void {
  const snapshot = recoveries.get(projectId);
  // A retried command can finish before its original response. That late
  // response owns its input object, never a newer editor's unsaved snapshot.
  if (snapshot?.epoch === epoch && (!expectedForm || snapshot.form === expectedForm)) recoveries.delete(projectId);
}
