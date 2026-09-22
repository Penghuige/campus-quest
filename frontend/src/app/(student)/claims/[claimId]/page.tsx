import type { Metadata } from "next";

import { ClaimDetailView } from "@/features/submissions/ClaimDetailView";

export const metadata: Metadata = {
  title: "我的任务 · CampusQuest",
  description: "任务领取详情：分配的平台与关键词、提交、校验与修改进度",
};

interface ClaimDetailPageProps {
  params: Promise<{ claimId: string }>;
}

/**
 * Claim detail (patterns §8 "Claim and Submission detail" archetype):
 * dynamic segment -> the claim/submission island (owner-scoped; the
 * island renders the 403/404-shaped states itself).
 */
export default async function ClaimDetailPage({ params }: ClaimDetailPageProps) {
  const { claimId } = await params;
  return <ClaimDetailView claimId={claimId} />;
}
