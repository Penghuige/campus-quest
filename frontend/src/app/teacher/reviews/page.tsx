import type { Metadata } from "next";

import { SubmissionReview } from "@/features/admin/SubmissionReview";

export const metadata: Metadata = {
  title: "审核队列 · CampusQuest",
  description: "查看通过机器校验的学生提交并作出审核决定",
};

/**
 * Teacher review queue (spec §41/§12.4): master/detail island — the
 * queue first (the workbench's primary loop), the selected submission's
 * judging context beside it on wide layouts.
 */
export default function TeacherReviewsPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">审核队列</h1>
        <p className="page-subtitle">
          通过机器校验的提交按时间先后等待人工审核
        </p>
      </div>
      <SubmissionReview />
    </>
  );
}
